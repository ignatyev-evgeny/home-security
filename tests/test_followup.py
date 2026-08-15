"""Проверка досылки кадров, пока объект в кадре."""
import asyncio, dataclasses, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

from app.config import load_config
from app.guard import Guard
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())
p = tmp / "c.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
base = load_config(p)
assert base.alerts.followup_seconds == 30 and base.alerts.followup_max == 20, base.alerts
print("дефолты из конфига:", base.alerts.followup_seconds, "с ×", base.alerts.followup_max)

def tuned(**kw):
    return dataclasses.replace(base, alerts=dataclasses.replace(base.alerts, **kw))

class N:
    def __init__(self): self.photos, self.texts, self.videos = [], [], []
    async def text(self, m): self.texts.append(m)
    async def photo(self, d, c, filename="s.jpg"): self.photos.append(c)
    async def video(self, d, c, filename="c.mp4"): self.videos.append(c)

class F:
    def __init__(self): self.fail = False
    async def latest_jpeg(self, cam, height=0, quality=0):
        if self.fail: raise RuntimeError("камера отвалилась")
        return b"jpeg"
    async def event_snapshot(self, eid, attempts=3, delay=1.0): return None
    async def event_clip(self, eid, attempts=4, delay=5.0): return b"mp4"

def ev(t, eid="e1", cam="cam_110"):
    return {"type": t, "after": {"camera": cam, "id": eid, "label": "person", "top_score": 0.9}}

async def scenario(name, cfg, body):
    n, f = N(), F()
    st = ArmState(tmp / f"{name}.json"); await st.set_armed(True, "t")
    g = Guard(cfg, st, n, f)
    await body(g, n, f, st)
    await g.shutdown()
    return n

async def main():
    cfg = tuned(followup_seconds=0.05, followup_max=3, send_clip=True)

    # 1. досылка идёт, пока событие живо
    async def s1(g, n, f, st):
        await g.on_event(ev("new"))
        assert len(n.photos) == 1, n.photos
        await asyncio.sleep(0.13)
        assert len(n.photos) >= 3, f"досылки не пришли: {n.photos}"
        assert "всё ещё в кадре" in n.photos[-1]
    n = await scenario("s1", cfg, s1)
    print("1. досылка работает:", len(n.photos), "кадра ·", n.photos[-1].replace("\n", " "))

    # 2. end останавливает досылку и присылает клип
    async def s2(g, n, f, st):
        await g.on_event(ev("new"))
        await asyncio.sleep(0.06)
        got = len(n.photos)
        await g.on_event(ev("end"))
        await asyncio.sleep(0.2)
        assert len(n.photos) == got, f"досылка не остановилась: {n.photos}"
        assert n.videos, "клип не пришёл"
    n = await scenario("s2", cfg, s2)
    print("2. end останавливает досылку, клип приходит:", n.videos)

    # 3. снятие с охраны останавливает досылку
    async def s3(g, n, f, st):
        await g.on_event(ev("new"))
        await asyncio.sleep(0.06)
        got = len(n.photos)
        await st.set_armed(False, "t")
        await asyncio.sleep(0.2)
        assert len(n.photos) == got, f"досылка идёт при снятой охране: {n.photos}"
    await scenario("s3", cfg, s3)
    print("3. снятие с охраны останавливает досылку")

    # 4. предохранитель
    async def s4(g, n, f, st):
        await g.on_event(ev("new"))
        await asyncio.sleep(0.5)
        assert len(n.photos) == 1 + 3, f"превышен предел досылок: {n.photos}"
        assert n.texts and "досылка кадров остановлена" in n.texts[-1], n.texts
    n = await scenario("s4", cfg, s4)
    print("4. предохранитель сработал на 3 досылках:", n.texts[-1][:60], "…")

    # 5. отказ камеры не роняет задачу
    async def s5(g, n, f, st):
        await g.on_event(ev("new"))
        f.fail = True
        await asyncio.sleep(0.2)
        assert len(n.photos) == 1, n.photos
    await scenario("5", cfg, s5)
    print("5. отказ камеры прерывает досылку без падения")

    # 6. followup_seconds: 0 выключает
    async def s6(g, n, f, st):
        await g.on_event(ev("new"))
        await asyncio.sleep(0.2)
        assert len(n.photos) == 1, f"досылка при followup_seconds=0: {n.photos}"
    await scenario("s6", tuned(followup_seconds=0), s6)
    print("6. followup_seconds: 0 отключает досылку")

asyncio.run(main())
print("\nFOLLOWUP OK")
