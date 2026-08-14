from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


class ArmState:
    """Режим охраны, переживающий перезапуск сервиса.

    Состояние пишется на диск атомарно: иначе перезагрузка бокса молча снимет
    дом с охраны, а узнать об этом можно будет только постфактум.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self.armed = False
        self.changed_at = 0.0
        self.changed_by = "—"
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("не читается %s (%s) — стартуем со снятой охраной", self._path, exc)
            return
        self.armed = bool(data.get("armed", False))
        self.changed_at = float(data.get("changed_at") or 0.0)
        self.changed_by = str(data.get("changed_by") or "—")

    async def set_armed(self, armed: bool, by: str) -> None:
        async with self._lock:
            self.armed = armed
            self.changed_at = time.time()
            self.changed_by = by
            await asyncio.to_thread(self._persist)

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        payload = {"armed": self.armed, "changed_at": self.changed_at, "changed_by": self.changed_by}
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)
