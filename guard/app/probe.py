from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


async def check_dahua(host: str, username: str, password: str, timeout: float = 6.0) -> tuple[bool, str]:
    """Проверяет камеру до того, как она попадёт в конфиг Frigate.

    Иначе о неверном IP или пароле мы узнаем только по рестарту Frigate
    с уже сломанной конфигурацией.
    """
    url = f"http://{host}/cgi-bin/magicBox.cgi?action=getDeviceType"
    auth = httpx.DigestAuth(username, password)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, auth=auth)
    except httpx.HTTPError as exc:
        return False, f"нет связи с {host}: {exc}"

    if response.status_code in (401, 403):
        return False, f"{host}: неверный логин или пароль"
    if response.status_code >= 400:
        return False, f"{host}: HTTP {response.status_code}"

    _, _, model = response.text.strip().partition("=")
    return True, model or "неизвестная модель"


def camera_password() -> str:
    """Пароль камер для проверки при добавлении.

    В конфиг Frigate уезжает плейсхолдер {FRIGATE_RTSP_PASSWORD}, но проверить
    доступность камеры надо реальным паролем.
    """
    return os.environ.get("CAM_PASSWORD", "")
