from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Frigate не принимает пробелы и дефисы в именах камер.
CAMERA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


class ConfigError(RuntimeError):
    """Конфиг нечитаем или неполон — падаем на старте, а не в момент тревоги."""


def _expand(raw: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        value = os.environ.get(name)
        if value is None:
            raise ConfigError(f"переменная окружения {name} не задана (см. .env.example)")
        return value

    return _VAR.sub(repl, raw)


@dataclass(frozen=True)
class FrigateSettings:
    url: str = "http://frigate:5000"
    user: str = "admin"
    password: str = ""
    # Адрес Frigate, доступный с телефона (через VPN). Нужен только для ссылок
    # на клипы, которые не влезли в лимит Telegram. Пусто — ссылок не будет.
    public_url: str = ""


@dataclass(frozen=True)
class MqttSettings:
    host: str = "mosquitto"
    port: int = 1883
    topic_prefix: str = "frigate"
    username: str = ""
    password: str = ""


@dataclass(frozen=True)
class AlertSettings:
    labels: tuple[str, ...] = ("person",)
    min_score: float = 0.0
    cooldown_seconds: int = 45
    send_clip: bool = True
    notify_offline: bool = True
    # Пока объект не покинул кадр, досылать свежий кадр раз в N секунд (0 — выключить).
    followup_seconds: int = 30
    # Предохранитель: событие может тянуться очень долго, если человек сел и замер.
    followup_max: int = 20
    # 0 — родное разрешение detect-потока. Больше него запрашивать бесполезно.
    snapshot_height: int = 0
    snapshot_quality: int = 90


@dataclass(frozen=True)
class CameraDefaults:
    """Параметры, с которыми /addcam собирает RTSP-ссылки для новой камеры."""

    username: str = "admin"
    # Плейсхолдер Frigate: пароль подставляется из окружения и не лежит в конфиге.
    password_placeholder: str = "{FRIGATE_RTSP_PASSWORD}"
    rtsp_port: int = 554
    record_path: str = "/cam/realmonitor?channel=1&subtype=0"
    detect_path: str = "/cam/realmonitor?channel=1&subtype=1"


@dataclass(frozen=True)
class HeartbeatSettings:
    enabled: bool = False
    url: str = ""
    token: str = ""
    interval_seconds: int = 60
    site: str = "home"


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_chat_ids: tuple[int, ...]
    state_path: Path
    timezone: str = "Europe/Moscow"
    frigate: FrigateSettings = field(default_factory=FrigateSettings)
    mqtt: MqttSettings = field(default_factory=MqttSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    camera_defaults: CameraDefaults = field(default_factory=CameraDefaults)
    heartbeat: HeartbeatSettings = field(default_factory=HeartbeatSettings)


def load_config(path: Path) -> Config:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"не читается конфиг {path}: {exc}") from exc

    data = yaml.safe_load(_expand(raw_text)) or {}

    telegram = data.get("telegram") or {}
    token = str(telegram.get("bot_token") or "").strip()
    if not token:
        raise ConfigError("telegram.bot_token пуст")

    chat_ids = tuple(int(c) for c in (telegram.get("allowed_chat_ids") or ()))
    if not chat_ids:
        # Без whitelist управлять охраной сможет любой, кто найдёт бота.
        raise ConfigError("telegram.allowed_chat_ids пуст — укажи хотя бы один chat_id")

    fr = data.get("frigate") or {}
    mq = data.get("mqtt") or {}
    al = data.get("alerts") or {}
    cd = data.get("camera_defaults") or {}
    hb = data.get("heartbeat") or {}

    heartbeat = HeartbeatSettings(
        enabled=bool(hb.get("enabled", False)),
        url=str(hb.get("url") or "").strip(),
        token=str(hb.get("token") or "").strip(),
        interval_seconds=int(hb.get("interval_seconds", 60)),
        site=str(hb.get("site") or "home"),
    )
    if heartbeat.enabled and not heartbeat.url:
        raise ConfigError("heartbeat.enabled=true, но heartbeat.url пуст")

    return Config(
        bot_token=token,
        allowed_chat_ids=chat_ids,
        state_path=Path(str(data.get("state_path") or "data/state.json")),
        timezone=str(data.get("timezone") or "Europe/Moscow"),
        frigate=FrigateSettings(
            url=str(fr.get("url") or "http://frigate:5000").rstrip("/"),
            user=str(fr.get("user") or "admin"),
            password=str(fr.get("password") or ""),
            public_url=str(fr.get("public_url") or "").rstrip("/"),
        ),
        mqtt=MqttSettings(
            host=str(mq.get("host") or "mosquitto"),
            port=int(mq.get("port", 1883)),
            topic_prefix=str(mq.get("topic_prefix") or "frigate"),
            username=str(mq.get("username") or ""),
            password=str(mq.get("password") or ""),
        ),
        alerts=AlertSettings(
            labels=tuple(str(x) for x in (al.get("labels") or ("person",))),
            min_score=float(al.get("min_score", 0.0)),
            cooldown_seconds=int(al.get("cooldown_seconds", 45)),
            send_clip=bool(al.get("send_clip", True)),
            notify_offline=bool(al.get("notify_offline", True)),
            followup_seconds=int(al.get("followup_seconds", 30)),
            followup_max=int(al.get("followup_max", 20)),
            snapshot_height=int(al.get("snapshot_height", 0)),
            snapshot_quality=int(al.get("snapshot_quality", 90)),
        ),
        camera_defaults=CameraDefaults(
            username=str(cd.get("username") or "admin"),
            password_placeholder=str(cd.get("password_placeholder") or "{FRIGATE_RTSP_PASSWORD}"),
            rtsp_port=int(cd.get("rtsp_port", 554)),
            record_path=str(cd.get("record_path") or "/cam/realmonitor?channel=1&subtype=0"),
            detect_path=str(cd.get("detect_path") or "/cam/realmonitor?channel=1&subtype=1"),
        ),
        heartbeat=heartbeat,
    )
