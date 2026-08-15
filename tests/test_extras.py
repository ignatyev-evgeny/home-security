"""Проверка телеметрии хоста и управления подсветкой."""
import asyncio, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

import httpx
from app import system
from app.config import load_config
from app.lighting import Lighting
from app.bot import status_text
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())
p = tmp / "c.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(p)
assert cfg.lighting.enabled is True
print("конфиг: lighting.enabled =", cfg.lighting.enabled)

# --- телеметрия ---------------------------------------------------------------
snap = system.snapshot()
print("снимок хоста:", {k: v for k, v in snap.items()})
assert "cpus" in snap and snap["cpus"] > 0
line = system.format_line(snap)
print("строка для /status:", line)
assert line and "Нагрузка" in line

# отсутствующие источники не должны ронять
assert system.format_line({}) is None
assert system.format_line(None) is None
assert system.format_line({"cpus": 8}) is None
only_temp = system.format_line({"temps": {"CPU": 61.0}})
assert only_temp == "CPU 61 °C", only_temp
# перегрузка помечается предупреждением
hot = system.format_line({"cpus": 8, "load": (12.0, 9.0, 7.0)})
assert hot.startswith("⚠️"), hot
calm = system.format_line({"cpus": 8, "load": (3.0, 3.0, 3.0)})
assert calm.startswith("🖥"), calm
print("частичные данные и порог перегрузки:", hot, "|", calm)

st = ArmState(tmp / "s.json")
txt = status_text(cfg, st, {}, {"free_gb": 100.0, "total_gb": 169.0})
assert "Нагрузка" in txt, txt
print("в /status телеметрия есть\n")


# --- подсветка ----------------------------------------------------------------
CONFIG = {"cameras": {
    "cam_110": {"ffmpeg": {"inputs": [{"path": "rtsp://admin:pw@192.168.1.110:554/cam/realmonitor?channel=1&subtype=0"}]}},
    "cam_111": {"ffmpeg": {"inputs": [{"path": "rtsp://admin:pw@192.168.1.111:554/cam/realmonitor?channel=1&subtype=0"}]}},
    "cam_117": {"ffmpeg": {"inputs": [{"path": "rtsp://192.168.1.117:554/user=admin_password=pw_channel=0_stream=0.sdp?real_stream"}]}},
}}

class FakeFrigate:
    async def config(self): return CONFIG

STATE = {"192.168.1.110": False, "192.168.1.111": True}
CALLS = []

class T(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        host = request.url.host
        q = request.url.query.decode()
        CALLS.append((host, q))
        if host not in STATE:                       # камера без подсветки
            return httpx.Response(200, text="")
        if "setConfig" in q:
            STATE[host] = "Enable=true" in q
            return httpx.Response(200, text="OK\r\n")
        return httpx.Response(200, text=f"table.FlashLight.Enable={str(STATE[host]).lower()}\r\ntable.FlashLight.Brightness=51\r\n")

async def main():
    lt = Lighting(FakeFrigate(), "admin", "pw")
    lt._client = httpx.AsyncClient(transport=T(), timeout=5)

    hosts = await lt.hosts()
    assert hosts == {"cam_110": "192.168.1.110", "cam_111": "192.168.1.111",
                     "cam_117": "192.168.1.117"}, hosts
    print("адреса из конфига Frigate:", hosts)

    s = await lt.states()
    assert s == {"cam_110": False, "cam_111": True, "cam_117": None}, s
    print("состояния:", s, "— XiongMai корректно отвалилась в None")

    # повторный опрос не дёргает камеру без поддержки
    CALLS.clear()
    await lt.states()
    assert not any(h == "192.168.1.117" for h, _ in CALLS), CALLS
    print("повторный опрос пропускает неподдерживаемую камеру")

    assert await lt.set("cam_110", True) is True
    assert STATE["192.168.1.110"] is True
    print("включение одной камеры работает")

    res = await lt.set_all(False)
    assert res == {"cam_110": True, "cam_111": True}, res
    assert STATE == {"192.168.1.110": False, "192.168.1.111": False}, STATE
    print("выключение всех:", res)

    res = await lt.set_all(True)
    assert all(STATE.values()), STATE
    assert "cam_117" not in res, "неподдерживаемая камера попала в set_all"
    print("включение всех:", res)

    # обрыв связи не должен навсегда исключать камеру из опроса
    lt2 = Lighting(FakeFrigate(), "admin", "pw")
    class Broken(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("сеть недоступна")
    lt2._client = httpx.AsyncClient(transport=Broken(), timeout=5)
    assert await lt2.state("cam_110") is None
    assert lt2._supported == {}, "недоступность приняли за отсутствие поддержки"
    await lt2.aclose()
    print("обрыв связи не помечает камеру неподдерживаемой")

    assert await lt.brightness("cam_111") == 51
    assert await lt.state("нет_такой") is None
    assert await lt.set("нет_такой", True) is False
    print("яркость читается, неизвестная камера не роняет")

    await lt.aclose()

asyncio.run(main())
print("\nEXTRAS OK")
