"""Проверка guard: конфиг, правка конфига Frigate, логика алертов и живости камер."""
import asyncio, dataclasses, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(
    BOT_TOKEN="123:FAKE", FRIGATE_PASSWORD="", CAM_PASSWORD="secret",
    WATCHDOG_URL="https://wd.example.com/heartbeat", WATCHDOG_TOKEN="tok",
)

from app.config import load_config
from app.cameras import add_camera, remove_camera, list_cameras, CameraEditError
from app.guard import Guard, cameras_from_stats
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())

# --- конфиг -----------------------------------------------------------------
cfg_path = tmp / "config.yaml"
cfg_path.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(cfg_path)
assert cfg.frigate.url == "http://frigate:5000"
assert cfg.frigate.password == "", "пустой FRIGATE_PASSWORD должен отключать логин"
assert cfg.heartbeat.enabled and cfg.heartbeat.url.startswith("https://")
assert cfg.alerts.labels == ("person",)
print("config OK:", cfg.mqtt.host, "| chat_ids", cfg.allowed_chat_ids, "| heartbeat", cfg.heartbeat.interval_seconds, "с")

# --- правка конфига Frigate -------------------------------------------------
raw = (ROOT / "frigate/config.example.yml").read_text()
BASE = len(list_cameras(raw))
assert BASE >= 6, BASE

added = add_camera(raw, "prihozhaya", "192.168.1.114", cfg.camera_defaults)
assert "prihozhaya" in list_cameras(added)
assert "{FRIGATE_RTSP_PASSWORD}" in added and "secret" not in added, "реальный пароль утёк в конфиг!"
block = added.split("prihozhaya")[1].split("cam_")[0]
assert block.count("- path:") == 1, "detect и record на одном потоке — вход должен быть один"
assert "- record" in block and "- detect" in block, "у входа нет обеих ролей"
# комментарии и остальные камеры не потеряны
assert "OpenVINO" in added and "cam_120" in added, "ruamel потерял комментарии/камеры"
print("add_camera OK ->", list_cameras(added))

removed = remove_camera(added, "cam_112")
assert "cam_112" not in list_cameras(removed) and len(list_cameras(removed)) == BASE
print("remove_camera OK ->", list_cameras(removed))

for bad in ("Prihozhaya", "при-хожая", "9cam", "a"):
    try:
        add_camera(raw, bad, "1.2.3.4", cfg.camera_defaults)
    except CameraEditError:
        pass
    else:
        raise SystemExit(f"FAIL: имя {bad!r} прошло валидацию")
try:
    add_camera(raw, "cam_110", "1.2.3.4", cfg.camera_defaults)
except CameraEditError as e:
    print("дубликат отвергнут:", e)
else:
    raise SystemExit("FAIL: дубликат камеры прошёл")

single = "cameras:\n  only:\n    ffmpeg: {inputs: []}\n"
try:
    remove_camera(single, "only")
except CameraEditError as e:
    print("последняя камера защищена:", e)
else:
    raise SystemExit("FAIL: удалили последнюю камеру")

# --- stats ------------------------------------------------------------------
stats = {"cameras": {
    "cam_110": {"camera_fps": 5.0, "detection_fps": 0.3},
    "cam_111": {"camera_fps": 0.0, "detection_fps": 0.0},
}}
health = cameras_from_stats(stats)
assert health["cam_110"]["online"] and not health["cam_111"]["online"]
print("stats OK:", health)


# --- логика алертов ---------------------------------------------------------
class FakeNotifier:
    def __init__(self): self.sent = []
    async def text(self, m): self.sent.append(("text", m))
    async def photo(self, d, c, filename="s.jpg"): self.sent.append(("photo", c))
    async def video(self, d, c, filename="c.mp4"): self.sent.append(("video", c))

class FakeFrigate:
    def __init__(self): self.stats_payload = {"cameras": {}}
    async def latest_jpeg(self, cam, height=0, quality=0): return b"\xff\xd8jpeg"
    async def event_clip(self, eid, attempts=4, delay=5.0): return b"mp4"
    async def stats(self): return self.stats_payload

def event(etype, cam="cam_110", eid="e1", label="person", score=0.9, fp=False):
    return {"type": etype, "after": {"camera": cam, "id": eid, "label": label,
                                     "top_score": score, "false_positive": fp}}

async def main():
    notifier, frigate = FakeNotifier(), FakeFrigate()
    state = ArmState(tmp / "state.json")
    # досылка проверяется отдельно в test_followup.py; здесь она только
    # мешала бы — фоновые задачи спят по 30 секунд
    quiet = dataclasses.replace(cfg, alerts=dataclasses.replace(cfg.alerts, followup_seconds=0))
    g = Guard(quiet, state, notifier, frigate)

    # снято с охраны — тишина
    await g.on_event(event("new"))
    assert not notifier.sent, "алерт при снятой охране"

    await state.set_armed(True, "тест")

    # не тот объект — тишина
    await g.on_event(event("new", label="cat", eid="e0"))
    assert not notifier.sent, "алерт на нецелевой объект"
    # ложное срабатывание — тишина
    await g.on_event(event("new", eid="e0", fp=True))
    assert not notifier.sent, "алерт на false_positive"

    await g.on_event(event("new"))
    assert notifier.sent[-1][0] == "photo", notifier.sent
    print("алерт:", notifier.sent[-1][1].replace("\n", " | "))

    # второе событие той же камеры в кулдауне
    before = len(notifier.sent)
    await g.on_event(event("new", eid="e2"))
    assert len(notifier.sent) == before, "кулдаун не сработал"

    # другая камера проходит сразу
    await g.on_event(event("new", cam="cam_111", eid="e3"))
    assert len(notifier.sent) == before + 1, "кулдаун ошибочно общий для всех камер"

    # клип по окончании события
    await g.on_event(event("end"))
    await asyncio.gather(*g._background, return_exceptions=True)
    assert any(k == "video" for k, _ in notifier.sent), "клип не отправлен"
    # событие, которое не проходило по кулдауну, клип не порождает
    await g.on_event(event("end", eid="e2"))
    assert sum(1 for k, _ in notifier.sent if k == "video") == 1, "лишний клип"
    print("клипы OK")

    # переходы online/offline
    notifier.sent.clear()
    async def poll(fps):
        h = cameras_from_stats({"cameras": {"cam_110": {"camera_fps": fps}}})
        g._apply_streaks(h); await g._report_transitions(h)
    for _ in range(3):
        await poll(0.0)
    assert "не отдаёт поток" in notifier.sent[-1][1]
    n = len(notifier.sent)
    await poll(0.0)
    assert len(notifier.sent) == n, "повторное сообщение о той же упавшей камере"
    await poll(5.0)
    assert "снова на связи" in notifier.sent[-1][1]
    print("переходы OK:", [m for _, m in notifier.sent])

    await g.shutdown()

asyncio.run(main())
print("\nGUARD OK")
