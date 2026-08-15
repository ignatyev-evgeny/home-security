"""Проверка ссылок на клип для случаев, когда видео в Telegram не уехало."""
import asyncio, dataclasses, os, re, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

from app.config import load_config
from app.frigate import FrigateError
from app.guard import Guard, TELEGRAM_FILE_LIMIT
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())
p = tmp / "c.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(p)
assert cfg.frigate.public_url == "https://192.168.1.50:8971", cfg.frigate.public_url
print("public_url из конфига:", cfg.frigate.public_url)

class N:
    def __init__(self): self.texts, self.videos = [], []
    async def text(self, m): self.texts.append(m)
    async def photo(self, d, c, filename="s.jpg"): return None
    async def video(self, d, c, filename="c.mp4"): self.videos.append(c); return self.err
    err = None

class F:
    def __init__(self, clip=b"mp4", exc=None): self.clip, self.exc = clip, exc
    async def latest_jpeg(self, cam, height=0, quality=0): return b"jpeg"
    async def event_clip(self, eid, attempts=4, delay=5.0):
        if self.exc: raise self.exc
        return self.clip

async def run(cfg, frigate, notifier):
    st = ArmState(tmp / "s.json"); await st.set_armed(True, "t")
    g = Guard(cfg, st, notifier, frigate)
    await g._send_clip("cam_110", "1786680000.123456-abcdef")
    await g.shutdown()

def links(text):
    return re.findall(r'href="([^"]+)"', text)

async def main():
    # 1. большой клип -> ссылки
    n = N()
    await run(cfg, F(clip=b"x" * (TELEGRAM_FILE_LIMIT + 1)), n)
    assert not n.videos
    got = links(n.texts[0])
    assert got == ["https://192.168.1.50:8971/api/events/1786680000.123456-abcdef/clip.mp4",
                   "https://192.168.1.50:8971/explore"], got
    print("1. большой клип:", n.texts[0].replace("\n", "\n   "))

    # 2. клипа нет — ссылки тоже полезны
    n = N()
    await run(cfg, F(exc=FrigateError("клип не появился за 20 с")), n)
    assert len(links(n.texts[0])) == 2, n.texts[0]
    print("2. клипа нет — ссылки на месте")

    # 3. Telegram отверг файл
    n = N(); n.err = "Request Entity Too Large"
    await run(cfg, F(clip=b"y" * 1024), n)
    assert len(links(n.texts[0])) == 2, n.texts[0]
    print("3. Telegram отверг — ссылки на месте")

    # 4. успех — лишних сообщений и ссылок нет
    n = N()
    await run(cfg, F(clip=b"z" * 2048), n)
    assert n.videos and not n.texts, (n.videos, n.texts)
    print("4. успешный клип: без лишних сообщений")

    # 5. public_url не задан — подсказка вместо битой ссылки
    nocfg = dataclasses.replace(cfg, frigate=dataclasses.replace(cfg.frigate, public_url=""))
    n = N()
    await run(nocfg, F(clip=b"x" * (TELEGRAM_FILE_LIMIT + 1)), n)
    assert not links(n.texts[0]), n.texts[0]
    assert "public_url" in n.texts[0], n.texts[0]
    print("5. без public_url:", n.texts[0].split("\n")[-1])

    # 6. хвостовой слэш не удваивается
    slash = dataclasses.replace(cfg, frigate=dataclasses.replace(cfg.frigate, public_url="http://h:8971/"))
    p2 = tmp / "c2.yaml"
    p2.write_text((ROOT / "guard/config.example.yaml").read_text()
                  .replace("- 000000000", "- 42")
                  .replace('public_url: "https://192.168.1.50:8971"', 'public_url: "http://h:8971/"'))
    assert load_config(p2).frigate.public_url == "http://h:8971", load_config(p2).frigate.public_url
    print("6. хвостовой слэш срезается")

asyncio.run(main())
print("\nLINKS OK")
