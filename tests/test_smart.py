"""Проверка чтения SMART: свежесть данных, распознавание проблем, вывод в статус."""
import asyncio, json, os, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

tmp = Path(tempfile.mkdtemp())
smart_file = tmp / "smart.json"
os.environ["SMART_FILE"] = str(smart_file)

from app import system
from app.config import load_config
from app.guard import Guard
from app.state import ArmState

cfg_path = tmp / "c.yaml"
cfg_path.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(cfg_path)

HEALTHY = {"sda": {"model": "HGST HTS725050A7E630", "passed": True, "temp": 39,
                   "hours": 59172, "reallocated": 0, "pending": 0, "uncorrectable": 0},
           "nvme0n1": {"model": "SAMSUNG", "passed": True, "temp": 45,
                       "used_pct": 3, "media_errors": 0}}

def write(disks, age=0.0):
    smart_file.write_text(json.dumps({"updated": time.time() - age, "disks": disks}))

# --- файла нет: не ошибка, просто нет данных ---------------------------------
assert system.smart() == {}, "отсутствие файла должно давать пустой ответ"
assert system.format_disks({}) is None
print("без файла SMART бот работает как раньше")

# --- здоровые диски -----------------------------------------------------------
write(HEALTHY)
s = system.smart()
assert set(s["disks"]) == {"sda", "nvme0n1"}, s
assert system.disk_problems(s) == [], system.disk_problems(s)
line = system.format_disks(s)
assert line.startswith("💿") and "39 °C" in line and "6 лет" in line, line
assert "0 лет" not in line, "молодой диск не должен показываться как нулевой"
print("здоровые диски:", line)

# возраст словами: месяцы до года, дальше годы в нужном падеже
for hours, want in ((0, None), (720, "1 мес"), (5639, "7 мес"), (8760, "1 год"),
                    (17520, "2 года"), (43800, "5 лет"), (183960, "21 год")):
    got = system.format_age(hours)
    assert got == want, f"{hours} ч -> {got}, ожидалось {want}"
print("возраст диска склоняется правильно")

# --- появились переназначенные секторы ----------------------------------------
bad = json.loads(json.dumps(HEALTHY))
bad["sda"]["reallocated"] = 8
write(bad)
probs = system.disk_problems(system.smart())
assert probs == ["sda: 8 переназначенных секторов"], probs
assert system.format_disks(system.smart()).startswith("⚠️")
print("деградация замечена:", probs[0])

# --- SMART сообщает о неисправности -------------------------------------------
failing = json.loads(json.dumps(HEALTHY))
failing["sda"]["passed"] = False
write(failing)
assert system.disk_problems(system.smart()) == ["sda: SMART сообщает о неисправности"]
print("отказ по общему статусу распознан")

# --- ошибки носителя NVMe ------------------------------------------------------
nvme_bad = json.loads(json.dumps(HEALTHY))
nvme_bad["nvme0n1"]["media_errors"] = 2
write(nvme_bad)
assert system.disk_problems(system.smart()) == ["nvme0n1: 2 ошибок носителя"]
print("ошибки носителя NVMe распознаны")

# --- протухшие данные ----------------------------------------------------------
write(HEALTHY, age=7200)
s = system.smart()
assert s.get("stale") is True, s
assert "устарели" in system.format_disks(s)
print("протухшие данные помечаются, а не выдаются за свежие")

# --- диск, который не отдал SMART ----------------------------------------------
write({"sdb": {"error": "smartctl недоступен"}})
assert system.disk_problems(system.smart()) == [], "ошибку опроса приняли за неисправность"
print("недоступный SMART не считается поломкой")


# --- предупреждения в Telegram --------------------------------------------------
class N:
    def __init__(self): self.texts = []
    async def text(self, m): self.texts.append(m)
    async def photo(self, *a, **k): return None
    async def video(self, *a, **k): return None

async def main():
    n = N()
    g = Guard(cfg, ArmState(tmp / "s.json"), n, None)

    write(HEALTHY)
    await g._check_disks()
    assert not n.texts, n.texts

    write(bad)
    await g._check_disks()
    assert len(n.texts) == 1 and "переназначенных" in n.texts[0], n.texts
    print("предупреждение отправлено:", n.texts[0])

    await g._check_disks()
    assert len(n.texts) == 1, f"повторное предупреждение: {n.texts}"
    print("повторов нет")

    write(HEALTHY)
    await g._check_disks()
    assert len(n.texts) == 2 and "больше нет" in n.texts[1], n.texts
    print("восстановление:", n.texts[1])

    await g.shutdown()

asyncio.run(main())
print("\nSMART OK")
