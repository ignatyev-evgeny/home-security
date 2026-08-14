from __future__ import annotations

import asyncio
import html
import logging
import time

from .bot import Notifier, now_time
from .config import Config
from .frigate import FrigateClient

log = logging.getLogger(__name__)

MONITOR_INTERVAL = 30.0


def cameras_from_stats(stats: dict) -> dict[str, dict]:
    """Состояние камер из /api/stats.

    camera_fps == 0 означает, что Frigate не получает кадры: камера недоступна
    или отдаёт битый поток.
    """
    result: dict[str, dict] = {}
    for name, data in (stats.get("cameras") or {}).items():
        if not isinstance(data, dict):
            continue
        camera_fps = float(data.get("camera_fps") or 0.0)
        result[name] = {
            "online": camera_fps > 0.0,
            "camera_fps": round(camera_fps, 1),
            "detection_fps": round(float(data.get("detection_fps") or 0.0), 1),
        }
    return result


class Guard:
    """События Frigate → Telegram, плюс слежение за живостью камер."""

    def __init__(self, config: Config, state, notifier: Notifier, frigate: FrigateClient) -> None:
        self._config = config
        self._state = state
        self._notifier = notifier
        self._frigate = frigate

        self.camera_health: dict[str, dict] = {}
        self.frigate_ok = False

        self._last_alert: dict[str, float] = {}
        self._pending_clips: dict[str, str] = {}
        self._offline_reported: set[str] = set()
        self._frigate_reported_down = False
        self._background: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # --- события детекции ---------------------------------------------------

    async def on_event(self, payload: dict) -> None:
        after = payload.get("after") or {}
        camera = after.get("camera")
        event_id = after.get("id")
        label = after.get("label")
        if not camera or not event_id:
            return
        if label not in self._config.alerts.labels:
            return
        if after.get("false_positive"):
            return

        event_type = payload.get("type")
        if event_type == "new":
            await self._on_new(camera, event_id, label, after)
        elif event_type == "end":
            pending = self._pending_clips.pop(event_id, None)
            if pending:
                self._spawn(self._send_clip(pending, event_id))

    async def _on_new(self, camera: str, event_id: str, label: str, after: dict) -> None:
        if not self._state.armed:
            return

        score = float(after.get("top_score") or after.get("score") or 0.0)
        if self._config.alerts.min_score and score < self._config.alerts.min_score:
            log.debug("[%s] %s score=%.2f ниже порога", camera, label, score)
            return

        now = time.monotonic()
        if now - self._last_alert.get(camera, 0.0) < self._config.alerts.cooldown_seconds:
            log.debug("[%s] событие в кулдауне — пропускаю", camera)
            return
        self._last_alert[camera] = now

        caption = (
            f"🚨 <b>{html.escape(camera)}</b> · {now_time(self._config.timezone)}\n"
            f"{html.escape(label)}"
            + (f" · {score:.0%}" if score else "")
        )
        try:
            # latest.jpg доступен сразу; снимок самого события Frigate дописывает позже.
            frame = await self._frigate.latest_jpeg(camera)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] не получить кадр: %s", camera, exc)
            await self._notifier.text(f"{caption}\n⚠️ кадр не получен: {html.escape(str(exc))}")
        else:
            await self._notifier.photo(frame, caption)

        if self._config.alerts.send_clip:
            self._pending_clips[event_id] = camera

    async def _send_clip(self, camera: str, event_id: str) -> None:
        clip = await self._frigate.event_clip(event_id)
        if clip is None:
            return
        await self._notifier.video(clip, f"🎥 {html.escape(camera)}")

    async def on_frigate_availability(self, online: bool) -> None:
        self.frigate_ok = online
        if online and self._frigate_reported_down:
            self._frigate_reported_down = False
            await self._notifier.text("✅ Frigate снова на связи.")
        elif not online and not self._frigate_reported_down:
            self._frigate_reported_down = True
            await self._notifier.text("⚠️ Frigate ушёл в offline — детекция не работает.")

    # --- живость камер ------------------------------------------------------

    async def monitor(self) -> None:
        """Опрашивает /api/stats и сообщает о переходах камер online/offline."""
        while True:
            try:
                stats = await self._frigate.stats()
            except Exception as exc:  # noqa: BLE001
                self.frigate_ok = False
                log.warning("не получить stats: %s", exc)
            else:
                self.frigate_ok = True
                health = cameras_from_stats(stats)
                self.camera_health = health
                if self._config.alerts.notify_offline:
                    await self._report_transitions(health)
            await asyncio.sleep(MONITOR_INTERVAL)

    async def _report_transitions(self, health: dict[str, dict]) -> None:
        for name, info in health.items():
            if not info["online"] and name not in self._offline_reported:
                self._offline_reported.add(name)
                await self._notifier.text(
                    f"⚠️ Камера <b>{html.escape(name)}</b> не отдаёт поток — движение с неё не отслеживается."
                )
            elif info["online"] and name in self._offline_reported:
                self._offline_reported.discard(name)
                await self._notifier.text(f"✅ Камера <b>{html.escape(name)}</b> снова на связи.")

        # Камеру могли удалить из конфига, пока она числилась упавшей.
        self._offline_reported &= set(health)

    async def shutdown(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
