from __future__ import annotations

import io

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import SingleQuotedScalarString as SQ

from .config import CAMERA_NAME_RE, CameraDefaults


class CameraEditError(RuntimeError):
    pass


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # иначе длинные RTSP-ссылки переносятся и конфиг становится нечитаемым
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _load(raw: str) -> CommentedMap:
    data = _yaml().load(raw)
    if not isinstance(data, CommentedMap):
        raise CameraEditError("конфиг Frigate не похож на YAML-словарь")
    return data


def _dump(data: CommentedMap) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def list_cameras(raw: str) -> list[str]:
    return sorted((_load(raw).get("cameras") or {}).keys())


def rtsp_url(host: str, path: str, defaults: CameraDefaults) -> str:
    return (
        f"rtsp://{defaults.username}:{defaults.password_placeholder}"
        f"@{host}:{defaults.rtsp_port}{path}"
    )


def add_camera(raw: str, name: str, host: str, defaults: CameraDefaults) -> str:
    if not CAMERA_NAME_RE.match(name):
        raise CameraEditError(
            f"недопустимое имя «{name}»: латиница в нижнем регистре, цифры и _, начиная с буквы"
        )
    data = _load(raw)
    cameras = data.get("cameras")
    if cameras is None:
        cameras = CommentedMap()
        data["cameras"] = cameras
    if name in cameras:
        raise CameraEditError(f"камера «{name}» уже есть в конфиге")

    def make_input(path: str, roles: list[str]) -> CommentedMap:
        item = CommentedMap()
        item["path"] = SQ(rtsp_url(host, path, defaults))
        item["input_args"] = "preset-rtsp-generic"
        item["roles"] = roles
        return item

    if defaults.record_path == defaults.detect_path:
        # Один и тот же поток: два входа означали бы два процесса ffmpeg,
        # читающих одно и то же.
        inputs = [make_input(defaults.record_path, ["record", "detect"])]
    else:
        inputs = [
            make_input(defaults.record_path, ["record"]),
            make_input(defaults.detect_path, ["detect"]),
        ]

    ffmpeg = CommentedMap()
    ffmpeg["inputs"] = inputs

    camera = CommentedMap()
    camera["ffmpeg"] = ffmpeg

    cameras[name] = camera
    return _dump(data)


def remove_camera(raw: str, name: str) -> str:
    data = _load(raw)
    cameras = data.get("cameras") or {}
    if name not in cameras:
        raise CameraEditError(f"камеры «{name}» нет в конфиге")
    if len(cameras) == 1:
        # Frigate не стартует с пустым списком камер.
        raise CameraEditError("это последняя камера — Frigate не запустится без единой камеры")
    del cameras[name]
    return _dump(data)
