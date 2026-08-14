"""Спрашивает у камеры по ONVIF её модель и точные RTSP-ссылки.

Нужен, когда камера не Dahua и /addcam её собрать не может: перебирать списки
известных форматов ссылок долго и ненадёжно, а камера знает ответ сама.

Запуск из каталога проекта (httpx уже есть в контейнере guard):

    CAM_PROBE_HOST=192.168.1.117 docker compose exec -T \\
        -e CAM_PROBE_HOST guard python - < tools/onvif_probe.py

Пароль берётся из CAM_PASSWORD контейнера, то есть из FRIGATE_RTSP_PASSWORD.
Если у камеры пароль свой, передай его через -e CAM_PROBE_PASSWORD.

В выводе пароль заменён на плейсхолдер Frigate — строку можно сразу вставлять
в frigate/config.yml. Оригинальные URI многие камеры отдают с паролем в
открытом виде, поэтому as-is их лучше никуда не пересылать.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import os
import re
import secrets
import sys

import httpx

HOST = os.environ.get("CAM_PROBE_HOST", "")
USER = os.environ.get("CAM_PROBE_USER", "admin")
PASSWORD = os.environ.get("CAM_PROBE_PASSWORD") or os.environ.get("CAM_PASSWORD", "")
PLACEHOLDER = "{FRIGATE_RTSP_PASSWORD}"
WSS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity"

if not HOST:
    sys.exit("укажи CAM_PROBE_HOST=<ip камеры>")


def security_header() -> str:
    """WS-Security UsernameToken с дайджестом — обычная схема авторизации ONVIF."""
    nonce = secrets.token_bytes(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + PASSWORD.encode()).digest()
    ).decode()
    return (
        f'<s:Header><Security s:mustUnderstand="1" xmlns="{WSS}-secext-1.0.xsd">'
        f"<UsernameToken><Username>{USER}</Username>"
        f'<Password Type="{WSS}-username-token-profile-1.0#PasswordDigest">{digest}</Password>'
        f'<Nonce EncodingType="{WSS}-soap-message-security-1.0#Base64Binary">'
        f"{base64.b64encode(nonce).decode()}</Nonce>"
        f'<Created xmlns="{WSS}-utility-1.0.xsd">{created}</Created>'
        "</UsernameToken></Security></s:Header>"
    )


def call(url: str, body: str, auth: bool = True) -> str:
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        + (security_header() if auth else "")
        + f"<s:Body>{body}</s:Body></s:Envelope>"
    )
    response = httpx.post(
        url,
        content=envelope.encode(),
        headers={"Content-Type": "application/soap+xml"},
        timeout=15,
    )
    return response.text


def mask(uri: str) -> str:
    """Прячет пароль, подставляя плейсхолдер Frigate.

    Форматов два: обычный user:pass@host и XiongMai, где логин с паролем
    зашиты в путь через `_` или `&`.
    """
    uri = re.sub(r"://([^:/@]+):[^@]*@", r"://\1:" + PLACEHOLDER + "@", uri)
    return re.sub(r"(password=)[^_&?/]*", r"\1" + PLACEHOLDER, uri)


def find(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


device = f"http://{HOST}:8899/onvif/device_service"
info = call(device, '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>')

if "Fault" in info or "NotAuthorized" in info:
    print("авторизация не прошла — у камеры другой логин или пароль")
    print(re.sub(r"<[^>]+>", " ", info)[:300].strip())
    sys.exit(1)

for tag in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber"):
    value = find(rf"<tds:{tag}>([^<]*)", info)
    if value:
        print(f"{tag:16} {value}")

caps = call(
    device,
    '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl">'
    "<Category>Media</Category></GetCapabilities>",
)
media = find(r"<tt:XAddr>(http[^<]*media[^<]*)</tt:XAddr>", caps) or device.replace(
    "device_service", "media_service"
)

profiles_xml = call(media, '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>')
resolutions = re.findall(r"<tt:Width>(\d+)</tt:Width><tt:Height>(\d+)</tt:Height>", profiles_xml)
if resolutions:
    print("разрешения:    ", ", ".join(f"{w}x{h}" for w, h in resolutions))

print("\nссылки на потоки (пароль заменён на плейсхолдер):")
for token in dict.fromkeys(re.findall(r'token="([^"]+)"', profiles_xml)):
    body = (
        '<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl"><StreamSetup>'
        '<Stream xmlns="http://www.onvif.org/ver10/schema">RTP-Unicast</Stream>'
        '<Transport xmlns="http://www.onvif.org/ver10/schema"><Protocol>RTSP</Protocol></Transport>'
        f"</StreamSetup><ProfileToken>{token}</ProfileToken></GetStreamUri>"
    )
    uri = find(r"<tt:Uri>([^<]+)</tt:Uri>", call(media, body))
    if uri:
        print(f"  {token:16} {mask(uri)}")
