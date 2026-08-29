#!/usr/bin/env python3
"""Перезагружает сервер, когда встроенное видеоядро зависло насмерть.

Зачем это вообще нужно. Аппаратное декодирование всех камер и детектор
объектов живут на одной встроенной графике. Когда она виснет, ядро пытается
сбросить видеодвижок раз в три секунды — и на Kaby Lake это иногда не удаётся
вообще никогда. 29.08.2026 так прошло пять часов: охраны всё это время не
было, а Frigate тёк по 6 ГБ в час, пока не съел всю память.

Помогает только перезагрузка, и ждать её от человека, который спит, — значит
оставить дом без присмотра до утра. Поэтому решение принимается здесь.

Запускается на хосте от root по таймеру systemd: нужен и журнал ядра, и само
право на перезагрузку — из контейнера ни того, ни другого не достать.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
STAMP = Path("/var/lib/gpu-watchdog.stamp")

# Одиночный срыв ядро обычно переживает: сбрасывает движок и работает дальше.
# Смертельный случай выглядит иначе — попытки сброса идут непрерывно, примерно
# двадцать в минуту. За три минуты это шестьдесят с лишним, так что порог в
# десять надёжно отделяет одно от другого.
WINDOW_MINUTES = 3
MIN_HANGS = 10
# Не перезагружаться сразу после загрузки: если графика виснет прямо на старте,
# машина уйдёт в вечный цикл. Лучше остаться со сломанным GPU и тревогой в боте.
MIN_UPTIME = 15 * 60
# И не чаще раза в сутки — по той же причине.
COOLDOWN = 24 * 3600


def uptime() -> float:
    return float(Path("/proc/uptime").read_text().split()[0])


def hang_count() -> int:
    """Сколько раз ядро жаловалось на зависание графики за последнее время."""
    try:
        out = subprocess.run(
            ["journalctl", "-k", "--since", f"{WINDOW_MINUTES} min ago", "--no-pager"],
            capture_output=True, text=True, timeout=60, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return out.count("GPU HANG")


def recently_rebooted() -> bool:
    try:
        return time.time() - float(STAMP.read_text().strip()) < COOLDOWN
    except (OSError, ValueError):
        return False


def telegram_targets() -> tuple[str, list[str]]:
    """Токен бота и получатели — из того же конфига, что и у самого бота.

    Дублировать секреты в двух местах — верный способ однажды поменять их
    только в одном.
    """
    token = ""
    env = PROJECT / ".env"
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("BOT_TOKEN="):
                token = line.partition("=")[2].strip().strip("'\"")
    except OSError:
        pass
    chats: list[str] = []
    try:
        text = (PROJECT / "guard" / "config.yaml").read_text(encoding="utf-8")
        block = re.search(r"allowed_chat_ids:(.*?)(?=\n\w|\n\s*\n)", text, re.S)
        if block:
            chats = re.findall(r"-?\d{5,}", block.group(1))
    except OSError:
        pass
    return token, chats


def notify(text: str) -> None:
    """Предупредить перед перезагрузкой. Не вышло — перезагружаемся молча:
    висящее видеоядро хуже, чем неотправленное сообщение."""
    token, chats = telegram_targets()
    if not token or not chats:
        return
    for chat in chats:
        data = urllib.parse.urlencode(
            {"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        try:
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15).read()
        except Exception as exc:  # noqa: BLE001
            print(f"не отправить в Telegram ({chat}): {exc}", file=sys.stderr)


def main() -> int:
    if uptime() < MIN_UPTIME:
        return 0
    hangs = hang_count()
    if hangs < MIN_HANGS:
        return 0
    if recently_rebooted():
        print(f"видеоядро висит ({hangs} срывов за {WINDOW_MINUTES} мин), "
              "но перезагрузка уже была за последние сутки — не повторяю", file=sys.stderr)
        return 0

    print(f"видеоядро висит: {hangs} срывов за {WINDOW_MINUTES} мин — перезагружаюсь",
          file=sys.stderr)
    notify("🔄 <b>Перезагружаю сервер.</b>\n"
           f"Видеоядро зависло намертво: {hangs} безуспешных сбросов за "
           f"{WINDOW_MINUTES} минуты. Камеры не отдают поток, и само это не чинится.\n"
           "Минуты через две всё поднимется само.")
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(str(time.time()))
    except OSError:
        pass
    time.sleep(5)                       # дать сообщению уйти
    subprocess.run(["systemctl", "reboot"], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
