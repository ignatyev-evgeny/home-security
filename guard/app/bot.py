from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

from .admin import CameraAdmin
from .cameras import CameraEditError
from .config import Config
from .frigate import FrigateClient, FrigateError
from .state import ArmState

log = logging.getLogger(__name__)

HELP = (
    "<b>Команды</b>\n"
    "/status — режим охраны\n"
    "/cams — список камер и их состояние\n"
    "/photo — текущий кадр со всех камер\n"
    "/addcam <code>имя ip</code> — добавить камеру\n"
    "/delcam <code>имя</code> — удалить камеру\n"
    "/arm, /disarm — то же, что кнопки"
)


def build_keyboard(armed: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Под охраной ✅" if armed else "🔒 Поставить на охрану",
                    callback_data="arm",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔓 Снять с охраны" if armed else "🔓 Снято ✅",
                    callback_data="disarm",
                )
            ],
            [
                InlineKeyboardButton(text="📸 Снимки", callback_data="photo"),
                InlineKeyboardButton(text="📋 Камеры", callback_data="cams"),
            ],
        ]
    )


def fmt_time(ts: float, timezone: str) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, ZoneInfo(timezone)).strftime("%d.%m %H:%M")


def now_time(timezone: str) -> str:
    return datetime.now(ZoneInfo(timezone)).strftime("%d.%m %H:%M:%S")


def status_text(config: Config, state: ArmState, health: dict[str, dict], storage: dict | None = None) -> str:
    head = "🔴 <b>Дом под охраной</b>" if state.armed else "🟢 <b>Охрана снята</b>"
    online = sum(1 for info in health.values() if info["online"])
    total = len(health)
    cams = f"{online}/{total} на связи" if total else "нет данных о камерах"
    lines = [
        head,
        f"Изменено: {fmt_time(state.changed_at, config.timezone)} ({html.escape(state.changed_by)})",
        f"Камеры: {cams} · пауза между алертами: {config.alerts.cooldown_seconds} с",
    ]
    free = (storage or {}).get("free_gb")
    if free is not None:
        mark = "⚠️" if config.alerts.min_free_gb and free < config.alerts.min_free_gb else "💾"
        lines.append(f"{mark} Свободно под записи: {free} ГБ из {storage.get('total_gb', '?')} ГБ")
    return "\n".join(lines)


class Notifier:
    """Рассылка в разрешённые чаты; падение одного чата не роняет остальные."""

    def __init__(self, bot: Bot, chat_ids: Sequence[int]) -> None:
        self._bot = bot
        self._chat_ids = tuple(chat_ids)

    async def text(self, message: str) -> None:
        for chat_id in self._chat_ids:
            try:
                await self._bot.send_message(chat_id, message)
            except Exception as exc:  # noqa: BLE001
                log.warning("не отправить текст в %s: %s", chat_id, exc)

    async def photo(self, data: bytes, caption: str, filename: str = "snapshot.jpg") -> str | None:
        return await self._send("фото", self._bot.send_photo, data, filename, caption)

    async def video(self, data: bytes, caption: str, filename: str = "clip.mp4") -> str | None:
        return await self._send("клип", self._bot.send_video, data, filename, caption)

    async def _send(self, what: str, method, data: bytes, filename: str, caption: str) -> str | None:
        """Возвращает причину, если не ушло ни в один чат.

        Вызывающий код сообщает о ней текстом: молчаливо пропавшее видео
        неотличимо от «ничего не происходило», а это разные вещи.
        """
        errors: list[str] = []
        for chat_id in self._chat_ids:
            try:
                await method(chat_id, BufferedInputFile(data, filename), caption=caption)
            except Exception as exc:  # noqa: BLE001
                log.warning("не отправить %s в %s: %s", what, chat_id, exc)
                errors.append(str(exc))
        if errors and len(errors) == len(self._chat_ids):
            return errors[0]
        return None


