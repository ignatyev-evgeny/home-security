from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

import httpx

from .frigate import FrigateClient

log = logging.getLogger(__name__)

# Секция конфигурации Dahua, отвечающая за встроенную подсветку.
_ENABLE = re.compile(r"FlashLight\.Enable=(\S+)")
_BRIGHTNESS = re.compile(r"FlashLight\.Brightness=(\S+)")


class Lighting:
    """Управление подсветкой камер Dahua через их собственный API.

    Адреса берём из конфига Frigate, а не из отдельного списка: так камера,
    добавленная через /addcam, сразу оказывается доступна и здесь.
    Поддержку подсветки определяем опросом — у камер других вендоров такой
    секции конфигурации нет, и они молча выпадают из списка.
    """

    def __init__(self, frigate: FrigateClient, username: str, password: str) -> None:
        self._frigate = frigate
        self._auth = httpx.BasicAuth(username, password)
        self._client = httpx.AsyncClient(timeout=12.0)
        self._hosts: dict[str, str] = {}
        self._supported: dict[str, bool] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def hosts(self, refresh: bool = False) -> dict[str, str]:
        """Имя камеры → её адрес, вытащенный из RTSP-ссылки в конфиге Frigate."""
        if self._hosts and not refresh:
            return self._hosts
        config = await self._frigate.config()
        hosts: dict[str, str] = {}
        for name, camera in (config.get("cameras") or {}).items():
            for entry in ((camera.get("ffmpeg") or {}).get("inputs") or []):
                host = urlparse(str(entry.get("path", ""))).hostname
                if host:
                    hosts[name] = host
                    break
        self._hosts = hosts
        return hosts

    async def _cgi(self, host: str, query: str) -> str | None:
        try:
            response = await self._client.get(
                f"http://{host}/cgi-bin/configManager.cgi?{query}", auth=self._auth
            )
        except httpx.HTTPError as exc:
            log.warning("подсветка %s: %s", host, exc)
            return None
        if response.status_code >= 400:
            log.warning("подсветка %s: HTTP %s", host, response.status_code)
            return None
        return response.text

    async def state(self, name: str) -> bool | None:
        """True — горит, False — выключена, None — камера не умеет или не ответила."""
        host = (await self.hosts()).get(name)
        if not host:
            return None
        text = await self._cgi(host, "action=getConfig&name=FlashLight")
        if text is None:
            # Связи нет — выводов о поддержке не делаем, попробуем в следующий раз.
            return None
        match = _ENABLE.search(text)
        # Камера ответила, но секции нет: подсветкой она не управляет.
        self._supported[name] = match is not None
        if not match:
            return None
        return match.group(1).strip().lower() == "true"

    async def states(self) -> dict[str, bool | None]:
        # Камеру, однажды ответившую «не умею», больше не дёргаем.
        names = [n for n in sorted(await self.hosts()) if self._supported.get(n, True)]
        results = await asyncio.gather(*(self.state(n) for n in names), return_exceptions=True)
        return {
            n: (None if isinstance(r, BaseException) else r)
            for n, r in zip(names, results)
        }

    async def set(self, name: str, on: bool) -> bool:
        host = (await self.hosts()).get(name)
        if not host:
            return False
        text = await self._cgi(
            host, f"action=setConfig&FlashLight.Enable={'true' if on else 'false'}"
        )
        return bool(text and "OK" in text)

    async def set_all(self, on: bool) -> dict[str, bool]:
        """Переключает все камеры, которые умеют подсветку."""
        states = await self.states()
        targets = [n for n, s in states.items() if s is not None]
        results = await asyncio.gather(*(self.set(n, on) for n in targets), return_exceptions=True)
        return {
            n: (False if isinstance(r, BaseException) else bool(r))
            for n, r in zip(targets, results)
        }

    async def brightness(self, name: str) -> int | None:
        host = (await self.hosts()).get(name)
        if not host:
            return None
        text = await self._cgi(host, "action=getConfig&name=FlashLight")
        match = _BRIGHTNESS.search(text or "")
        return int(match.group(1)) if match and match.group(1).isdigit() else None
