"""История телеметрии: запись, ретеншн, прореживание и страница."""
import asyncio, os, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

from aiohttp.test_utils import TestServer
from aiohttp import ClientSession

from app.config import load_config
from app.metrics import Metrics
from app.web import build_app
from app.guard import inference_from_stats, gpu_from_stats

tmp = Path(tempfile.mkdtemp())
p = tmp / "c.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(p)
assert cfg.web.enabled and cfg.web.port == 8090 and cfg.web.retention_days == 30
print("конфиг: порт", cfg.web.port, "хранение", cfg.web.retention_days, "дней")

# разбор скорости детектора
assert inference_from_stats({"detectors": {"ov": {"inference_speed": 9.91}}}) == 9.91
assert inference_from_stats({"detectors": {}}) is None
assert inference_from_stats({}) is None
print("скорость детектора разбирается")

# загрузка видеоядра: Frigate отдаёт её строкой с процентом
assert gpu_from_stats({"gpu_usages": {"intel-vaapi": {"gpu": "43.15%"}}}) == 43.1
assert gpu_from_stats({"gpu_usages": {"intel-vaapi": {"gpu": "0.0%"}}}) == 0.0
# прочерк и пустая строка — это «счётчики недоступны», а не «видеоядро простаивает»
for bad in ("-%", "", "n/a", None, 42):
    assert gpu_from_stats({"gpu_usages": {"intel-vaapi": {"gpu": bad}}}) is None, bad
assert gpu_from_stats({}) is None and gpu_from_stats({"gpu_usages": {}}) is None
print("загрузка видеоядра разбирается, недоступные счётчики не превращаются в ноль")


class FakeGuard:
    camera_health = {"a": {"online": True}, "b": {"online": True}, "c": {"online": False}}
    storage = {"free_gb": 375.0}
    inference_ms = 9.9
    gpu_pct = 43.1
    armed = True


