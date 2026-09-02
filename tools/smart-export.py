#!/usr/bin/env python3
"""Складывает сводку SMART в файл, который читает бот.

Запускается на хосте от root по расписанию. Смысл в том, чтобы не выдавать
контейнеру прямой доступ к дисковым устройствам: он лишь читает готовый JSON
из каталога, который и так примонтирован.

Установка (раз в 15 минут):

    sudo crontab -l 2>/dev/null | grep -v smart-export > /tmp/cron
    echo '*/15 * * * * /usr/bin/python3 /home/developer/docker/home-security/tools/smart-export.py' >> /tmp/cron
    sudo crontab /tmp/cron

Отдельный PATH в crontab не нужен: скрипт сам находит smartctl в /usr/sbin.

Путь вывода задаётся переменной SMART_OUT, по умолчанию — data/smart.json
рядом с проектом.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
OUT = Path(os.environ.get("SMART_OUT", PROJECT / "data" / "smart.json"))

# smartctl живёт в /usr/sbin, а cron работает с PATH=/usr/bin:/bin — искать
# программу по имени значит каждые 15 минут получать «файл не найден».
# Ошибка тихая: файл при этом пишется, просто с ошибкой вместо данных.
SMARTCTL = (shutil.which("smartctl")
            or next((p for p in ("/usr/sbin/smartctl", "/sbin/smartctl",
                                 "/usr/local/sbin/smartctl") if os.path.exists(p)),
                    "smartctl"))

# Атрибуты ATA, по которым видно деградацию поверхности.
ATTRS = {
    5: "reallocated",
    197: "pending",
    198: "uncorrectable",
    9: "hours",
}


def devices() -> list[str]:
    """Физические диски: разделы и виртуальные устройства не интересны."""
    try:
        out = subprocess.run(
            ["lsblk", "-dn", "-o", "NAME,TYPE"], capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "disk":
            found.append(parts[0])
    return found


def probe(name: str) -> dict | None:
    try:
        result = subprocess.run(
            [SMARTCTL, "-H", "-A", "-i", "--json", f"/dev/{name}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}

    # smartctl возвращает ненулевой код и при живом диске (биты состояния),
    # поэтому ориентируемся на разбор вывода, а не на код возврата.
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return {"error": (result.stderr or "нечитаемый ответ smartctl").strip()[:200]}
    if not data:
        return None

    disk: dict = {
        "model": data.get("model_name"),
        "passed": (data.get("smart_status") or {}).get("passed"),
    }
    temp = (data.get("temperature") or {}).get("current")
    if temp is not None:
        disk["temp"] = temp

    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        disk["hours"] = nvme.get("power_on_hours")
        disk["media_errors"] = nvme.get("media_errors")
        # У NVMe износ выражен процентом исчерпания ресурса записи.
        disk["used_pct"] = nvme.get("percentage_used")
    else:
        for row in ((data.get("ata_smart_attributes") or {}).get("table") or []):
            key = ATTRS.get(row.get("id"))
            if key:
                disk[key] = (row.get("raw") or {}).get("value")

    return disk


def main() -> int:
    disks = {}
    for name in devices():
        info = probe(name)
        if info:
            disks[name] = info

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"updated": time.time(), "disks": disks}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, OUT)
    # Файл читает контейнер от другого пользователя.
    os.chmod(OUT, 0o644)
    print(f"записано дисков: {len(disks)} -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
