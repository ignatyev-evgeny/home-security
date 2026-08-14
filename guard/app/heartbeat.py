from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import HeartbeatSettings
from .state import ArmState

log = logging.getLogger(__name__)


async def run(settings: HeartbeatSettings, state: ArmState, guard) -> None:
    """Раз в interval_seconds отправляет отчёт внешнему сторожу.

    Молчание этого цикла и есть сигнал тревоги на той стороне: так внешний
    сервис замечает пропажу света, интернета или всего сервера целиком —
    ситуации, о которых локальный бот сообщить уже не сможет.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            payload = {
                "site": settings.site,
                "ts": time.time(),
                "armed": state.armed,
                "frigate_ok": guard.frigate_ok,
                "cameras": guard.camera_health,
            }
            try:
                response = await client.post(
                    settings.url,
                    json=payload,
                    headers={"X-Auth-Token": settings.token},
                )
                if response.status_code >= 400:
                    log.warning("сторож ответил %s: %.200s", response.status_code, response.text)
            except httpx.HTTPError as exc:
                # Не страшно: сторож сам поднимет тревогу, если отчёты пропали.
                log.warning("не отправить heartbeat: %s", exc)

            await asyncio.sleep(settings.interval_seconds)
