"""Первое фото тревоги — снимок события с рамкой, с откатом на живой кадр."""
import asyncio, dataclasses, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

import httpx
from app.config import load_config
from app.frigate import FrigateClient, FrigateSettings
from app.guard import Guard
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())
p = tmp / "c.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(p)
assert cfg.alerts.event_snapshot is True
print("конфиг: event_snapshot =", cfg.alerts.event_snapshot)

# отключаем досылки и клипы: здесь интересует только первое фото
quiet = dataclasses.replace(cfg, alerts=dataclasses.replace(
    cfg.alerts, followup_seconds=0, send_clip=False))

SNAP, LIVE = b"\xff\xd8SNAP-with-bbox", b"\xff\xd8LIVE-frame"

class N:
    def __init__(self): self.photos, self.texts = [], []
    async def text(self, m): self.texts.append(m)
    async def photo(self, data, caption, filename="s.jpg"): self.photos.append(data); return None
    async def video(self, *a, **k): return None

class F:
    def __init__(self, snap=SNAP, live_fail=False):
        self.snap, self.live_fail, self.calls = snap, live_fail, []
    async def event_snapshot(self, eid, attempts=3, delay=1.0):
        self.calls.append("snapshot"); return self.snap
    async def latest_jpeg(self, cam, height=0, quality=0):
        self.calls.append("live")
        if self.live_fail: raise RuntimeError("камера молчит")
        return LIVE

def ev(eid="e1"):
    return {"type": "new", "after": {"camera": "cam_112", "id": eid, "label": "person",
                                     "data": {"top_score": 0.83}}}

async def run(cfg_, frigate):
    n = N()
    st = ArmState(tmp / f"{id(frigate)}.json"); await st.set_armed(True, "t")
    g = Guard(cfg_, st, n, frigate)
    await g.on_event(ev())
    await g.shutdown()
    return n, frigate

async def main():
    # 1. снимок события готов — уходит он, живой кадр не трогаем
    n, f = await run(quiet, F())
    assert n.photos == [SNAP], n.photos
    assert f.calls == ["snapshot"], f.calls
    print("1. снимок с рамкой ушёл, живой кадр не запрашивался")

    # 2. снимка нет (ещё не записан) — откат на живой кадр
    n, f = await run(quiet, F(snap=None))
    assert n.photos == [LIVE], n.photos
    assert f.calls == ["snapshot", "live"], f.calls
    print("2. снимка нет — ушёл живой кадр")

    # 3. ни снимка, ни живого кадра — текст с причиной, без падения
    n, f = await run(quiet, F(snap=None, live_fail=True))
    assert n.photos == [] and n.texts and "кадр не получен" in n.texts[0], (n.photos, n.texts)
    print("3. оба источника недоступны — объяснение текстом")

    # 4. event_snapshot: false — сразу живой кадр, снимок события не дёргаем
    off = dataclasses.replace(quiet, alerts=dataclasses.replace(quiet.alerts, event_snapshot=False))
    n, f = await run(off, F())
    assert n.photos == [LIVE] and f.calls == ["live"], (n.photos, f.calls)
    print("4. event_snapshot: false — только живой кадр")

    # 5. клиент: 404 повторяется, другие ошибки — нет
    seen = []
    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append(str(request.url))
            n404 = sum(1 for u in seen if "snapshot" in u)
            if n404 < 3: return httpx.Response(404)
            return httpx.Response(200, content=SNAP)
    c = FrigateClient(FrigateSettings(url="http://frigate:5000", password=""))
    c._client = httpx.AsyncClient(base_url="http://frigate:5000", transport=T())
    got = await c.event_snapshot("abc", attempts=3, delay=0.01)
    assert got == SNAP and len(seen) == 3, (got, seen)
    assert seen[0].endswith("/api/events/abc/snapshot.jpg?bbox=1"), seen[0]
    print("5. клиент: до трёх попыток на 404, URL с bbox=1")

    seen.clear()
    class T500(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append(1); return httpx.Response(500)
    c._client = httpx.AsyncClient(base_url="http://frigate:5000", transport=T500())
    assert await c.event_snapshot("abc", attempts=3, delay=0.01) is None
    assert len(seen) == 1, "на 500 повторять бессмысленно"
    print("6. клиент: на 500 сдаётся сразу")
    await c.aclose()

asyncio.run(main())
print("\nEVENT SNAPSHOT OK")
