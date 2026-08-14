from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from . import events, heartbeat
from .admin import CameraAdmin
from .bot import Notifier, register_handlers
from .config import ConfigError, load_config
from .frigate import FrigateClient
from .guard import Guard
from .state import ArmState

log = logging.getLogger("app")


async def run() -> None:
    config = load_config(Path(os.environ.get("CONFIG_PATH", "config.yaml")))
    state = ArmState(config.state_path)

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    frigate = FrigateClient(config.frigate)
    notifier = Notifier(bot, config.allowed_chat_ids)
    guard = Guard(config, state, notifier, frigate)
    admin = CameraAdmin(frigate, config.camera_defaults)

    register_handlers(dp, config, state, frigate, admin, guard)

    tasks = [
        asyncio.create_task(
            events.subscribe(config.mqtt, guard.on_event, guard.on_frigate_availability),
            name="mqtt",
        ),
        asyncio.create_task(guard.monitor(), name="monitor"),
    ]
    if config.heartbeat.enabled:
        tasks.append(
            asyncio.create_task(heartbeat.run(config.heartbeat, state, guard), name="heartbeat")
        )
        log.info("heartbeat включён: %s каждые %dс", config.heartbeat.url, config.heartbeat.interval_seconds)

    mode = "🔴 под охраной" if state.armed else "🟢 снята"
    await notifier.text(f"✅ Сервис охраны запущен. Текущий режим: {mode}")
    log.info("запущен, режим: %s", "ON" if state.armed else "OFF")

    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await guard.shutdown()
        await frigate.aclose()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except ConfigError as exc:
        raise SystemExit(f"Ошибка конфигурации: {exc}") from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