def register_handlers(
    dp: Dispatcher,
    config: Config,
    state: ArmState,
    frigate: FrigateClient,
    admin: CameraAdmin,
    guard,
) -> None:
    allowed = set(config.allowed_chat_ids)

    async def _camera_names() -> list[str]:
        if guard.camera_health:
            return sorted(guard.camera_health)
        try:
            return await frigate.camera_names()
        except Exception as exc:  # noqa: BLE001
            log.warning("не получить список камер: %s", exc)
            return []

    async def _panel(message: Message) -> None:
        await message.answer(
            status_text(config, state, guard.camera_health, guard.storage),
            reply_markup=build_keyboard(state.armed),
        )

    async def _send_all_frames(message: Message) -> None:
        names = await _camera_names()
        if not names:
            await message.answer("Камер нет или Frigate недоступен.")
            return

        await message.bot.send_chat_action(message.chat.id, "upload_photo")
        results = await asyncio.gather(
            *(
                frigate.latest_jpeg(
                    name, config.alerts.snapshot_height, config.alerts.snapshot_quality
                )
                for name in names
            ),
            return_exceptions=True,
        )

        media: list[InputMediaPhoto] = []
        failed: list[str] = []
        for name, result in zip(names, results):
            if isinstance(result, BaseException) or not result:
                failed.append(name)
                continue
            media.append(
                InputMediaPhoto(
                    media=BufferedInputFile(result, f"{name}.jpg"),
                    caption=f"{name} · {now_time(config.timezone)}",
                )
            )

        if len(media) == 1:
            await message.answer_photo(media[0].media, caption=media[0].caption)
        elif media:
            # Telegram принимает медиагруппы по 10 элементов за раз.
            for chunk in (media[i : i + 10] for i in range(0, len(media), 10)):
                await message.answer_media_group(chunk)

        if failed:
            await message.answer("⚠️ Не ответили: " + ", ".join(f"<code>{html.escape(n)}</code>" for n in failed))

    # --- команды ------------------------------------------------------------

    @dp.message(CommandStart())
    async def _start(message: Message) -> None:
        if message.chat.id not in allowed:
            # Свой же chat_id — единственное, что нужно человеку для настройки whitelist.
            await message.answer(
                "Доступ запрещён.\n"
                f"Твой chat_id: <code>{message.chat.id}</code> — добавь его в "
                "<code>telegram.allowed_chat_ids</code> и перезапусти сервис."
            )
            return
        await _panel(message)
        await message.answer(HELP)

    @dp.message(Command("help"))
    async def _help(message: Message) -> None:
        if message.chat.id in allowed:
            await message.answer(HELP)

    @dp.message(Command("status"))
    async def _status(message: Message) -> None:
        if message.chat.id in allowed:
            await _panel(message)

    @dp.message(Command("arm"))
    async def _arm(message: Message) -> None:
        if message.chat.id not in allowed:
            return
        await state.set_armed(True, message.from_user.full_name if message.from_user else "—")
        await _panel(message)

    @dp.message(Command("disarm"))
    async def _disarm(message: Message) -> None:
        if message.chat.id not in allowed:
            return
        await state.set_armed(False, message.from_user.full_name if message.from_user else "—")
        await _panel(message)

    @dp.message(Command("photo"))
    async def _photo(message: Message) -> None:
        if message.chat.id in allowed:
            await _send_all_frames(message)

    @dp.message(Command("cams"))
    async def _cams(message: Message) -> None:
        if message.chat.id not in allowed:
            return
        try:
            names = await admin.names()
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"⚠️ Не получить конфиг Frigate: {html.escape(str(exc))}")
            return
        await message.answer(admin.status_text(names, guard.camera_health, guard.frigate_ok, guard.storage))

    @dp.message(Command("addcam"))
    async def _addcam(message: Message, command: CommandObject) -> None:
        if message.chat.id not in allowed:
            return
        parts = (command.args or "").split()
        if len(parts) != 2:
            await message.answer(
                "Формат: <code>/addcam имя ip</code>\n"
                "Например: <code>/addcam prihozhaya 192.168.1.114</code>\n"
                "Имя — латиница в нижнем регистре, цифры и <code>_</code>."
            )
            return
        name, host = parts
        await message.answer(f"Проверяю {html.escape(host)}…")
        try:
            await message.answer(await admin.add(name, host))
        except (CameraEditError, FrigateError) as exc:
            await message.answer(f"❌ {html.escape(str(exc))}")
        except Exception as exc:  # noqa: BLE001
            log.exception("addcam упал")
            await message.answer(f"❌ Неожиданная ошибка: {html.escape(str(exc))}")

    @dp.message(Command("delcam"))
    async def _delcam(message: Message, command: CommandObject) -> None:
        if message.chat.id not in allowed:
            return
        name = (command.args or "").strip()
        if not name:
            await message.answer("Формат: <code>/delcam имя</code>")
            return
        # Удаление перезапускает Frigate, поэтому спрашиваем подтверждение.
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delcam_ok:{name}"),
                    InlineKeyboardButton(text="Отмена", callback_data="delcam_no"),
                ]
            ]
        )
        await message.answer(
            f"Удалить камеру <b>{html.escape(name)}</b> из Frigate? Он будет перезапущен.",
            reply_markup=keyboard,
        )

    # --- кнопки -------------------------------------------------------------

    @dp.callback_query(F.data.in_({"arm", "disarm"}))
    async def _toggle(callback: CallbackQuery) -> None:
        message = callback.message
        if message is None or message.chat.id not in allowed:
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        armed = callback.data == "arm"
        if armed == state.armed:
            await callback.answer("Уже в этом режиме")
            return
        who = callback.from_user.full_name or str(callback.from_user.id)
        await state.set_armed(armed, who)
        log.info("режим охраны: %s (%s)", "ON" if armed else "OFF", who)
        await callback.answer("Дом под охраной" if armed else "Охрана снята")
        try:
            await message.edit_text(
                status_text(config, state, guard.camera_health, guard.storage),
                reply_markup=build_keyboard(armed),
            )
        except TelegramBadRequest as exc:
            log.debug("сообщение не обновлено: %s", exc)

    @dp.callback_query(F.data == "photo")
    async def _photo_button(callback: CallbackQuery) -> None:
        message = callback.message
        if message is None or message.chat.id not in allowed:
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer("Собираю снимки…")
        await _send_all_frames(message)

    @dp.callback_query(F.data == "cams")
    async def _cams_button(callback: CallbackQuery) -> None:
        message = callback.message
        if message is None or message.chat.id not in allowed:
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer()
        try:
            names = await admin.names()
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"⚠️ Не получить конфиг Frigate: {html.escape(str(exc))}")
            return
        await message.answer(admin.status_text(names, guard.camera_health, guard.frigate_ok, guard.storage))

    @dp.callback_query(F.data == "delcam_no")
    async def _delcam_cancel(callback: CallbackQuery) -> None:
        await callback.answer("Отменено")
        if callback.message:
            try:
                await callback.message.edit_text("Удаление отменено.")
            except TelegramBadRequest:
                pass

    @dp.callback_query(F.data.startswith("delcam_ok:"))
    async def _delcam_confirm(callback: CallbackQuery) -> None:
        message = callback.message
        if message is None or message.chat.id not in allowed:
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        name = callback.data.split(":", 1)[1]
        await callback.answer("Удаляю…")
        try:
            result = await admin.remove(name)
        except (CameraEditError, FrigateError) as exc:
            result = f"❌ {html.escape(str(exc))}"
        except Exception as exc:  # noqa: BLE001
            log.exception("delcam упал")
            result = f"❌ Неожиданная ошибка: {html.escape(str(exc))}"
        try:
            await message.edit_text(result)
        except TelegramBadRequest:
            await message.answer(result)
