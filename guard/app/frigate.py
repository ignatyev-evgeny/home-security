from __future__ import annotations

import asyncio
import logging

import httpx

from .config import FrigateSettings

log = logging.getLogger(__name__)


class FrigateError(RuntimeError):
    pass


class FrigateClient:
    """HTTP-клиент Frigate с автологином.

    Frigate 0.14+ включает аутентификацию по умолчанию и отдаёт JWT в cookie,
    поэтому на 401 переавторизуемся и повторяем запрос один раз.
    """

    def __init__(self, settings: FrigateSettings) -> None:
        self._s = settings
        self._client = httpx.AsyncClient(base_url=settings.url, timeout=20.0, follow_redirects=True)
        self._lock = asyncio.Lock()
        self._authed = False

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        async with self._lock:
            response = await self._client.post(
                "/api/login",
                json={"user": self._s.user, "password": self._s.password},
            )
            if response.status_code >= 400:
                raise FrigateError(
                    f"логин в Frigate не прошёл ({response.status_code}). "
                    "Проверь FRIGATE_PASSWORD — при первом старте Frigate печатает его в логах."
                )
            self._authed = True
            log.info("авторизовались во Frigate как %s", self._s.user)

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._authed and self._s.password:
            await self._login()
        response = await self._client.request(method, path, **kwargs)
        if response.status_code in (401, 403) and self._s.password:
            self._authed = False
            await self._login()
            response = await self._client.request(method, path, **kwargs)
        return response

    async def _bytes(self, path: str) -> bytes:
        response = await self.request("GET", path)
        response.raise_for_status()
        return response.content

    # --- данные ------------------------------------------------------------

    async def stats(self) -> dict:
        response = await self.request("GET", "/api/stats")
        response.raise_for_status()
        return response.json()

    async def config(self) -> dict:
        response = await self.request("GET", "/api/config")
        response.raise_for_status()
        return response.json()

    async def camera_names(self) -> list[str]:
        return sorted((await self.config()).get("cameras", {}))

    async def latest_jpeg(self, camera: str, height: int = 720) -> bytes:
        """Текущий кадр камеры. Доступен всегда, не привязан к событию."""
        return await self._bytes(f"/api/{camera}/latest.jpg?h={height}")

    async def event_clip(self, event_id: str, attempts: int = 4, delay: float = 5.0) -> bytes | None:
        """Клип события. После окончания события Frigate дописывает его не мгновенно."""
        for attempt in range(1, attempts + 1):
            try:
                return await self._bytes(f"/api/events/{event_id}/clip.mp4")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    log.warning("клип %s: %s", event_id, exc)
                    return None
            except httpx.HTTPError as exc:
                log.warning("клип %s: %s", event_id, exc)
                return None
            if attempt < attempts:
                await asyncio.sleep(delay)
        log.info("клип %s так и не появился", event_id)
        return None

    # --- конфиг ------------------------------------------------------------

    async def raw_config(self) -> str:
        response = await self.request("GET", "/api/config/raw")
        response.raise_for_status()
        return response.text

    async def save_config(self, raw_yaml: str, restart: bool = True) -> None:
        """Сохраняет конфиг через Frigate — он же его и валидирует.

        Правку через API предпочитаем прямой записи в файл: невалидный конфиг
        будет отвергнут с понятной ошибкой, а не уронит Frigate после рестарта.
        """
        option = "restart" if restart else "saveonly"
        response = await self.request(
            "POST",
            f"/api/config/save?save_option={option}",
            content=raw_yaml.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        if response.status_code >= 400:
            raise FrigateError(f"Frigate отверг конфиг: {response.text[:800]}")

    async def restart(self) -> None:
        await self.request("POST", "/api/restart")
