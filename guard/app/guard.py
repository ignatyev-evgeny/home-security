from __future__ import annotations

import asyncio
import html
import logging
import time

from . import system
from .bot import Notifier, now_time
from .config import Config
from .frigate import FrigateClient, FrigateError

log = logging.getLogger(__name__)

MONITOR_INTERVAL = 30.0
# Сколько опросов подряд камера должна молчать, прежде чем это считается падением.
# Один нулевой замер ловится штатно при перезапуске ffmpeg внутри Frigate и
# падением не является — без этого порога каждый рестарт даёт ложную тревогу.
OFFLINE_STREAK = 3
# Отпускаем тревогу по памяти не на самом пороге, а ниже: около границы
# показание колеблется, и без запаса бот слал бы пары «тревога/норма».
MEM_HYSTERESIS = 5.0
# Температуру меряем мгновенно, а пакет процессора гуляет на 10-12 °C за
# секунды: одиночный замер выше порога — это пик, а не перегрев. Тревожим
# только когда превышение держится столько опросов подряд.
TEMP_STREAK = 4
TEMP_HYSTERESIS = 5.0
# Сколько камер должно смениться разом, чтобы слать сводку вместо россыпи
# отдельных сообщений. Одна-две — это правда про камеры; больше — про общую
# причину, и тогда семь сообщений только мешают её увидеть.
BULK_THRESHOLD = 2
# Потолок Telegram на файл, отправляемый ботом.
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024


def event_score(after: dict) -> float:
    """Достаёт уверенность детекции из события.

    Frigate 0.17 переложил score внутрь `data`, оставив на верхнем уровне
    пустое поле. Без разбора обоих вариантов ненулевой alerts.min_score
    отсекал бы вообще все события.
    """
    data = after.get("data") or {}
    for source in (data, after):
        for key in ("top_score", "score"):
            value = source.get(key)
            if value:
                return float(value)
    return 0.0


def storage_from_stats(stats: dict) -> dict:
    """Место на разделе с записями, в гигабайтах.

    Frigate отдаёт цифры по всей файловой системе, а не только по своим
    файлам, — это и нужно: раздел общий с другими сервисами, и переполнить
    его может кто угодно, а запись встанет у нас.
    """
    storage = (stats.get("service") or {}).get("storage") or {}
    for path in ("/media/frigate/recordings", "/media/frigate/clips"):
        entry = storage.get(path)
        if entry:
            return {
                "free_gb": round(float(entry.get("free") or 0.0) / 1024, 1),
                "total_gb": round(float(entry.get("total") or 0.0) / 1024, 1),
            }
    return {}


def inference_from_stats(stats: dict) -> float | None:
    """Скорость детектора в миллисекундах на кадр."""
    for data in (stats.get("detectors") or {}).values():
        speed = (data or {}).get("inference_speed")
        if speed:
            return round(float(speed), 2)
    return None


