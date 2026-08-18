from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

# В базу пишем раз в минуту: за 30 дней это ~43 тысячи строк, единицы мегабайт.
SAMPLE_INTERVAL = 60.0
# А снимаем температуру часто. Пакет 35-ваттного процессора под рывковой
# нагрузкой гуляет на 10-12 °C за секунды, и одиночный замер раз в минуту —
# это случайное мгновение, по которому нельзя судить ни о нагреве, ни о
# результате замены термопасты. Поэтому за минуту копим и пишем min/avg/max.
PROBE_INTERVAL = 5.0

FIELDS = ("ts", "cpu_temp", "cpu_min", "cpu_max", "disk_temp", "fan_rpm", "load1",
          "mem_pct", "free_gb", "inference", "cameras_ok", "armed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts          INTEGER PRIMARY KEY,
    cpu_temp    REAL,   -- среднее за минуту
    cpu_min     REAL,
    cpu_max     REAL,
    disk_temp   REAL,
    fan_rpm     INTEGER,
    load1       REAL,
    mem_pct     REAL,
    free_gb     REAL,
    inference   REAL,
    cameras_ok  INTEGER,
    armed       INTEGER
);
"""


class Metrics:
    """История телеметрии в SQLite.

    Отдельная база, а не файл рядом с состоянием: замеры пишутся постоянно и
    их не жаль потерять, тогда как state.json должен переживать что угодно.
    """

    def __init__(self, path: Path, retention_days: int = 30) -> None:
        self._path = path
        self._retention = max(1, retention_days)
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)
            # База могла быть создана прежней версией без колонок разброса.
            have = {r[1] for r in db.execute("PRAGMA table_info(samples)")}
            for column in ("cpu_min", "cpu_max", "fan_rpm"):
                if column not in have:
                    db.execute(f"ALTER TABLE samples ADD COLUMN {column} REAL")
                    log.info("в историю добавлена колонка %s", column)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=10)
        # WAL держит чтение страницы независимым от записи замеров.
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _write(self, row: dict) -> None:
        # Недостающие поля — NULL, а не отказ записи: лучше сохранить то, что
        # есть, чем потерять весь замер из-за одного недоступного датчика.
        row = {k: row.get(k) for k in FIELDS}
        cutoff = int(time.time() - self._retention * 86400)
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO samples "
                "(ts, cpu_temp, cpu_min, cpu_max, disk_temp, fan_rpm, load1, mem_pct, "
                " free_gb, inference, cameras_ok, armed) "
                "VALUES (:ts, :cpu_temp, :cpu_min, :cpu_max, :disk_temp, :fan_rpm, :load1, "
                ":mem_pct, :free_gb, :inference, :cameras_ok, :armed)",
                row,
            )
            db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))

    def _read(self, since: float, limit: int) -> list[dict]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM samples WHERE ts >= ? ORDER BY ts", (int(since),)
            ).fetchall()
        if len(rows) <= limit:
            return [dict(r) for r in rows]
        # Прореживаем равномерно: браузеру незачем рисовать 43 тысячи точек.
        step = len(rows) / limit
        return [dict(rows[int(i * step)]) for i in range(limit)]

    async def sample(self, snapshot: dict, guard, cpu_samples: list[float] | None = None) -> None:
        """Складывает один замер за минуту.

        `cpu_samples` — накопленные за минуту показания пакета процессора.
        Пусто — берём мгновенное значение из снимка, но это заметно хуже.
        """
        temps = snapshot.get("temps") or {}
        load = snapshot.get("load") or ()
        mem = snapshot.get("memory") or {}
        health = guard.camera_health or {}
        cpu = list(cpu_samples or [])
        if not cpu and temps.get("CPU") is not None:
            cpu = [float(temps["CPU"])]
        row = {
            "ts": int(time.time()),
            "cpu_temp": round(sum(cpu) / len(cpu), 1) if cpu else None,
            "cpu_min": round(min(cpu), 1) if cpu else None,
            "cpu_max": round(max(cpu), 1) if cpu else None,
            "disk_temp": temps.get("диск"),
            # Берём самый быстрый вентилятор: на Dell их может быть несколько,
            # а интересует тот, что реагирует на нагрев процессора.
            "fan_rpm": max((snapshot.get("fans") or {}).values(), default=None),
            # os.getloadavg() отдаёт полную точность двоичной дроби;
            # на графике и карточке нужны два знака, а не 5.52197265625.
            "load1": round(float(load[0]), 2) if load else None,
            "mem_pct": mem.get("used_pct"),
            "free_gb": (guard.storage or {}).get("free_gb"),
            "inference": guard.inference_ms,
            "cameras_ok": sum(1 for v in health.values() if v.get("online")) or None,
            "armed": 1 if guard.armed else 0,
        }
        async with self._lock:
            try:
                await asyncio.to_thread(self._write, row)
            except sqlite3.Error as exc:
                log.warning("замер не записан: %s", exc)

    async def history(self, days: float = 30.0, limit: int = 1500) -> list[dict]:
        since = time.time() - days * 86400
        try:
            return await asyncio.to_thread(self._read, since, limit)
        except sqlite3.Error as exc:
            log.warning("история не прочитана: %s", exc)
            return []

    async def run(self, snapshot_fn, guard) -> None:
        """Часто опрашивает температуру, раз в минуту пишет сводку за неё."""
        cpu: list[float] = []
        elapsed = 0.0
        while True:
            snapshot = snapshot_fn()
            value = (snapshot.get("temps") or {}).get("CPU")
            if value is not None:
                cpu.append(float(value))
            if elapsed >= SAMPLE_INTERVAL:
                await self.sample(snapshot, guard, cpu)
                cpu, elapsed = [], 0.0
            await asyncio.sleep(PROBE_INTERVAL)
            elapsed += PROBE_INTERVAL
