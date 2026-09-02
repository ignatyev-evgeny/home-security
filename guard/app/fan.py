from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Общий каталог с хостом: контейнер кладёт заявку, tools/fan-control.py её
# исполняет. Прямого доступа к железу у контейнера нет и быть не должно —
# /sys смонтирован только на чтение, а внутри лежат токен бота и пароли камер.
DATA = Path("data")
REQUEST = DATA / "fan.request"
STATE = DATA / "fan.state"

MODES = ("auto", "low", "high")
NAMES = {"auto": "авто (BIOS)", "low": "низкий", "high": "высокий"}
# Сколько держится ручной режим, если не сказано иначе. Дальше хост вернёт
# управление BIOS сам — на машине, которая держит охрану, ручной режим не
# должен уметь остаться навсегда.
DEFAULT_MINUTES = 60


def available() -> bool:
    """Настроен ли исполнитель на хосте.

    Отсутствие файла состояния — не ошибка: на машине без Dell SMM или без
    установленного юнита управление просто не предлагается.
    """
    return STATE.exists()


def state() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def request(mode: str, minutes: int = DEFAULT_MINUTES) -> bool:
    """Кладёт заявку. Исполнитель подхватит её по systemd .path, за секунды."""
    if mode not in MODES:
        return False
    body = {"mode": mode, "until": time.time() + max(1, minutes) * 60}
    try:
        REQUEST.parent.mkdir(parents=True, exist_ok=True)
        REQUEST.write_text(json.dumps(body), encoding="utf-8")
    except OSError as exc:
        log.warning("заявка на режим вентилятора не записана: %s", exc)
        return False
    return True


def describe(data: dict) -> str:
    """Человеческое описание текущего состояния."""
    if not data:
        return "Состояние вентилятора неизвестно — исполнитель на хосте не отвечает."
    mode = data.get("mode") or "auto"
    parts = [f"Режим: <b>{NAMES.get(mode, mode)}</b>"]
    if data.get("settling"):
        parts.append("обороты подстраиваются")
    elif data.get("rpm"):
        parts.append(f"{data['rpm']} об/мин")
    if data.get("temp") is not None:
        parts.append(f"процессор {data['temp']:.0f} °C")
    line = " · ".join(parts)

    extra = []
    until = float(data.get("until") or 0)
    if mode != "auto" and until:
        left = (until - time.time()) / 60
        if left > 0:
            extra.append(f"Вернётся в авто через {left:.0f} мин.")
    if data.get("note"):
        extra.append(str(data["note"]).capitalize() + ".")
    if data.get("error"):
        extra.append(f"⚠️ Ошибка: {data['error']}")
    return line + ("\n" + " ".join(extra) if extra else "")
