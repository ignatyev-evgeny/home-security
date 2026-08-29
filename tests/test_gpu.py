"""Срыв видеоядра и схлопывание массовых падений камер.

Написан после аварии 29.08.2026. В 23:30:56 повисла встроенная графика:
разом встали все семь ffmpeg и сам детектор, ядро пять часов безуспешно
пыталось её сбросить, Frigate тёк по 6 ГБ в час, и в 04:32 сервер умер.
Бот прислал семь сообщений «камера не отдаёт поток» — формально верных,
но не называющих причину и не подсказывающих, что делать.
"""
import asyncio, os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "guard"))
os.environ.update(BOT_TOKEN="1:F", FRIGATE_PASSWORD="", CAM_PASSWORD="s",
                  WATCHDOG_URL="https://x/hb", WATCHDOG_TOKEN="t")

from app import guard as guard_mod
from app import system
from app.config import load_config
from app.guard import Guard, BULK_THRESHOLD
from app.state import ArmState

tmp = Path(tempfile.mkdtemp())
p = tmp / "c.yaml"
p.write_text((ROOT / "guard/config.example.yaml").read_text().replace("- 000000000", "- 42"))
cfg = load_config(p)


class N:
    def __init__(self): self.texts = []
    async def text(self, m): self.texts.append(m)
    async def photo(self, *a, **k): return None
    async def video(self, *a, **k): return None


# --- чтение /sys: настоящий формат, который отдаёт i915 ----------------------
def with_drm(content):
    """Подкладывает card0/error с заданным содержимым."""
    root = Path(tempfile.mkdtemp())
    if content is not None:
        card = root / "card0"; card.mkdir()
        (card / "error").write_text(content)
    system.DRM = root
    return root

assert with_drm("No error state collected\n") is not None
assert system.gpu_error() is False, "штатный ответ i915 не должен считаться срывом"
print("«No error state collected» — срыва нет")

with_drm("GPU HANG: ecode 9:0:00000000\nActive process: ffmpeg [5860]\n" + "x" * 5_000_000)
assert system.gpu_error() is True, "дамп ошибки должен распознаваться"
print("дамп ошибки распознан (и прочитан без загрузки мегабайтов в память)")

with_drm(None)
assert system.gpu_error() is None, "нет файла — это «неизвестно», а не «всё хорошо»"
print("отсутствие файла — неизвестно, а не «срывов нет»")


def health(*names, dead=()):
    return {n: {"online": n not in dead, "stable": n not in dead} for n in names}


CAMS = [f"cam_11{i}" for i in range(7)]


async def main():
    # --- срыв видеоядра при мёртвых камерах ---------------------------------
    n = N()
    g = Guard(cfg, ArmState(tmp / "s.json"), n, None)
    g.camera_health = health(*CAMS, dead=CAMS)
    with_drm("No error state collected\n")
    await g._check_gpu()
    assert not n.texts, "без срыва тревожить не за что"

    with_drm("GPU HANG: ecode 9:0:00000000\n")
    await g._check_gpu()
    assert len(n.texts) == 1, n.texts
    assert "видеоядро" in n.texts[0] and "7 из 7" in n.texts[0], n.texts[0]
    assert "перезагрузка" in n.texts[0], "надо сказать, что делать"
    print("срыв:", n.texts[0].splitlines()[0], "|", n.texts[0].splitlines()[1][:60])

    # не повторяемся каждые 30 секунд пять часов подряд
    for _ in range(5):
        await g._check_gpu()
    assert len(n.texts) == 1, "тревога не должна повторяться"
    print("повторов нет")

    # --- срыв, который обошёлся ---------------------------------------------
    n2 = N()
    g2 = Guard(cfg, ArmState(tmp / "s2.json"), n2, None)
    g2.camera_health = health(*CAMS)
    await g2._check_gpu()
    assert len(n2.texts) == 1 and "обошлось" in n2.texts[0], n2.texts
    print("восстановившийся срыв отмечается мягче:", n2.texts[0].splitlines()[0])

    # неизвестное состояние не тревожит
    n3 = N()
    g3 = Guard(cfg, ArmState(tmp / "s3.json"), n3, None)
    with_drm(None)
    await g3._check_gpu()
    assert not n3.texts, "«неизвестно» — не повод для тревоги"
    print("недоступное /sys не тревожит")

    # --- схлопывание массовых падений ---------------------------------------
    n4 = N()
    g4 = Guard(cfg, ArmState(tmp / "s4.json"), n4, None)

    # одна камера — обычное сообщение, с именем
    await g4._report_transitions(health(*CAMS, dead=CAMS[:1]))
    assert len(n4.texts) == 1 and CAMS[0] in n4.texts[0], n4.texts
    print("одна камера — отдельное сообщение")

    # две — всё ещё по отдельности: это порог
    n4.texts.clear()
    await g4._report_transitions(health(*CAMS, dead=CAMS[:BULK_THRESHOLD]))
    assert len(n4.texts) == BULK_THRESHOLD - 1, n4.texts
    print(f"{BULK_THRESHOLD} камеры — по-прежнему по отдельности")

    # все семь — одна сводка вместо россыпи
    n4.texts.clear()
    await g4._report_transitions(health(*CAMS, dead=CAMS))
    assert len(n4.texts) == 1, f"ожидалась одна сводка, пришло {len(n4.texts)}"
    assert "Разом отвалились 5 камер" in n4.texts[0], n4.texts[0]
    assert all(c in n4.texts[0] for c in CAMS[2:]), "в сводке должны быть все имена"
    print("массовое падение:", n4.texts[0].splitlines()[0])

    # возврат тоже схлопывается
    n4.texts.clear()
    await g4._report_transitions(health(*CAMS))
    assert len(n4.texts) == 1 and "Снова на связи 7 камер" in n4.texts[0], n4.texts
    print("массовый возврат:", n4.texts[0])

    # и не повторяется на следующем опросе
    n4.texts.clear()
    await g4._report_transitions(health(*CAMS))
    assert not n4.texts, "стабильное состояние молчит"
    print("стабильное состояние молчит")

asyncio.run(main())
print("\nGPU OK")
