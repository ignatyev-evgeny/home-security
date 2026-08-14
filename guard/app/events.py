from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import aiomqtt

from .config import MqttSettings

log = logging.getLogger(__name__)

_RECONNECT_MIN = 2.0
_RECONNECT_MAX = 60.0

EventCallback = Callable[[dict], Awaitable[None]]
AvailabilityCallback = Callable[[bool], Awaitable[None]]


async def subscribe(
    settings: MqttSettings,
    on_event: EventCallback,
    on_frigate_availability: AvailabilityCallback,
) -> None:
    """Слушает события Frigate по MQTT, переподключаясь с backoff.

    `frigate/events` — детекции объектов, `frigate/available` — сам Frigate
    (last will «offline», если он упал).
    """
    events_topic = f"{settings.topic_prefix}/events"
    available_topic = f"{settings.topic_prefix}/available"
    backoff = _RECONNECT_MIN

    while True:
        try:
            async with aiomqtt.Client(
                hostname=settings.host,
                port=settings.port,
                username=settings.username or None,
                password=settings.password or None,
                keepalive=30,
            ) as client:
                await client.subscribe(events_topic)
                await client.subscribe(available_topic)
                log.info("подписаны на %s и %s", events_topic, available_topic)
                backoff = _RECONNECT_MIN

                async for message in client.messages:
                    topic = str(message.topic)
                    payload = message.payload
                    if isinstance(payload, (bytes, bytearray)):
                        text = payload.decode("utf-8", errors="replace")
                    else:
                        text = str(payload)

                    if topic == available_topic:
                        await on_frigate_availability(text.strip().lower() == "online")
                        continue

                    try:
                        await on_event(json.loads(text))
                    except json.JSONDecodeError:
                        log.warning("нераспознанный payload в %s: %.120s", topic, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — любой сбой лечится переподключением
            log.warning("MQTT отвалился (%s), переподключение через %.0fс", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX)