async def main():
    m = Metrics(tmp / "m.db", retention_days=30)

    # пустая база не роняет запросы
    assert await m.history(30) == []
    print("пустая история отдаётся как пустой список")

    snap = {"temps": {"CPU": 67.0, "диск": 45.0}, "load": (4.9, 4.6, 4.8),
            "memory": {"used_pct": 26}, "cpus": 8}
    await m.sample(snap, FakeGuard())
    rows = await m.history(1)
    assert len(rows) == 1, rows
    r = rows[0]
    assert (r["cpu_temp"], r["disk_temp"], r["load1"], r["mem_pct"]) == (67.0, 45.0, 4.9, 26)
    # нагрузка округляется: сырое os.getloadavg() даёт 5.52197265625
    await m.sample({**snap, "load": (5.52197265625, 1.0, 1.0)}, FakeGuard())
    assert (await m.history(1))[-1]["load1"] == 5.52, (await m.history(1))[-1]["load1"]
    assert r["free_gb"] == 375.0 and r["inference"] == 9.9 and r["gpu_pct"] == 43.1
    assert r["cameras_ok"] == 2 and r["armed"] == 1
    print("замер записан:", {k: r[k] for k in ("cpu_temp", "load1", "cameras_ok", "armed")})

    # --- разброс температуры за минуту -------------------------------------
    # Пакет процессора гуляет на 10+ °C за секунды, поэтому в минуту пишется
    # не мгновенное значение, а сводка по частым замерам.
    await m.sample(snap, FakeGuard(), cpu_samples=[57.0, 69.0, 61.0, 58.0, 64.0])
    r = (await m.history(1))[-1]
    assert (r["cpu_min"], r["cpu_max"]) == (57.0, 69.0), r
    assert r["cpu_temp"] == 61.8, r["cpu_temp"]
    print(f"сводка за минуту: среднее {r['cpu_temp']}, разброс {r['cpu_min']}–{r['cpu_max']} °C")

    # без накопленных замеров берётся мгновенное значение из снимка
    await m.sample(snap, FakeGuard(), cpu_samples=[])
    r = (await m.history(1))[-1]
    assert r["cpu_temp"] == r["cpu_min"] == r["cpu_max"] == 67.0, r
    print("без накопления пишется мгновенное значение")

    # обороты вентилятора: берём самый быстрый из доступных
    await m.sample({**snap, "fans": {"fan1": 1200, "fan2": 2400}}, FakeGuard(), [60.0])
    assert (await m.history(1))[-1]["fan_rpm"] == 2400
    # датчиков нет — колонка пустая, а не ноль
    await m.sample(snap, FakeGuard(), [60.0])
    assert (await m.history(1))[-1]["fan_rpm"] is None
    print("обороты вентилятора: пишутся при наличии датчика, иначе NULL")

    # датчик недоступен — не выдумываем нули
    await m.sample({"load": (1.0, 1.0, 1.0)}, FakeGuard(), cpu_samples=[])
    r = (await m.history(1))[-1]
    assert r["cpu_temp"] is None and r["cpu_min"] is None, r
    print("недоступный датчик пишется как NULL")

    # недоступные источники не ломают запись
    await asyncio.sleep(0)
    m._write({"ts": int(time.time()) + 1, "cpu_temp": None, "disk_temp": None, "load1": None,
              "mem_pct": None, "free_gb": None, "inference": None, "cameras_ok": None, "armed": 0})
    rows = await m.history(1)
    assert len(rows) == 2 and rows[-1]["cpu_temp"] is None
    print("пропуски пишутся как NULL, а не как ноль")

    # ретеншн: старые замеры удаляются при следующей записи.
    # Сеем напрямую — _write чистит старое в той же транзакции, что и вставляет,
    # поэтому через него засеять «прошлое» нельзя, и это правильное поведение.
    old = int(time.time()) - 40 * 86400
    import sqlite3
    with sqlite3.connect(tmp / "m.db") as db:
        db.execute("INSERT INTO samples (ts, cpu_temp, load1) VALUES (?, ?, ?)", (old, 50.0, 1.0))
    assert any(r["ts"] == old for r in await m.history(90)), "тестовая старая запись не легла"
    await m.sample(snap, FakeGuard())            # запись запускает чистку
    assert not any(r["ts"] == old for r in await m.history(90)), "ретеншн не сработал"
    print("замеры старше 30 дней удаляются")

    # прореживание: браузеру не отдаём десятки тысяч точек
    base = int(time.time()) - 3600
    for i in range(3000):
        m._write({"ts": base + i, "cpu_temp": 60.0 + (i % 10), "disk_temp": None, "load1": 1.0,
                  "mem_pct": 20, "free_gb": 300.0, "inference": 9.0, "cameras_ok": 7, "armed": 0})
    rows = await m.history(1, limit=500)
    assert len(rows) == 500, len(rows)
    assert rows[0]["ts"] < rows[-1]["ts"], "порядок нарушен"
    print(f"прореживание: 3000 замеров -> {len(rows)} точек, порядок сохранён")

    # --- страница и API ---
    server = TestServer(build_app(m)); await server.start_server()
    base_url = f"http://127.0.0.1:{server.port}"
    async with ClientSession() as s:
        async with s.get(f"{base_url}/") as r:
            html = await r.text()
            assert r.status == 200 and r.content_type == "text/html"
        for needle in ("Телеметрия сервера", "cpu_temp", "cpu_min", "cpu_max", "fan_rpm",
                       "gpu_pct", "Видеоядро", "видеоядро",
                       "api/metrics", "30 дней", "разброс",
                       "pointermove", "выдели участок"):
            assert needle in html, needle
        assert "http://" not in html.split("<script>")[1], "страница тянет что-то извне"
        assert "<script src" not in html and "cdn" not in html.lower(), "внешний скрипт"
        print("страница отдаётся, внешних зависимостей нет")

        async with s.get(f"{base_url}/api/metrics?days=1") as r:
            data = await r.json()
            assert r.status == 200 and isinstance(data, list) and data
        # некорректный параметр не роняет
        for q in ("days=abc", "days=-5", "days=99999", ""):
            async with s.get(f"{base_url}/api/metrics?{q}") as r:
                assert r.status == 200, (q, r.status)
        print("API отвечает и не падает на мусорных параметрах")
    await server.close()

    # --- база, созданная прежней версией -----------------------------------
    # На сервере лежат десятки тысяч замеров без новых колонок: миграция
    # обязана добавить их, не тронув накопленное.
    old_db = tmp / "old.db"
    import sqlite3 as sq
    with sq.connect(old_db) as db:
        db.execute("CREATE TABLE samples (ts INTEGER PRIMARY KEY, cpu_temp REAL, "
                   "disk_temp REAL, load1 REAL, mem_pct REAL, free_gb REAL, "
                   "inference REAL, cameras_ok INTEGER, armed INTEGER)")
        db.execute("INSERT INTO samples (ts, cpu_temp, load1) VALUES (?, ?, ?)",
                   (int(time.time()) - 60, 55.5, 3.3))
    m2 = Metrics(old_db, retention_days=30)
    cols = {r[1] for r in sq.connect(old_db).execute("PRAGMA table_info(samples)")}
    for new in ("cpu_min", "cpu_max", "fan_rpm", "gpu_pct"):
        assert new in cols, f"колонка {new} не добавлена при миграции"
    old_rows = await m2.history(1)
    assert len(old_rows) == 1 and old_rows[0]["cpu_temp"] == 55.5, old_rows
    assert old_rows[0]["gpu_pct"] is None, "у старых замеров новой величины быть не может"
    # и запись в мигрированную базу работает
    await m2.sample(snap, FakeGuard(), [60.0])
    assert (await m2.history(1))[-1]["gpu_pct"] == 43.1
    print("старая база мигрирует: колонки добавлены, накопленное цело")

asyncio.run(main())
print("\nMETRICS OK")
