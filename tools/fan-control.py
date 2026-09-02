#!/usr/bin/env python3
"""Ручной режим вентилятора с гарантированным возвратом к BIOS.

Зачем на хосте. Управление вентилятором идёт через SMM-вызовы, доступные
только root, а /sys в контейнере смонтирован только на чтение — и это
правильно: в контейнере лежат токен бота и пароли камер, и расширять ему
права ради регулировки оборотов был бы плохой размен. Поэтому бот лишь
кладёт заявку в общий каталог data/, а исполняет её этот скрипт.

Что выяснено опытом на OptiPlex 7050 (в документации этого нет):

    pwm1 = 0            -> BIOS забирает управление обратно, ~40 секунд
    cur_state = 1       -> ручной низкий, ~850 об/мин
    cur_state = 2       -> ручной высокий, ~1650 об/мин
    cur_state = 3       -> ОТКЛОНЯЕТСЯ, максимум 2

Ключевое здесь — первая строка. Штатного «верни управление» у драйвера нет:
запись 3 отклоняется, перезагрузка модуля не помогает, само по себе не
рассасывается (проверено — пять минут на повышенных оборотах). Возврат даёт
только запись нуля в pwm1, и на этом держится всё остальное: ручной режим
можно разрешить лишь потому, что выход из него — команда, а не надежда.

Два предохранителя, оба обязательны на машине, которая держит охрану:
ручной режим протухает по времени, и при перегреве управление возвращается
BIOS немедленно, не спрашивая.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
REQUEST = PROJECT / "data" / "fan.request"
STATE = PROJECT / "data" / "fan.state"

HWMON = Path("/sys/class/hwmon")
COOLING = Path("/sys/class/thermal")

MODES = ("auto", "low", "high")
LEVEL = {"low": 1, "high": 2}
# Ручной режим протухает: если про него забыли или бот умер, машина не должна
# остаться с фиксированными оборотами навсегда.
DEFAULT_TTL = 3600.0
MAX_TTL = 12 * 3600.0
# Выше этой температуры ручной режим снимается немедленно. Порог ниже
# тревожного (80 °C) намеренно: сперва пусть BIOS попробует справиться сам,
# и только если не справится — придёт тревога.
TEMP_GUARD = 70.0


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def hwmon() -> Path | None:
    """Каталог dell_smm. Номер hwmon* меняется между загрузками, ищем по имени."""
    try:
        for mon in sorted(HWMON.glob("hwmon*")):
            if _read(mon / "name") == "dell_smm":
                return mon
    except OSError:
        pass
    return None


def cooling() -> Path | None:
    try:
        for dev in sorted(COOLING.glob("cooling_device*")):
            if _read(dev / "type") == "dell-smm-fan1":
                return dev
    except OSError:
        pass
    return None


def measure(mon: Path | None) -> dict:
    if not mon:
        return {}
    out: dict = {}
    rpm = _read(mon / "fan1_input")
    if rpm and rpm.isdigit():
        out["rpm"] = int(rpm)
    raw = _read(mon / "temp1_input")
    try:
        out["temp"] = round(int(raw) / 1000, 1)
    except (TypeError, ValueError):
        pass
    return out


def apply(mode: str) -> str | None:
    """Ставит режим. Возвращает текст ошибки или None при успехе."""
    mon, dev = hwmon(), cooling()
    if not mon:
        return "датчик dell_smm не найден"
    if mode == "auto":
        try:
            (mon / "pwm1").write_text("0")
        except OSError as exc:
            return f"не записать pwm1: {exc}"
        return None
    if not dev:
        return "устройство dell-smm-fan1 не найдено"
    try:
        (dev / "cur_state").write_text(str(LEVEL[mode]))
    except OSError as exc:
        return f"не записать cur_state: {exc}"
    return None


def load_request() -> dict:
    try:
        data = json.loads(REQUEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(**kw) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(kw, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"не сохранить состояние: {exc}", file=sys.stderr)


def main() -> int:
    mon = hwmon()
    now = time.time()
    req = load_request()
    mode = req.get("mode")
    if mode not in MODES:
        mode = "auto"
    until = float(req.get("until") or 0)
    if mode != "auto" and not until:
        until = now + DEFAULT_TTL
    until = min(until, now + MAX_TTL)

    measured = measure(mon)
    temp = measured.get("temp")
    note = ""

    # --- предохранители, в порядке важности ---------------------------------
    if mode != "auto" and temp is not None and temp >= TEMP_GUARD:
        mode, note = "auto", f"перегрев {temp:.0f} °C — управление возвращено BIOS"
    elif mode != "auto" and now >= until:
        mode, note = "auto", "срок ручного режима истёк"

    if note:                       # заявка отменена — стираем, чтобы не повторялось
        try:
            REQUEST.unlink(missing_ok=True)
        except OSError:
            pass

    previous = {}
    try:
        previous = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass

    error = None
    # Пишем в железо только при смене режима: каждая запись — это SMM-вызов,
    # а он останавливает все ядра. Дёргать его раз в полминуты без нужды незачем.
    if previous.get("mode") != mode or previous.get("error"):
        error = apply(mode)
        if error:
            print(f"режим {mode} не применён: {error}", file=sys.stderr)
        else:
            print(f"режим вентилятора: {mode}" + (f" ({note})" if note else ""),
                  file=sys.stderr)
        measured = measure(hwmon())

    # note объясняет последний переход — почему режим стал таким, каким стал.
    save_state(mode=mode, until=(until if mode != "auto" else 0), ts=now,
               note=note, error=error, **measured)
    return 0


if __name__ == "__main__":
    sys.exit(main())
