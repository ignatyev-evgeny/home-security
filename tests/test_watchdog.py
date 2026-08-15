"""Проверка сторожа: авторизация, переходы камер, тревога по молчанию, /health."""
import asyncio, os, sys, tempfile, time
from pathlib import Path

os.environ.update(
    AUTH_TOKEN="s3cret", BOT_TOKEN="", CHAT_IDS="",
    STALE_SECONDS="2", CHECK_INTERVAL="1",
    STATE_PATH=str(Path(tempfile.mkdtemp()) / "state.json"),
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "watchdog"))

import app as wd
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

SENT = []
async def fake_notify(session, text):
    SENT.append(text)
wd.notify = fake_notify  # перехватываем отправку в Telegram

def hb(cameras, frigate_ok=True, armed=True):
    return {"site": "Дом", "ts": time.time(), "armed": armed,
            "frigate_ok": frigate_ok, "cameras": cameras}

UP = {"cam_110": {"online": True}, "cam_111": {"online": True}}
DOWN = {"cam_110": {"online": True}, "cam_111": {"online": False}}

async def main():
    server = TestServer(wd.build_app())
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"

    async with ClientSession() as s:
        # без токена
        async with s.post(f"{base}/heartbeat", json=hb(UP)) as r:
            assert r.status == 401, r.status
        async with s.post(f"{base}/heartbeat", json=hb(UP),
                          headers={"X-Auth-Token": "wrong"}) as r:
            assert r.status == 401
        print("авторизация OK")

        H = {"X-Auth-Token": "s3cret"}
        async with s.post(f"{base}/heartbeat", json=hb(UP), headers=H) as r:
            assert r.status == 200
        assert not SENT, SENT

        # камера упала
        async with s.post(f"{base}/heartbeat", json=hb(DOWN), headers=H) as r:
            assert r.status == 200
        assert len(SENT) == 1 and "cam_111" in SENT[0], SENT
        # повтор не дублируется
        async with s.post(f"{base}/heartbeat", json=hb(DOWN), headers=H) as r:
            pass
        assert len(SENT) == 1, SENT
        # вернулась
        async with s.post(f"{base}/heartbeat", json=hb(UP), headers=H) as r:
            pass
        assert "снова на связи" in SENT[-1], SENT
        print("переходы камер OK:", len(SENT), "сообщения")

        # Frigate упал и поднялся
        SENT.clear()
        async with s.post(f"{base}/heartbeat", json=hb(UP, frigate_ok=False), headers=H) as r:
            pass
        assert "Frigate не отвечает" in SENT[-1], SENT
        async with s.post(f"{base}/heartbeat", json=hb(UP), headers=H) as r:
            pass
        assert "Frigate снова работает" in SENT[-1], SENT
        print("Frigate OK")

        # молчание -> тревога
        SENT.clear()
        await asyncio.sleep(4)
        assert SENT and "не выходит на связь" in SENT[0], SENT
        assert "под охраной" in SENT[0], SENT
        n = len(SENT)
        await asyncio.sleep(2)
        assert len(SENT) == n, "тревога о молчании повторяется"
        print("тревога по молчанию OK:", SENT[0].replace("\n", " | "))

        # без токена — только жив/не жив, без наводок на состояние дома
        async with s.get(f"{base}/health") as r:
            pub = await r.json()
        assert set(pub) == {"ok", "age_seconds"}, pub
        assert pub["ok"] is False
        print("health без токена:", pub)
        async with s.get(f"{base}/health", headers=H) as r:
            health = await r.json()
        assert health["ok"] is False and health["armed"] is True
        assert "cameras_down" in health and "storage" in health
        print("health с токеном:", {k: health[k] for k in ("armed", "cameras_down")})

        # связь восстановилась
        SENT.clear()
        async with s.post(f"{base}/heartbeat", json=hb(UP), headers=H) as r:
            pass
        assert "связь восстановлена" in SENT[-1], SENT
        print("восстановление OK:", SENT[-1])

        # состояние переживает рестарт сторожа
        w2 = wd.Watch()
        assert w2.last_seen > 0 and not w2.site_down
        print("persist OK")

    await server.close()

asyncio.run(main())
print("\nWATCHDOG OK")
