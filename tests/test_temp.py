"""Порог температуры процессора.

Добавлен 02.09.2026 после замены охлаждения: датчик оборотов замолчал, и
отказ вентилятора стало нечем заметить. Обороты показывает не всякое
железо, а температура есть всегда — при вставшем вентиляторе она уходит
вверх за минуты.
"""
import asyncio, dataclasses, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

from app import guard as guard_mod
from app.config import load_config
from app.guard import Guard, TEMP_STREAK, TEMP_HYSTERESIS
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())
p = tmp / "c.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(p)
LIMIT = cfg.alerts.max_cpu_temp
assert LIMIT == 80.0, LIMIT
print("порог из конфига:", LIMIT, "°C, подтверждений подряд:", TEMP_STREAK)


class N:
    def __init__(self): self.texts = []
    async def text(self, m): self.texts.append(m)
    async def photo(self, *a, **k): return None
    async def video(self, *a, **k): return None


def sensors(cpu, fans=None):
    guard_mod.system.temperatures = lambda: ({"CPU": cpu} if cpu is not None else {})
    guard_mod.system.fans = lambda: (fans or {})


async def main():
    n = N()
    g = Guard(cfg, ArmState(tmp / "s.json"), n, None)

    async def poll(cpu, fans=None):
        sensors(cpu, fans)
        await g._check_temp()

    # рабочий режим машины — 50-55 °C, пики до 70
    for t in (52, 55, 69, 51, 62, 54):
        await poll(t)
    assert not n.texts, n.texts
    print("рабочий диапазон 51-69 °C — тишина")

    # одиночный выброс выше порога не тревожит: пакет скачет на 10 °C за секунды
    await poll(84)
    assert not n.texts, "одиночный пик — не перегрев"
    await poll(52)
    print("одиночный выброс 84 °C проигнорирован")

    # устойчивое превышение — тревога, ровно одна
    for _ in range(TEMP_STREAK - 1):
        await poll(85)
    assert not n.texts, f"тревога раньше {TEMP_STREAK} подтверждений"
    await poll(86, fans={})
    assert len(n.texts) == 1, n.texts
    assert "перегревается" in n.texts[0] and "86 °C" in n.texts[0], n.texts[0]
    assert "датчик оборотов молчит" in n.texts[0], "нет датчика — так и скажи"
    assert "троттлинг" in n.texts[0]
    print("устойчивый перегрев:", n.texts[0].splitlines()[0])

    for _ in range(10):
        await poll(92)
    assert len(n.texts) == 1, "тревога не должна повторяться каждые 30 секунд"
    print("повторов нет даже при 92 °C")

    # у самой границы отбоя нет: там показание колеблется
    n.texts.clear()
    await poll(LIMIT - TEMP_HYSTERESIS + 1)
    assert not n.texts, "у границы порога отбоя быть не должно"
    print(f"{LIMIT - TEMP_HYSTERESIS + 1:.0f} °C — гистерезис держит тревогу")

    # уверенное снижение — отбой
    await poll(54)
    assert len(n.texts) == 1 and "в норме" in n.texts[0], n.texts
    print("отбой:", n.texts[0])

    # и снова тревога, если опять нагреется
    n.texts.clear()
    for _ in range(TEMP_STREAK):
        await poll(88, fans={"Processor Fan": 0})
    assert len(n.texts) == 1 and "перегревается" in n.texts[0]
    print("повторный перегрев замечен снова")

    # обороты попадают в сообщение, когда датчик работает
    n2 = N()
    g2 = Guard(cfg, ArmState(tmp / "s2.json"), n2, None)
    for _ in range(TEMP_STREAK):
        sensors(90, {"Processor Fan": 900}); await g2._check_temp()
    assert "900 об/мин" in n2.texts[0], n2.texts[0]
    print("рабочий датчик оборотов попадает в сообщение: 900 об/мин")

    # датчик температуры недоступен — молчим, а не выдумываем
    n3 = N()
    g3 = Guard(cfg, ArmState(tmp / "s3.json"), n3, None)
    for _ in range(TEMP_STREAK + 2):
        sensors(None); await g3._check_temp()
    assert not n3.texts
    print("недоступный датчик температуры не тревожит")

    # порог 0 отключает слежение
    off = dataclasses.replace(cfg, alerts=dataclasses.replace(cfg.alerts, max_cpu_temp=0.0))
    n4 = N()
    g4 = Guard(off, ArmState(tmp / "s4.json"), n4, None)
    for _ in range(TEMP_STREAK + 2):
        sensors(99); await g4._check_temp()
    assert not n4.texts, "при max_cpu_temp=0 слежения быть не должно"
    print("порог 0 отключает слежение")

asyncio.run(main())
print("\nTEMP OK")
