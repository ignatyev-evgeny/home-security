"""Внешний сторож: принимает отчёты от домашнего сервера и бьёт тревогу, когда они пропали.

Смысл в том, чтобы жить вне дома: если пропал свет, интернет или упал сам
сервер, локальный бот сообщить об этом уже не сможет — молчание и есть сигнал.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import ClientError, ClientSession, web

log = logging.getLogger("watchdog")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_IDS = tuple(int(x) for x in os.environ.get("CHAT_IDS", "").replace(" ", "").split(",") if x)
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
STALE_SECONDS = int(os.environ.get("STALE_SECONDS", "180"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))
STATE_PATH = Path(os.environ.get("STATE_PATH", "/data/state.json"))
PORT = int(os.environ.get("PORT", "8080"))


class Watch:
    def __init__(self) -> None:
        self.last_seen = 0.0
        self.last_payload: dict = {}
        self.site_down = False
        self.cameras_down: set[str] = set()
        self.frigate_down = False
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.last_seen = float(data.get("last_seen") or 0.0)
        self.site_down = bool(data.get("site_down"))
        self.frigate_down = bool(data.get("frigate_down"))
        self.cameras_down = set(data.get("cameras_down") or ())

    def save(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "last_seen": self.last_seen,
                    "site_down": self.site_down,
                    "frigate_down": self.frigate_down,
                    "cameras_down": sorted(self.cameras_down),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, STATE_PATH)


async def notify(session: ClientSession, text: str) -> None:
    if not BOT_TOKEN or not CHAT_IDS:
        log.warning("некуда слать (%s): BOT_TOKEN или CHAT_IDS пусты", text)
        return
    for chat_id in CHAT_IDS:
        try:
            async with session.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=15,
            ) as response:
                if response.status >= 400:
                    log.warning("Telegram ответил %s: %.200s", response.status, await response.text())
        except (ClientError, asyncio.TimeoutError) as exc:
            log.warning("не отправить в %s: %s", chat_id, exc)


def _fmt_gap(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} с"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60} мин"


async def handle_heartbeat(request: web.Request) -> web.Response:
    if AUTH_TOKEN and request.headers.get("X-Auth-Token") != AUTH_TOKEN:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "bad json"}, status=400)

    watch: Watch = request.app["watch"]
    session: ClientSession = request.app["session"]

    was_down, gap = watch.site_down, time.time() - watch.last_seen
    watch.last_seen = time.time()
    watch.last_payload = payload
    site = str(payload.get("site") or "home")

    if was_down:
        watch.site_down = False
        await notify(session, f"✅ <b>{site}</b>: связь восстановлена (молчал {_fmt_gap(gap)}).")

    # Frigate
    frigate_ok = bool(payload.get("frigate_ok", True))
    if not frigate_ok and not watch.frigate_down:
        watch.frigate_down = True
        await notify(session, f"⚠️ <b>{site}</b>: Frigate не отвечает — детекция стоит.")
    elif frigate_ok and watch.frigate_down:
        watch.frigate_down = False
        await notify(session, f"✅ <b>{site}</b>: Frigate снова работает.")

    # Камеры: сообщаем только о переходах, чтобы не сыпать одним и тем же.
    cameras = payload.get("cameras") or {}
    # `stable` — состояние, уже отфильтрованное домом от кратковременных провалов.
    # Откат на `online` оставлен для домов со старой версией guard.
    now_down = {
        name
        for name, info in cameras.items()
        if not (info or {}).get("stable", (info or {}).get("online"))
    }
    for name in sorted(now_down - watch.cameras_down):
        await notify(session, f"⚠️ <b>{site}</b>: камера <code>{name}</code> не отдаёт поток.")
    for name in sorted(watch.cameras_down - now_down):
        if name in cameras:
            await notify(session, f"✅ <b>{site}</b>: камера <code>{name}</code> снова на связи.")
    watch.cameras_down = now_down

    watch.save()
    return web.json_response({"ok": True})


async def handle_health(request: web.Request) -> web.Response:
    watch: Watch = request.app["watch"]
    age = time.time() - watch.last_seen if watch.last_seen else None
    return web.json_response(
        {
            "ok": not watch.site_down,
            "last_seen": watch.last_seen or None,
            "age_seconds": round(age) if age is not None else None,
            "armed": watch.last_payload.get("armed"),
            "cameras_down": sorted(watch.cameras_down),
        }
    )


async def staleness_loop(app: web.Application) -> None:
    watch: Watch = app["watch"]
    session: ClientSession = app["session"]
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        # До первого отчёта тревожить не о чем: сторож мог подняться раньше дома.
        if not watch.last_seen or watch.site_down:
            continue
        gap = time.time() - watch.last_seen
        if gap > STALE_SECONDS:
            watch.site_down = True
            watch.save()
            site = str(watch.last_payload.get("site") or "home")
            armed = watch.last_payload.get("armed")
            mode = "под охраной" if armed else "снят с охраны"
            await notify(
                session,
                f"🛑 <b>{site}</b>: сервер не выходит на связь {_fmt_gap(gap)}.\n"
                f"Последний известный режим — {mode}.\n"
                "Возможные причины: пропал свет, интернет или упал сервер.",
            )


async def on_startup(app: web.Application) -> None:
    app["session"] = ClientSession()
    app["task"] = asyncio.create_task(staleness_loop(app))


async def on_cleanup(app: web.Application) -> None:
    app["task"].cancel()
    await asyncio.gather(app["task"], return_exceptions=True)
    await app["session"].close()


def build_app() -> web.Application:
    app = web.Application()
    app["watch"] = Watch()
    app.router.add_post("/heartbeat", handle_heartbeat)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not AUTH_TOKEN:
        log.warning("AUTH_TOKEN пуст — эндпоинт открыт всем, кто знает адрес")
    web.run_app(build_app(), port=PORT)
