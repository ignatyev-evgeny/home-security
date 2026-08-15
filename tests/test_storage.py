"""Проверка контроля свободного места: порог, переходы, отчёт сторожу."""
import asyncio, dataclasses, os, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

from app.config import load_config
from app.guard import Guard, storage_from_stats
from app.bot import status_text
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())
p = tmp / "c.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(p)
assert cfg.alerts.min_free_gb == 20.0, cfg.alerts.min_free_gb
print("порог из конфига:", cfg.alerts.min_free_gb, "ГБ")

# разбор ответа Frigate (значения в МБ, как в реальном /api/stats)
real = {"service": {"storage": {
    "/media/frigate/recordings": {"total": 173090.8, "used": 3612.2, "free": 160615.0},
    "/media/frigate/clips": {"total": 173090.8, "used": 3612.2, "free": 160615.0},
}}}
st = storage_from_stats(real)
assert st == {"free_gb": 156.9, "total_gb": 169.0}, st
print("разбор /api/stats:", st)
assert storage_from_stats({}) == {}, "пустая статистика не должна падать"
assert storage_from_stats({"service": {}}) == {}
print("пустой ответ обрабатывается")

class N:
    def __init__(self): self.texts = []
    async def text(self, m): self.texts.append(m)
    async def photo(self, *a, **k): return None
    async def video(self, *a, **k): return None

def stats(free_gb):
    return {"cameras": {}, "service": {"storage": {
        "/media/frigate/recordings": {"total": 170 * 1024, "free": free_gb * 1024}}}}

async def main():
    n = N()
    state = ArmState(tmp / "s.json")
    g = Guard(cfg, state, n, None)

    async def poll(free_gb):
        g.storage = storage_from_stats(stats(free_gb))
        await g._check_storage()

    await poll(150)
    assert not n.texts, n.texts
    assert not g.storage_low
    print("150 ГБ — тишина")

    await poll(15)
    assert len(n.texts) == 1 and "Мало места" in n.texts[0], n.texts
    assert g.storage_low
    print("15 ГБ:", n.texts[0].split("\n")[0])

    await poll(10)
    assert len(n.texts) == 1, f"повторное предупреждение: {n.texts}"
    print("повторов нет, пока не восстановилось")

    await poll(80)
    assert len(n.texts) == 2 and "в норме" in n.texts[1], n.texts
    assert not g.storage_low
    print("восстановление:", n.texts[1])

    # ровно на пороге тревоги нет
    await poll(20)
    assert len(n.texts) == 2, n.texts
    print("ровно порог — не тревога")

    # порог 0 отключает
    off = dataclasses.replace(cfg, alerts=dataclasses.replace(cfg.alerts, min_free_gb=0))
    n2 = N(); g2 = Guard(off, state, n2, None)
    g2.storage = storage_from_stats(stats(1))
    await g2._check_storage()
    assert not n2.texts and not g2.storage_low, n2.texts
    print("min_free_gb: 0 отключает контроль")

    # статус в боте
    g.storage = storage_from_stats(stats(150))
    assert "💾" in status_text(cfg, state, {}, g.storage)
    g.storage = storage_from_stats(stats(5))
    assert "⚠️" in status_text(cfg, state, {}, g.storage)
    assert "Свободно" not in status_text(cfg, state, {}, {})
    print("статус: значок меняется, без данных строки нет")

    await g.shutdown(); await g2.shutdown()

asyncio.run(main())

# --- сторож ------------------------------------------------------------------
os.environ.update(AUTH_TOKEN="s", CHAT_IDS="", STATE_PATH=str(tmp / "w.json"),
                  STALE_SECONDS="999", CHECK_INTERVAL="999")
for m in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
    del sys.modules[m]
sys.path.remove(str(ROOT / "guard"))
import importlib.util
spec = importlib.util.spec_from_file_location("wd", ROOT / "watchdog/app.py")
wd = importlib.util.module_from_spec(spec); spec.loader.exec_module(wd)

w = wd.Watch()
assert w.storage_low is False, "storage_low должен существовать без файла состояния"
print("сторож: storage_low инициализирован без файла состояния")

SENT = []
async def fake(session, text): SENT.append(text)
wd.notify = fake

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

def hb(free, low):
    return {"site": "Дом", "ts": time.time(), "armed": True, "frigate_ok": True,
            "cameras": {}, "storage": {"free_gb": free, "total_gb": 169.0, "low": low}}

async def wmain():
    server = TestServer(wd.build_app()); await server.start_server()
    base = f"http://127.0.0.1:{server.port}"; H = {"X-Auth-Token": "s"}
    async with ClientSession() as s:
        async with s.post(f"{base}/heartbeat", json=hb(150, False), headers=H) as r:
            assert r.status == 200
        assert not SENT, SENT
        async with s.post(f"{base}/heartbeat", json=hb(12, True), headers=H) as r: pass
        assert len(SENT) == 1 and "мало места" in SENT[0].lower(), SENT
        async with s.post(f"{base}/heartbeat", json=hb(11, True), headers=H) as r: pass
        assert len(SENT) == 1, f"повтор: {SENT}"
        async with s.post(f"{base}/heartbeat", json=hb(90, False), headers=H) as r: pass
        assert len(SENT) == 2 and "в норме" in SENT[1], SENT
        print("сторож:", SENT[0], "|", SENT[1])
        async with s.get(f"{base}/health") as r:
            pub = await r.json()
        assert "storage" not in pub, f"место видно без токена: {pub}"
        async with s.get(f"{base}/health", headers=H) as r:
            h = await r.json()
        assert h["storage"]["free_gb"] == 90, h
        print("health отдаёт место только по токену:", h["storage"])
    await server.close()

asyncio.run(wmain())
print("\nSTORAGE OK")
