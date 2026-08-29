"""Контроль оперативной памяти.

Написан после аварии 29.08.2026: Frigate потёк, за четыре часа съел все 31 ГБ,
и сервер встал целиком — вместе с ботом, который должен был предупредить.
Данные о памяти писались в базу каждую минуту, но порога тревоги не было.
"""
import asyncio, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

from app import guard as guard_mod
from app import system
from app.config import load_config
from app.guard import Guard, MEM_HYSTERESIS
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())
p = tmp / "c.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(p)
assert cfg.alerts.max_mem_pct == 70.0, cfg.alerts.max_mem_pct
print("порог из конфига:", cfg.alerts.max_mem_pct, "%")


class N:
    def __init__(self): self.texts = []
    async def text(self, m): self.texts.append(m)
    async def photo(self, *a, **k): return None
    async def video(self, *a, **k): return None


def fake_memory(pct):
    """Подменяет /proc/meminfo: 31 ГБ, как на реальной машине."""
    total = 31.0
    guard_mod.system.memory = lambda: (
        None if pct is None else
        {"total_gb": total, "used_gb": round(total * pct / 100, 1), "used_pct": pct})


async def main():
    n = N()
    g = Guard(cfg, ArmState(tmp / "s.json"), n, None)

    async def poll(pct):
        fake_memory(pct)
        await g._check_memory()

    # нормальный режим этой машины — 25%
    await poll(25)
    assert not n.texts, n.texts
    print("25% — тишина")

    # рост до порога: тревога ровно одна, с цифрами
    await poll(69)
    assert not n.texts, "69% ниже порога, тревожить рано"
    await poll(70)
    assert len(n.texts) == 1, n.texts
    assert "70%" in n.texts[0] and "21.7" in n.texts[0] and "31.0" in n.texts[0], n.texts[0]
    print("тревога на пороге:", n.texts[0].splitlines()[0])

    # пока держится высоко — не повторяемся
    for pct in (75, 88, 99):
        await poll(pct)
    assert len(n.texts) == 1, "тревога не должна повторяться каждые 30 секунд"
    print("99% — повторных сообщений нет")

    # у самой границы не мигаем: 66% < 70, но в пределах гистерезиса
    await poll(int(70 - MEM_HYSTERESIS) + 1)
    assert len(n.texts) == 1, "у границы порога не должно быть отбоя"
    print(f"{int(70 - MEM_HYSTERESIS) + 1}% — отбоя нет, гистерезис работает")

    # уверенное снижение — отбой
    await poll(40)
    assert len(n.texts) == 2 and "в норме" in n.texts[1], n.texts
    print("отбой:", n.texts[1])

    # и снова тревога, если потечёт опять
    await poll(85)
    assert len(n.texts) == 3 and "Память занята" in n.texts[2]
    print("повторная утечка снова замечена")

    # датчик недоступен — молчим, а не падаем
    n.texts.clear()
    await poll(None)
    assert not n.texts
    print("недоступный /proc/meminfo не роняет и не тревожит")

    # порог 0 — слежение выключено
    import dataclasses
    off = dataclasses.replace(cfg, alerts=dataclasses.replace(cfg.alerts, max_mem_pct=0.0))
    g2 = Guard(off, ArmState(tmp / "s2.json"), n, None)
    fake_memory(99)
    await g2._check_memory()
    assert not n.texts, "при max_mem_pct=0 слежения быть не должно"
    print("порог 0 отключает слежение")

    # --- главное: тревога работает, когда Frigate НЕ отвечает ----------------
    # Именно в этом состоянии он и течёт. Если проверку памяти вызывать только
    # после успешного /api/stats, она молчит ровно тогда, когда нужна.
    class DeadFrigate:
        async def stats(self): raise RuntimeError("connection refused")

    n.texts.clear()
    g3 = Guard(cfg, ArmState(tmp / "s3.json"), n, DeadFrigate())
    fake_memory(95)
    guard_mod.MONITOR_INTERVAL = 0.01
    task = asyncio.create_task(g3.monitor())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert g3.frigate_ok is False, "Frigate должен числиться недоступным"
    assert n.texts and "Память занята" in n.texts[0], n.texts
    print("Frigate молчит, а тревога по памяти всё равно приходит")

asyncio.run(main())
print("\nMEMORY OK")
