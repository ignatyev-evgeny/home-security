"""Сторож видеоядра на хосте: когда он перезагружает сервер, а когда нет.

Скрипт единственный во всём проекте, который перезагружает машину, поэтому
проверяется прежде всего обратное: что в обычной жизни он этого не делает.
"""
import importlib.util, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gpu_watchdog", ROOT / "tools/gpu-watchdog.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)

tmp = Path(tempfile.mkdtemp())
wd.STAMP = tmp / "stamp"

REBOOTS: list = []
SENT: list = []
wd.subprocess.run = lambda cmd, **k: REBOOTS.append(cmd)
wd.notify = lambda text: SENT.append(text)
wd.time.sleep = lambda s: None


def scenario(hangs, up=3600, stamp=None):
    REBOOTS.clear(); SENT.clear()
    wd.hang_count = lambda: hangs
    wd.uptime = lambda: up
    if stamp is None:
        wd.STAMP.unlink(missing_ok=True)
    else:
        wd.STAMP.write_text(str(stamp))
    wd.main()
    return bool(REBOOTS)


# --- обычная жизнь: перезагружать не за что --------------------------------
assert not scenario(0), "без срывов перезагрузка недопустима"
print("срывов нет — сервер не трогаем")

assert not scenario(wd.MIN_HANGS - 1), "одиночные срывы ядро переживает само"
print(f"{wd.MIN_HANGS - 1} срывов — это восстановившийся сбой, не перезагружаем")

# --- смертельный случай -----------------------------------------------------
assert scenario(wd.MIN_HANGS), "порог должен срабатывать"
assert SENT and "Перезагружаю сервер" in SENT[0], SENT
assert "systemctl" in " ".join(REBOOTS[0]), REBOOTS
print(f"{wd.MIN_HANGS} срывов за {wd.WINDOW_MINUTES} мин — перезагрузка,",
      "с предупреждением в Telegram")

assert scenario(60), "непрерывный цикл сбросов — тем более"
print("60 срывов (как в реальной аварии) — перезагрузка")

# --- предохранители ---------------------------------------------------------
assert not scenario(60, up=wd.MIN_UPTIME - 1), \
    "сразу после загрузки нельзя: получится вечный цикл"
print(f"аптайм меньше {wd.MIN_UPTIME // 60} мин — воздерживаемся, чтобы не зациклиться")

assert not scenario(60, stamp=time.time() - 3600), \
    "перезагрузка была час назад — повторять нельзя"
print("перезагрузка была час назад — второй раз не идём")

assert scenario(60, stamp=time.time() - wd.COOLDOWN - 60), \
    "сутки прошли — можно снова"
print(f"прошло больше {wd.COOLDOWN // 3600} ч — снова разрешено")

# метка времени пишется, иначе предохранитель бесполезен
assert wd.STAMP.exists() and time.time() - float(wd.STAMP.read_text()) < 10
print("метка времени последней перезагрузки сохраняется")

# --- разбор конфигов --------------------------------------------------------
wd.PROJECT = tmp
(tmp / "guard").mkdir(exist_ok=True)
(tmp / ".env").write_text("TZ=Europe/Moscow\nBOT_TOKEN=123:ABC\nCAM_PASSWORD=x\n")
(tmp / "guard/config.yaml").write_text(
    "telegram:\n  bot_token: ${BOT_TOKEN}\n  allowed_chat_ids:\n"
    "    - 297978281\n    - 548055215\n\ntimezone: Europe/Moscow\n")
token, chats = wd.telegram_targets()
assert token == "123:ABC", token
assert chats == ["297978281", "548055215"], chats
print("токен и получатели берутся из конфига бота:", len(chats), "адресата")

# отсутствие конфигов не роняет — перезагрузка важнее уведомления
wd.PROJECT = tmp / "нет-такого"
assert wd.telegram_targets() == ("", [])
print("без конфигов не падаем")

print("\nGPU WATCHDOG OK")
