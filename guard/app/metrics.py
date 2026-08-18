from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Замер раз в минуту: за 30 дней это ~43 тысячи строк, единицы мегабайт.
# Чаще нет смысла — температуры и нагрузка так быстро не меняются.
SAMPLE_INTERVAL = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts          INTEGER PRIMARY KEY,
    cpu_temp    REAL,
    disk_temp   REAL,
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

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=10)
        # WAL держит чтение страницы независимым от записи замеров.
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _write(self, row: dict) -> None:
        cutoff = int(time.time() - self._retention * 86400)
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO samples "
                "(ts, cpu_temp, disk_temp, load1, mem_pct, free_gb, inference, cameras_ok, armed) "
                "VALUES (:ts, :cpu_temp, :disk_temp, :load1, :mem_pct, :free_gb, :inference, "
                ":cameras_ok, :armed)",
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

    async def sample(self, snapshot: dict, guard) -> None:
        """Складывает один замер. Недоступные источники пишутся как NULL."""
        temps = snapshot.get("temps") or {}
        load = snapshot.get("load") or ()
        mem = snapshot.get("memory") or {}
        health = guard.camera_health or {}
        row = {
            "ts": int(time.time()),
            "cpu_temp": temps.get("CPU"),
            "disk_temp": temps.get("диск"),
            "load1": load[0] if load else None,
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
        """Пишет замеры, пока сервис жив."""
        while True:
            await self.sample(snapshot_fn(), guard)
            await asyncio.sleep(SAMPLE_INTERVAL)
