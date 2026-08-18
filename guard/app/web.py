from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from .metrics import Metrics

log = logging.getLogger(__name__)

# Страница лежит отдельным файлом: HTML с разметкой, стилями и скриптом внутри
# строкового литерала невозможно ни подсветить, ни проверить.
PAGE = (Path(__file__).with_name("page.html")).read_text(encoding="utf-8")


def build_app(metrics: Metrics) -> web.Application:
    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=PAGE, content_type="text/html")

    async def api(request: web.Request) -> web.Response:
        try:
            days = min(90.0, max(0.1, float(request.query.get("days", 1))))
        except ValueError:
            days = 1.0
        return web.json_response(await metrics.history(days))

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/metrics", api)
    return app


async def run(metrics: Metrics, port: int) -> None:
    """Поднимает страницу телеметрии.

    Слушает только внутри домашней сети — наружу порт не публикуется, как и
    у Frigate: телеметрия сервера не то, что стоит показывать интернету.
    """
    runner = web.AppRunner(build_app(metrics), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("страница телеметрии слушает порт %d", port)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
