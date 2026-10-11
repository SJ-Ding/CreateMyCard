"""本机设备截图配置的校验、版本控制与原子持久化。"""

from __future__ import annotations

import os
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from debug_tools.paths import DEBUG_TOOLS_ROOT

from .commands import DeviceError

_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_KINDS = {"physical", "emulator", "unknown"}
_SIZES = {"2x2", "2x4"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DeviceProfileStore:
    """保存不含密钥和工具链路径的设备截图配置。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEBUG_TOOLS_ROOT / "device_config.yaml"

    def list(self) -> list[dict[str, Any]]:
        return [deepcopy(item) for item in self._read()]

    def get(self, profile_id: str) -> dict[str, Any]:
        for item in self._read():
            if item.get("id") == profile_id:
                return deepcopy(item)
        raise DeviceError("设备配置不存在", 404)

    def create(self, value: dict[str, Any]) -> dict[str, Any]:
        profiles = self._read()
        profile_id = str(value.get("id") or f"device_{uuid.uuid4().hex[:12]}")
        if any(item.get("id") == profile_id for item in profiles):
            raise DeviceError("设备配置标识已存在", 409)
        profile = self._validate(value, profile_id, 1)
        profiles.append(profile)
        self._write(profiles)
        return deepcopy(profile)

    def update(self, profile_id: str, value: dict[str, Any]) -> dict[str, Any]:
        profiles = self._read()
        for index, current in enumerate(profiles):
            if current.get("id") != profile_id:
                continue
            expected = value.get("version")
            if not isinstance(expected, int) or expected != current.get("version"):
                raise DeviceError("设备配置已被其他页面更新，请重新加载", 409)
            profile = self._validate(value, profile_id, expected + 1)
            profiles[index] = profile
            self._write(profiles)
            return deepcopy(profile)
        raise DeviceError("设备配置不存在", 404)

    def snapshot(self, profile_id: str) -> dict[str, Any]:
        return self.get(profile_id)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            root = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise DeviceError("设备配置文件损坏，请修复或备份后移走", 503) from exc
        if not isinstance(root, dict) or root.get("schemaVersion") != "device-config-v1":
            raise DeviceError("设备配置文件版本或根节点无效", 503)
        raw_profiles = root.get("profiles")
        if not isinstance(raw_profiles, list):
            raise DeviceError("设备配置列表无效", 503)
        profiles: list[dict[str, Any]] = []
        for raw in raw_profiles:
            if not isinstance(raw, dict):
                raise DeviceError("设备配置项必须是对象", 503)
            profile_id = raw.get("id")
            version = raw.get("version")
            if not isinstance(profile_id, str) or not isinstance(version, int):
                raise DeviceError("设备配置缺少标识或版本", 503)
            profiles.append(self._validate(raw, profile_id, version, preserve_time=True))
        return profiles

    def _write(self, profiles: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        document = {"schemaVersion": "device-config-v1", "profiles": profiles}
        temporary.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @staticmethod
    def _validate(
        value: dict[str, Any],
        profile_id: str,
        version: int,
        *,
        preserve_time: bool = False,
    ) -> dict[str, Any]:
        if not _PROFILE_ID.fullmatch(profile_id):
            raise DeviceError("设备配置标识无效", 422)
        name = value.get("name")
        serial = value.get("serial")
        device_name = value.get("deviceName")
        kind = value.get("kind", "unknown")
        emulator_id = value.get("emulatorId")
        render_wait = value.get("renderWaitSeconds", 8.0)
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise DeviceError("设备配置名称不能为空且不能超过 80 个字符", 422)
        if not isinstance(serial, str) or not serial.strip() or len(serial) > 128:
            raise DeviceError("设备连接标识无效", 422)
        if not isinstance(device_name, str) or not device_name.strip() or len(device_name) > 128:
            raise DeviceError("设备名称无效", 422)
        if kind not in _KINDS:
            raise DeviceError("设备类型无效", 422)
        if emulator_id is not None and not isinstance(emulator_id, str):
            raise DeviceError("模拟器关联无效", 422)
        if not isinstance(render_wait, (int, float)) or not 0.0 <= float(render_wait) <= 120.0:
            raise DeviceError("渲染等待时间须在 0 到 120 秒之间", 422)
        resolution = DeviceProfileStore._resolution(value.get("resolution"))
        crops = value.get("crops", {})
        if not isinstance(crops, dict):
            raise DeviceError("裁切框配置必须是对象", 422)
        normalized_crops: dict[str, dict[str, int]] = {}
        for size, crop in crops.items():
            if size not in _SIZES:
                raise DeviceError("裁切框只支持 2x2 和 2x4", 422)
            normalized_crops[size] = DeviceProfileStore._crop(crop, resolution)
        updated_at = value.get("updatedAt") if preserve_time else _now()
        if not isinstance(updated_at, str) or not updated_at:
            updated_at = _now()
        return {
            "id": profile_id,
            "name": name.strip(),
            "serial": serial.strip(),
            "deviceName": device_name.strip(),
            "kind": kind,
            "emulatorId": emulator_id or None,
            "resolution": resolution,
            "crops": normalized_crops,
            "renderWaitSeconds": float(render_wait),
            "version": version,
            "updatedAt": updated_at,
        }

    @staticmethod
    def _resolution(value: Any) -> dict[str, int]:
        if not isinstance(value, dict):
            raise DeviceError("设备截图分辨率不能为空", 422)
        width = value.get("width")
        height = value.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            raise DeviceError("设备截图分辨率必须是整数", 422)
        if width <= 0 or height <= 0 or width > 16384 or height > 16384:
            raise DeviceError("设备截图分辨率超出支持范围", 422)
        return {"width": width, "height": height}

    @staticmethod
    def _crop(value: Any, resolution: dict[str, int]) -> dict[str, int]:
        if not isinstance(value, dict):
            raise DeviceError("裁切框必须是对象", 422)
        keys = ("x", "y", "width", "height")
        if any(not isinstance(value.get(key), int) for key in keys):
            raise DeviceError("裁切框坐标必须是整数", 422)
        x, y = value.get("x"), value.get("y")
        width, height = value.get("width"), value.get("height")
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise DeviceError("裁切框坐标或尺寸无效", 422)
        if x + width > resolution["width"] or y + height > resolution["height"]:
            raise DeviceError("裁切框超出设备截图范围", 422)
        return {"x": x, "y": y, "width": width, "height": height}