def gpu_from_stats(stats: dict) -> float | None:
    """Загрузка видеоядра в процентах.

    Frigate отдаёт её строкой вида "43.15%", а когда счётчики недоступны —
    прочерком. Прочерк считаем отсутствием данных, а не нулём: ноль означал
    бы простаивающее видеоядро, а это совсем другое утверждение.
    """
    for data in (stats.get("gpu_usages") or {}).values():
        raw = (data or {}).get("gpu")
        if not isinstance(raw, str):
            continue
        try:
            return round(float(raw.strip().rstrip("%")), 1)
        except ValueError:
            continue
    return None


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
        self.storage: dict = {}
        self.inference_ms: float | None = None
        self.gpu_pct: float | None = None
        self.frigate_ok = False

        self._last_alert: dict[str, float] = {}
        self._pending_clips: dict[str, str] = {}
        self._offline_reported: set[str] = set()
        self._offline_streak: dict[str, int] = {}
        self._followups: dict[str, asyncio.Task] = {}
        self._frigate_reported_down = False
        self._low_disk_reported = False
        self._high_mem_reported = False
        self._gpu_reported = False
        self._hot_streak = 0
        self._hot_reported = False
        self._disk_problems_reported: set[str] = set()
        self._background: set[asyncio.Task] = set()

    def _links(self, event_id: str) -> str:
        """Ссылки на клип и на само событие во Frigate.

        Пригодны только внутри домашней сети, то есть с VPN. Отдаём их, когда
        видео в Telegram не уехало: посмотреть запись всё равно надо.
        """
        base = self._config.frigate.public_url
        if not base:
            return (
                "\nЧтобы получать прямые ссылки на такие клипы, укажи "
                "<code>frigate.public_url</code> в guard/config.yaml."
            )
        clip = html.escape(f"{base}/api/events/{event_id}/clip.mp4", quote=True)
        explore = html.escape(f"{base}/explore", quote=True)
        return f'\n🔗 <a href="{clip}">Открыть клип</a> · <a href="{explore}">Frigate</a> (нужен VPN)'

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

        score = event_score(after)
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
        frame = None
        if self._config.alerts.event_snapshot:
            # Снимок события с рамкой: видно, что именно детектор счёл человеком.
            frame = await self._frigate.event_snapshot(event_id)
        if frame is None:
            try:
                # Живой кадр всегда под рукой — на него откатываемся, если снимок
                # события ещё не записан или отключён.
                frame = await self._frame(camera)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] не получить кадр: %s", camera, exc)
                await self._notifier.text(f"{caption}\n⚠️ кадр не получен: {html.escape(str(exc))}")
                frame = None
        if frame is not None:
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
            await self._notifier.text(
                f"⚠️ <b>{name}</b>: клип не получен — {html.escape(str(exc))}.{self._links(event_id)}"
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("клип %s", event_id)
            await self._notifier.text(
                f"⚠️ <b>{name}</b>: клип не получен — {html.escape(str(exc))}.{self._links(event_id)}"
            )
            return

        size_mb = len(clip) / 1024 / 1024
        if len(clip) > TELEGRAM_FILE_LIMIT:
            # Отправлять бессмысленно: Telegram отклонит, а причина потеряется в логах.
            await self._notifier.text(
                f"🎥 <b>{name}</b>: клип {size_mb:.0f} МБ — больше лимита Telegram "
                f"({TELEGRAM_FILE_LIMIT // 1024 // 1024} МБ для ботов), "
                f"событие вышло длинным.{self._links(event_id)}"
            )
            return

        error = await self._notifier.video(clip, f"🎥 {name} · {size_mb:.1f} МБ")
        if error:
            await self._notifier.text(
                f"⚠️ <b>{name}</b>: клип {size_mb:.1f} МБ не ушёл в Telegram — "
                f"{html.escape(error)}{self._links(event_id)}"
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
                self.storage = storage_from_stats(stats)
                self.inference_ms = inference_from_stats(stats)
                self.gpu_pct = gpu_from_stats(stats)
                await self._check_storage()
                await self._check_disks()
                if self._config.alerts.notify_offline:
                    await self._report_transitions(health)
            # Вне ветки else намеренно: память надо мерить и тогда, когда
            # Frigate не отвечает. Именно в этом состоянии он и течёт.
            await self._check_memory()
            await self._check_gpu()
            await self._check_temp()
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

    async def _check_storage(self) -> None:
        """Предупреждает, пока место ещё есть.

        При заполненном разделе запись просто прекращается, и узнать об этом
        постфактум — ровно тогда, когда запись понадобилась, — худший вариант.
        """
        limit = self._config.alerts.min_free_gb
        free = self.storage.get("free_gb")
        if not limit or free is None:
            return
        if free < limit and not self._low_disk_reported:
            self._low_disk_reported = True
            await self._notifier.text(
                f"⚠️ Мало места под записи: свободно {free} ГБ из "
                f"{self.storage.get('total_gb', '?')} ГБ (порог {limit:g} ГБ).\n"
                "Когда раздел заполнится, запись остановится. Уменьши срок хранения "
                "во Frigate или освободи место."
            )
        elif free >= limit and self._low_disk_reported:
            self._low_disk_reported = False
            await self._notifier.text(f"✅ Место под записи в норме: свободно {free} ГБ.")

    async def _check_disks(self) -> None:
        """Сообщает о деградации дисков по данным SMART.

        Диск такого возраста, как правило, не умирает мгновенно — он начинает
        сыпаться постепенно, и первый же плохой сектор стоит увидеть сразу.
        """
        problems = set(system.disk_problems(system.smart()))
        for text in sorted(problems - self._disk_problems_reported):
            await self._notifier.text(f"⚠️ <b>Диск</b>: {html.escape(text)}")
        if self._disk_problems_reported and not problems:
            await self._notifier.text("✅ Претензий к дискам по SMART больше нет.")
        self._disk_problems_reported = problems

    async def _check_memory(self) -> None:
        """Предупреждает, пока память ещё есть.

        Течь может любой контейнер, а без лимита памяти утечка забирает не свой
        процесс, а всю машину: ядро уходит в своп, и сервер перестаёт отвечать
        целиком — вместе с этим ботом, который и должен был предупредить.
        Поэтому порог низкий: важно не «уже беда», а «через пару часов будет».
        """
        limit = self._config.alerts.max_mem_pct
        mem = system.memory()
        if not limit or not mem:
            return
        used = mem["used_pct"]
        if used >= limit and not self._high_mem_reported:
            self._high_mem_reported = True
            await self._notifier.text(
                f"⚠️ Память занята на {used}% — {mem['used_gb']} из {mem['total_gb']} ГБ "
                f"(порог {limit:g}%).\n"
                "Похоже на утечку. Когда память кончится, сервер перестанет отвечать "
                "целиком, включая этого бота. Посмотри график памяти на странице "
                "телеметрии и перезапусти виновный контейнер."
            )
        elif used < limit - MEM_HYSTERESIS and self._high_mem_reported:
            self._high_mem_reported = False
            await self._notifier.text(f"✅ Память в норме: занято {used}%.")

    async def _check_gpu(self) -> None:
        """Ловит срыв встроенного видеоядра.

        Аппаратное декодирование всех камер и сам детектор живут на одной
        встроенной графике. Когда она зависает, разом умирает всё, а ядро до
        перезагрузки безуспешно пытается её сбросить — раз в три секунды,
        часами. Само это не рассасывается, и знать надо сразу.
        """
        if self._gpu_reported or system.gpu_error() is not True:
            return
        self._gpu_reported = True
        total = len(self.camera_health)
        dead = [n for n, i in self.camera_health.items() if not i.get("stable", True)]
        if dead:
            await self._notifier.text(
                "🛑 <b>Сорвалось видеоядро.</b>\n"
                f"Аппаратное декодирование не работает, камер без потока: {len(dead)} из "
                f"{total}. Само не починится — нужна перезагрузка сервера."
            )
        else:
            await self._notifier.text(
                "⚠️ Видеоядро срывалось, но обошлось — камеры отдают поток.\n"
                "Если повторится, стоит снять с него нагрузку: перевести detect на субпотоки."
            )

    async def _check_temp(self) -> None:
        """Ловит перегрев процессора.

        Единственный признак, по которому виден отказ вентилятора: обороты
        показывает не всякое железо, а после замены кулера датчик может
        замолчать совсем. Температура же есть всегда, и при вставшем
        вентиляторе она уходит вверх за минуты.
        """
        limit = self._config.alerts.max_cpu_temp
        value = (system.temperatures() or {}).get("CPU")
        if not limit or value is None:
            return
        if value >= limit:
            self._hot_streak += 1
        elif value < limit - TEMP_HYSTERESIS:
            self._hot_streak = 0

        if self._hot_streak >= TEMP_STREAK and not self._hot_reported:
            self._hot_reported = True
            minutes = TEMP_STREAK * MONITOR_INTERVAL / 60
            rpm = system.fans()
            fan = (" · ".join(f"{k} {v} об/мин" for k, v in rpm.items()) if rpm
                   else "датчик оборотов молчит")
            await self._notifier.text(
                f"🌡 <b>Процессор перегревается:</b> {value:.0f} °C уже "
                f"{minutes:.0f} мин (порог {limit:g} °C).\n"
                f"Вентилятор: {html.escape(fan)}.\n"
                "Похоже на остановку вентилятора или забитый радиатор. "
                "Ближе к 100 °C начнётся троттлинг, и детекция станет отставать."
            )
        elif self._hot_streak == 0 and self._hot_reported:
            self._hot_reported = False
            await self._notifier.text(
                f"✅ Температура процессора в норме: {value:.0f} °C."
            )

    @property
    def armed(self) -> bool:
        return bool(self._state.armed)

    @property
    def storage_low(self) -> bool:
        limit = self._config.alerts.min_free_gb
        free = self.storage.get("free_gb")
        return bool(limit and free is not None and free < limit)

    async def _report_transitions(self, health: dict[str, dict]) -> None:
        """Сообщает о переходах камер, схлопывая массовые.

        При срыве видеоядра камеры отваливаются все разом, и россыпь из семи
        сообщений (а вместе со сторожем — четырнадцати) прячет главное:
        случилось одно общее событие, а не семь независимых.
        """
        went_down = sorted(n for n, i in health.items()
                           if not i["stable"] and n not in self._offline_reported)
        came_up = sorted(n for n, i in health.items()
                         if i["stable"] and n in self._offline_reported)
        self._offline_reported |= set(went_down)
        self._offline_reported -= set(came_up)

        if len(went_down) > BULK_THRESHOLD:
            await self._notifier.text(
                f"⚠️ Разом отвалились {len(went_down)} камер: "
                f"{', '.join(html.escape(n) for n in went_down)}.\n"
                "Столько сразу — это не камеры, а общая причина: видеоядро, сеть или Frigate."
            )
        else:
            for name in went_down:
                await self._notifier.text(
                    f"⚠️ Камера <b>{html.escape(name)}</b> не отдаёт поток — "
                    "движение с неё не отслеживается."
                )

        if len(came_up) > BULK_THRESHOLD:
            await self._notifier.text(
                f"✅ Снова на связи {len(came_up)} камер: "
                f"{', '.join(html.escape(n) for n in came_up)}."
            )
        else:
            for name in came_up:
                await self._notifier.text(f"✅ Камера <b>{html.escape(name)}</b> снова на связи.")

    async def shutdown(self) -> None:
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
