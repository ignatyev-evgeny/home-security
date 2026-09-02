"""Ручной режим вентилятора: заявка из бота и исполнитель на хосте.

Написан 02.09.2026. Выяснено опытом на OptiPlex 7050: штатного «верни
управление BIOS» у драйвера нет — запись 3 в cur_state отклоняется,
перезагрузка модуля не помогает, само не рассасывается. Возврат даёт
только запись нуля в pwm1. Ручной режим допустим лишь потому, что выход
из него — команда, а не надежда, и тесты стерегут именно это.
"""
import importlib.util, json, os, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

from app import fan

# ---------------------------------------------------------------- сторона бота
tmp = Path(tempfile.mkdtemp())
fan.DATA = tmp
fan.REQUEST = tmp / "fan.request"
fan.STATE = tmp / "fan.state"

assert fan.available() is False, "без файла состояния управление не предлагается"
print("исполнитель не настроен — управление не предлагается")

assert fan.request("нечто") is False, "неизвестный режим не должен записываться"
assert not fan.REQUEST.exists()
print("неизвестный режим отвергается")

assert fan.request("high", minutes=30)
req = json.loads(fan.REQUEST.read_text())
assert req["mode"] == "high"
left = (req["until"] - time.time()) / 60
assert 29 < left <= 30, left
print(f"заявка записана: high, срок {left:.0f} мин")

fan.STATE.write_text(json.dumps({"mode": "high", "rpm": 1655, "temp": 41.0,
                                 "until": time.time() + 1800, "note": ""}))
assert fan.available()
text = fan.describe(fan.state())
assert "высокий" in text and "1655 об/мин" in text and "41 °C" in text, text
assert "Вернётся в авто через 30 мин" in text, text
print("описание:", text.replace("\n", " | "))

text = fan.describe({"mode": "auto", "rpm": 843, "temp": 40.0,
                     "note": "перегрев 71 °C — управление возвращено BIOS"})
assert "авто (BIOS)" in text and "Перегрев" in text, text
print("возврат по перегреву виден в описании")
assert "неизвестно" in fan.describe({})
print("пустое состояние не притворяется рабочим")

# ------------------------------------------------------------ сторона хоста
spec = importlib.util.spec_from_file_location("fanctl", ROOT / "tools/fan-control.py")
fc = importlib.util.module_from_spec(spec); spec.loader.exec_module(fc)

sysfs = Path(tempfile.mkdtemp())
mon = sysfs / "hwmon" / "hwmon2"; mon.mkdir(parents=True)
(mon / "name").write_text("dell_smm")
(mon / "fan1_input").write_text("843")
(mon / "temp1_input").write_text("41000")
(mon / "pwm1").write_text("255")
dev = sysfs / "thermal" / "cooling_device13"; dev.mkdir(parents=True)
(dev / "type").write_text("dell-smm-fan1")
(dev / "cur_state").write_text("1")
fc.HWMON = sysfs / "hwmon"; fc.COOLING = sysfs / "thermal"
fc.REQUEST = tmp / "fan.request"; fc.STATE = tmp / "host.state"

def run(mode=None, until=None, temp=41.0, rpm=843):
    (mon / "temp1_input").write_text(str(int(temp * 1000)))
    (mon / "fan1_input").write_text(str(rpm))
    if mode is None:
        fc.REQUEST.unlink(missing_ok=True)
    else:
        fc.REQUEST.write_text(json.dumps({"mode": mode, "until": until or time.time() + 3600}))
    (mon / "pwm1").write_text("255"); (dev / "cur_state").write_text("9")
    fc.main()
    return json.loads(fc.STATE.read_text())

st = run("high")
assert st["mode"] == "high" and (dev / "cur_state").read_text() == "2", st
print("режим «высокий» пишет cur_state=2")

fc.STATE.unlink()
st = run("low")
assert st["mode"] == "low" and (dev / "cur_state").read_text() == "1"
print("режим «низкий» пишет cur_state=1")

fc.STATE.unlink()
st = run("auto")
assert st["mode"] == "auto" and (mon / "pwm1").read_text() == "0", st
print("режим «авто» пишет pwm1=0 — единственный способ вернуть BIOS")

# --- предохранитель по времени ---------------------------------------------
fc.STATE.write_text(json.dumps({"mode": "high"}))
st = run("high", until=time.time() - 1)
assert st["mode"] == "auto", st
assert "срок" in st["note"], st
assert (mon / "pwm1").read_text() == "0"
assert not fc.REQUEST.exists(), "просроченная заявка должна стираться"
print("истёкший срок возвращает авто:", st["note"])

# --- предохранитель по температуре ------------------------------------------
fc.STATE.write_text(json.dumps({"mode": "high"}))
st = run("high", temp=fc.TEMP_GUARD + 1)
assert st["mode"] == "auto" and "перегрев" in st["note"], st
assert (mon / "pwm1").read_text() == "0"
assert not fc.REQUEST.exists()
print("перегрев возвращает авто немедленно:", st["note"])

# порог перегрева ниже тревожного, чтобы BIOS успел справиться сам
assert fc.TEMP_GUARD < 80, "порог должен быть ниже тревоги бота"
print(f"порог возврата {fc.TEMP_GUARD:.0f} °C — ниже тревожных 80")

# --- лишних SMM-вызовов не делаем -------------------------------------------
fc.STATE.write_text(json.dumps({"mode": "high", "error": None}))
(dev / "cur_state").write_text("СТОРОЖ")
fc.REQUEST.write_text(json.dumps({"mode": "high", "until": time.time() + 3600}))
fc.main()
assert (dev / "cur_state").read_text() == "СТОРОЖ", "режим не менялся — писать в железо нельзя"
print("при неизменном режиме запись в железо не повторяется")

# --- потолок срока ----------------------------------------------------------
fc.STATE.unlink()
st = run("high", until=time.time() + 99 * 86400)
assert st["until"] <= time.time() + fc.MAX_TTL + 5, "срок должен ограничиваться"
print(f"срок ручного режима ограничен {fc.MAX_TTL/3600:.0f} часами")

# --- железа нет -------------------------------------------------------------
fc.HWMON = sysfs / "нет-такого"
fc.STATE.unlink()
st = run("high")
assert st["mode"] == "high" and st["error"], st
print("без железа не падаем, а сообщаем об ошибке:", st["error"])

print("\nFAN OK")
