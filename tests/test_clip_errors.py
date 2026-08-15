"""Проверка: пропавшее видео объясняется в чате, а не молчит."""
import asyncio, dataclasses, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

import httpx
from app.config import load_config
from app.frigate import FrigateClient, FrigateError, FrigateSettings
from app.guard import Guard, TELEGRAM_FILE_LIMIT
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())
p = tmp / "c.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(p)
assert cfg.alerts.snapshot_height == 0 and cfg.alerts.snapshot_quality == 95, cfg.alerts
print("качество снимков:", "родное разрешение, quality", cfg.alerts.snapshot_quality)

class N:
    def __init__(self, video_error=None):
        self.texts, self.videos, self.photos = [], [], []
        self.video_error = video_error
    async def text(self, m): self.texts.append(m)
    async def photo(self, d, c, filename="s.jpg"): self.photos.append(c); return None
    async def video(self, d, c, filename="c.mp4"):
        self.videos.append((c, len(d)))
        return self.video_error

class F:
    def __init__(self, clip=b"mp4", exc=None): self.clip, self.exc = clip, exc
    async def latest_jpeg(self, cam, height=0, quality=0): return b"jpeg"
    async def event_clip(self, eid, attempts=4, delay=5.0):
        if self.exc: raise self.exc
        return self.clip

async def run(frigate, notifier):
    st = ArmState(tmp / "s.json"); await st.set_armed(True, "t")
    g = Guard(cfg, st, notifier, frigate)
    await g._send_clip("cam_110", "e1")
    await g.shutdown()

async def main():
    # 1. клип не появился
    n = N()
    await run(F(exc=FrigateError("клип не появился за 20 с. Обычно это значит, что для камеры выключена запись (record)")), n)
    assert not n.videos and n.texts and "клип не получен" in n.texts[0], (n.texts, n.videos)
    print("1.", n.texts[0][:110], "…")

    # 2. клип больше лимита Telegram
    n = N()
    await run(F(clip=b"x" * (TELEGRAM_FILE_LIMIT + 1)), n)
    assert not n.videos, "огромный клип всё-таки пытались отправить"
    assert "больше лимита Telegram" in n.texts[0], n.texts
    print("2.", n.texts[0][:110], "…")

    # 3. Telegram отверг файл
    n = N(video_error="Request Entity Too Large")
    await run(F(clip=b"y" * 1024), n)
    assert n.videos and "не ушёл в Telegram" in n.texts[0], (n.texts, n.videos)
    print("3.", n.texts[0][:110], "…")

    # 4. всё хорошо — текстов нет, размер в подписи
    n = N()
    await run(F(clip=b"z" * (2 * 1024 * 1024)), n)
    assert n.videos and not n.texts, (n.texts, n.videos)
    print("4. успех:", n.videos[0][0])

    # 5. неожиданная ошибка тоже доезжает до чата
    n = N()
    await run(F(exc=RuntimeError("тестовый сбой")), n)
    assert n.texts and "тестовый сбой" in n.texts[0], n.texts
    print("5.", n.texts[0][:90], "…")

asyncio.run(main())

# --- URL снимка: без параметров при нулевых настройках ----------------------
seen = []
class T(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"jpeg")

async def urls():
    c = FrigateClient(FrigateSettings(url="http://frigate:5000", password=""))
    c._client = httpx.AsyncClient(base_url="http://frigate:5000", transport=T())
    await c.latest_jpeg("cam_110", 0, 0)
    await c.latest_jpeg("cam_110", 0, 90)
    await c.latest_jpeg("cam_110", 1080, 90)
    await c.aclose()

asyncio.run(urls())
assert seen[0].endswith("/api/cam_110/latest.jpg"), seen[0]
assert seen[1].endswith("latest.jpg?quality=90"), seen[1]
assert seen[2].endswith("latest.jpg?h=1080&quality=90"), seen[2]
print("URL снимков:", *seen, sep="\n  ")

print("\nCLIP ERRORS OK")
