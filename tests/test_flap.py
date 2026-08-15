"""Проверка гашения мигания: одиночный провал fps не должен поднимать тревогу."""
import asyncio, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="123:FAKE", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t",
                  AUTH_TOKEN="", CHAT_IDS="", STATE_PATH=str(Path(tempfile.mkdtemp())/"w.json"))

from app.config import load_config
from app.guard import Guard, cameras_from_stats, OFFLINE_STREAK
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())
p = tmp / "config.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(p)

class N:
    def __init__(self): self.sent = []
    async def text(self, m): self.sent.append(m)
    async def photo(self, *a, **k): pass
    async def video(self, *a, **k): pass

def stats(fps): return {"cameras": {"cam_110": {"camera_fps": fps}}}

async def main():
    n = N()
    g = Guard(cfg, ArmState(tmp / "s.json"), n, None)

    async def poll(fps):
        h = cameras_from_stats(stats(fps))
        g._apply_streaks(h)
        g.camera_health = h
        await g._report_transitions(h)
        return h

    # сценарий из реального лога: перезапуск ffmpeg — один нулевой замер
    await poll(5.0)
    h = await poll(0.0)
    assert not n.sent, f"тревога по одиночному провалу: {n.sent}"
    assert h["cam_110"]["online"] is False, "мгновенное состояние должно быть видно в /cams"
    assert h["cam_110"]["stable"] is True, "но подтверждённым падением ещё не считается"
    await poll(5.0)
    assert not n.sent, f"лишние сообщения при возврате: {n.sent}"
    print(f"одиночный провал проигнорирован (порог {OFFLINE_STREAK})")

    # настоящее падение: три опроса подряд
    for _ in range(OFFLINE_STREAK):
        await poll(0.0)
    assert len(n.sent) == 1 and "не отдаёт поток" in n.sent[0], n.sent
    print("настоящее падение:", n.sent[0])

    # пока лежит — не повторяем
    for _ in range(5):
        await poll(0.0)
    assert len(n.sent) == 1, f"повторные тревоги: {n.sent}"

    # вернулась — одно сообщение
    await poll(5.0)
    assert len(n.sent) == 2 and "снова на связи" in n.sent[1], n.sent
    print("восстановление:", n.sent[1])

    # камеру удалили из конфига — состояние подчищается
    empty = {}
    g._apply_streaks(empty)
    assert not g._offline_streak and not g._offline_reported
    print("очистка после удаления камеры OK")

    await g.shutdown()

asyncio.run(main())


# watchdog: тот же предикат, загружаем файл напрямую — имя `app` занято пакетом guard
import importlib.util
spec = importlib.util.spec_from_file_location("wd_app", ROOT / "watchdog/app.py")
wd = importlib.util.module_from_spec(spec); spec.loader.exec_module(wd)
src = (ROOT / "watchdog/app.py").read_text()
assert '"stable"' in src and '"online"' in src

def down(cams):
    return {n for n, i in cams.items()
            if not (i or {}).get("stable", (i or {}).get("online"))}

assert down({"a": {"online": False, "stable": True}}) == set(), "сторож не должен верить мгновенному online"
assert down({"a": {"online": False, "stable": False}}) == {"a"}
assert down({"a": {"online": False}}) == {"a"}, "откат на online для старого guard"
assert down({"a": {"online": True, "stable": True}}) == set()
print("watchdog читает stable с откатом на online")

print("\nFLAP OK")
