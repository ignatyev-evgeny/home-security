from __future__ import annotations

import asyncio
import html
import logging
import time

from .bot import Notifier, now_time
from .config import Config
from .frigate import FrigateClient, FrigateError

log = logging.getLogger(__name__)

MONITOR_INTERVAL = 30.0
# Сколько опросов подряд камера должна молчать, прежде чем это считается падением.
# Один нулевой замер ловится штатно при перезапуске ffmpeg внутри Frigate и
# падением не является — без этого порога каждый рестарт даёт ложную тревогу.
OFFLINE_STREAK = 3
# Потолок Telegram на файл, отправляемый ботом.
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024


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
        self._offline_streak: dict[str, int] = {}
        self._followups: dict[str, asyncio.Task] = {}
        self._frigate_reported_down = False
        self._background: set[asyncio.Task] = set()

    async def _frame(self, camera: str) -> bytes:
        alerts = self._config.alerts
        return await self._frigate.latest_jpeg(camera, alerts.snapshot_height, alerts.snapshot_quality)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

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
            self._stop_followup(event_id)
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
            frame = await self._frame(camera)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] не получить кадр: %s", camera, exc)
            await self._notifier.text(f"{caption}\n⚠️ кадр не получен: {html.escape(str(exc))}")
        else:
            await self._notifier.photo(frame, caption)

        if self._config.alerts.send_clip:
            self._pending_clips[event_id] = camera
        if self._config.alerts.followup_seconds > 0:
            self._start_followup(camera, event_id)

    # --- досылка кадров, пока объект в кадре ---------------------------------

    def _start_followup(self, camera: str, event_id: str) -> None:
        if event_id in self._followups:
            return
        task = self._spawn(self._followup(camera, event_id))
        self._followups[event_id] = task
        task.add_done_callback(lambda _, eid=event_id: self._followups.pop(eid, None))

    def _stop_followup(self, event_id: str) -> None:
        task = self._followups.pop(event_id, None)
        if task:
            task.cancel()

    async def _followup(self, camera: str, event_id: str) -> None:
        """Шлёт свежий кадр, пока событие не закончилось.

        Клип Frigate отдаёт только после ухода объекта из кадра, поэтому при
        долгом событии он приходит с большой задержкой. Досылка закрывает эту
        дыру: пока человек в комнате, видно, что там происходит прямо сейчас.
        """
        alerts = self._config.alerts
        for shot in range(1, alerts.followup_max + 1):
            await asyncio.sleep(alerts.followup_seconds)
            if not self._state.armed:
                return
            try:
                frame = await self._frame(camera)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] досылка прервана: %s", camera, exc)
                await self._notifier.text(
                    f"⚠️ <b>{html.escape(camera)}</b>: досылка кадров прервана — {html.escape(str(exc))}"
                )
                return
            await self._notifier.photo(
                frame,
                f"👁 <b>{html.escape(camera)}</b> · всё ещё в кадре"
                f" · {now_time(self._config.timezone)}",
            )
            if shot == alerts.followup_max:
                log.info("[%s] событие %s: предел досылок исчерпан", camera, event_id)
                await self._notifier.text(
                    f"ℹ️ <b>{html.escape(camera)}</b>: движение продолжается, "
                    "но досылка кадров остановлена. Смотри живой поток во Frigate."
                )

    async def _send_clip(self, camera: str, event_id: str) -> None:
        name = html.escape(camera)
        try:
            clip = await self._frigate.event_clip(event_id)
        except FrigateError as exc:
            await self._notifier.text(f"⚠️ <b>{name}</b>: клип не получен — {html.escape(str(exc))}.")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("клип %s", event_id)
            await self._notifier.text(f"⚠️ <b>{name}</b>: клип не получен — {html.escape(str(exc))}.")
            return

        size_mb = len(clip) / 1024 / 1024
        if len(clip) > TELEGRAM_FILE_LIMIT:
            # Отправлять бессмысленно: Telegram отклонит, а причина потеряется в логах.
            await self._notifier.text(
                f"⚠️ <b>{name}</b>: клип {size_mb:.0f} МБ — больше лимита Telegram "
                f"({TELEGRAM_FILE_LIMIT // 1024 // 1024} МБ для ботов). "
                "Событие оказалось длинным; смотри запись во Frigate."
            )
            return

        error = await self._notifier.video(clip, f"🎥 {name} · {size_mb:.1f} МБ")
        if error:
            await self._notifier.text(
                f"⚠️ <b>{name}</b>: клип {size_mb:.1f} МБ не ушёл в Telegram — {html.escape(error)}"
            )

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
                self._apply_streaks(health)
                self.camera_health = health
                if self._config.alerts.notify_offline:
                    await self._report_transitions(health)
            await asyncio.sleep(MONITOR_INTERVAL)

    def _apply_streaks(self, health: dict[str, dict]) -> None:
        """Добавляет каждой камере `stable` — подтверждённое состояние связи.

        `online` остаётся мгновенным (его показывает /cams), `stable` гасит
        мигание: именно по нему поднимается тревога и отчитывается heartbeat.
        """
        for name, info in health.items():
            if info["online"]:
                self._offline_streak[name] = 0
            else:
                self._offline_streak[name] = self._offline_streak.get(name, 0) + 1
            info["stable"] = self._offline_streak[name] < OFFLINE_STREAK

        # Камеру могли удалить из конфига, пока она числилась упавшей.
        known = set(health)
        self._offline_reported &= known
        self._offline_streak = {k: v for k, v in self._offline_streak.items() if k in known}

    async def _report_transitions(self, health: dict[str, dict]) -> None:
        for name, info in health.items():
            if not info["stable"] and name not in self._offline_reported:
                self._offline_reported.add(name)
                await self._notifier.text(
                    f"⚠️ Камера <b>{html.escape(name)}</b> не отдаёт поток — движение с неё не отслеживается."
                )
            elif info["stable"] and name in self._offline_reported:
                self._offline_reported.discard(name)
                await self._notifier.text(f"✅ Камера <b>{html.escape(name)}</b> снова на связи.")

    async def shutdown(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
