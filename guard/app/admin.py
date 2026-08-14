from __future__ import annotations

import html
import logging

from . import cameras as cam_edit
from .config import CameraDefaults
from .frigate import FrigateClient
from .probe import camera_password, check_dahua

log = logging.getLogger(__name__)


class CameraAdmin:
    """Добавление и удаление камер прямо в конфиге Frigate."""

    def __init__(self, frigate: FrigateClient, defaults: CameraDefaults) -> None:
        self._frigate = frigate
        self._defaults = defaults

    async def add(self, name: str, host: str) -> str:
        ok, detail = await check_dahua(host, self._defaults.username, camera_password())
        if not ok:
            return f"❌ {html.escape(detail)}\nКамера не добавлена."

        raw = await self._frigate.raw_config()
        updated = cam_edit.add_camera(raw, name, host, self._defaults)
        await self._frigate.save_config(updated, restart=True)
        return (
            f"✅ Камера <b>{html.escape(name)}</b> ({html.escape(host)}, {html.escape(detail)}) добавлена.\n"
            "Frigate перезапускается — на пару минут возможен разрыв потоков."
        )

    async def remove(self, name: str) -> str:
        raw = await self._frigate.raw_config()
        updated = cam_edit.remove_camera(raw, name)
        await self._frigate.save_config(updated, restart=True)
        return (
            f"🗑 Камера <b>{html.escape(name)}</b> удалена.\n"
            "Frigate перезапускается. Записи и снимки остаются на диске."
        )

    async def names(self) -> list[str]:
        return cam_edit.list_cameras(await self._frigate.raw_config())

    def status_text(self, names: list[str], health: dict[str, dict], frigate_ok: bool) -> str:
        if not names:
            return "Камер в конфиге нет."
        lines = ["<b>Камеры</b>"]
        for name in names:
            info = health.get(name)
            if info is None:
                mark, detail = "❔", "нет данных"
            elif info["online"]:
                mark = "🟢"
                detail = f"{info['camera_fps']} fps · детекция {info['detection_fps']} fps"
            else:
                mark, detail = "🔴", "нет потока"
            lines.append(f"{mark} <code>{html.escape(name)}</code> — {detail}")
        if not frigate_ok:
            lines.append("\n⚠️ Frigate не отвечает — данные могут быть устаревшими.")
        return "\n".join(lines)
