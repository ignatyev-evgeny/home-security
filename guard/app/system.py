from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

THERMAL = Path("/sys/class/thermal")
HWMON = Path("/sys/class/hwmon")

# Датчиков в системе много, и большинство из них — корпус и чипсет. Интересны
# два: температура пакета процессора и накопителя, на котором лежат записи.
CPU_SENSORS = ("x86_pkg_temp", "coretemp", "cpu_thermal", "k10temp")
DISK_SENSORS = ("nvme", "drivetemp")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def load_average() -> tuple[float, float, float] | None:
    """Средняя нагрузка хоста.

    В контейнере и os.getloadavg(), и /proc/loadavg показывают значения
    хост-системы, так что отдельный доступ к хосту не нужен. Первый вариант
    работает и вне Linux, второй остаётся запасным.
    """
    try:
        one, five, fifteen = os.getloadavg()
        return one, five, fifteen
    except (OSError, AttributeError):
        pass
    raw = _read(Path("/proc/loadavg"))
    if not raw:
        return None
    parts = raw.split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return None


def memory() -> dict | None:
    raw = _read(Path("/proc/meminfo"))
    if not raw:
        return None
    values: dict[str, int] = {}
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        try:
            values[key] = int(rest.split()[0])  # килобайты
        except (IndexError, ValueError):
            continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return {
        "total_gb": round(total / 1024 / 1024, 1),
        "used_gb": round((total - available) / 1024 / 1024, 1),
        "used_pct": round((total - available) / total * 100),
    }


def _thermal_zones() -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    try:
        zones = sorted(THERMAL.glob("thermal_zone*"))
    except OSError:
        return found
    for zone in zones:
        kind = _read(zone / "type")
        raw = _read(zone / "temp")
        if not kind or not raw:
            continue
        try:
            found.append((kind, int(raw) / 1000))
        except ValueError:
            continue
    return found


def _hwmon_sensors() -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    try:
        mons = sorted(HWMON.glob("hwmon*"))
    except OSError:
        return found
    for mon in mons:
        name = _read(mon / "name")
        if not name:
            continue
        raw = _read(mon / "temp1_input")
        if not raw:
            continue
        try:
            found.append((name, int(raw) / 1000))
        except ValueError:
            continue
    return found


def temperatures() -> dict[str, float]:
    """Температуры процессора и накопителя, если датчики доступны."""
    sensors = _thermal_zones() + _hwmon_sensors()
    result: dict[str, float] = {}
    for name, value in sensors:
        low = name.lower()
        if "CPU" not in result and any(s in low for s in CPU_SENSORS):
            result["CPU"] = round(value, 1)
        elif "диск" not in result and any(s in low for s in DISK_SENSORS):
            result["диск"] = round(value, 1)
    return result


def snapshot() -> dict:
    """Сводка о хосте. Любой недоступный источник просто отсутствует в ответе."""
    data: dict = {"cpus": os.cpu_count() or 0}
    load = load_average()
    if load:
        data["load"] = load
    mem = memory()
    if mem:
        data["memory"] = mem
    temps = temperatures()
    if temps:
        data["temps"] = temps
    return data


def format_line(data: dict | None) -> str | None:
    """Одна строка для /status: нагрузка, память, температуры."""
    if not data:
        return None
    parts: list[str] = []
    load, cpus = data.get("load"), data.get("cpus")
    if load:
        # Нагрузка выше числа ядер означает очередь на процессор.
        mark = "⚠️" if cpus and load[0] > cpus else "🖥"
        parts.append(f"{mark} Нагрузка {load[0]:.1f}" + (f" из {cpus}" if cpus else ""))
    mem = data.get("memory")
    if mem:
        parts.append(f"память {mem['used_pct']}% из {mem['total_gb']} ГБ")
    temps = data.get("temps")
    if temps:
        parts.append(" · ".join(f"{k} {v:.0f} °C" for k, v in temps.items()))
    return " · ".join(parts) if parts else None
