"""Resolve local DevEco and Node.js tools for every debug workflow."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from debug_tools.paths import DEBUG_TOOLS_ROOT, REPOSITORY_ROOT

DEFAULT_CONFIG_PATH = DEBUG_TOOLS_ROOT / "end_to_end_debug" / "backend" / "debug_agent.yaml"
DEVECO_HOME_REFERENCE = "${environment.deveco_studio_home}"


class ToolchainError(RuntimeError):
    """The configured local toolchain cannot be resolved safely."""


@dataclass(frozen=True)
class Toolchain:
    deveco_studio_home: Path | None
    node_executable: Path
    node_source: str
    java_home: Path | None
    sdk_home: Path | None
    hdc: Path | None
    hvigor: Path | None
    ohpm: Path | None
    emulator: Path | None

    @property
    def node_home(self) -> Path:
        return self.node_executable.parent

    def environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        environment = dict(base) if base is not None else os.environ.copy()
        environment["NODE_HOME"] = str(self.node_home)
        paths = [self.node_home]
        if self.java_home is not None:
            environment["JAVA_HOME"] = str(self.java_home)
            paths.append(self.java_home / "bin")
        if self.sdk_home is not None:
            environment["DEVECO_SDK_HOME"] = str(self.sdk_home)
        if self.hdc is not None:
            paths.append(self.hdc.parent)
        existing = environment.get("PATH", "")
        environment["PATH"] = os.pathsep.join([*(str(path) for path in paths), existing])
        return environment


def load_toolchain(config_path: Path | None = None) -> Toolchain:
    """Load the shared toolchain, retaining compatibility with device_capture aliases."""

    path = config_path or DEFAULT_CONFIG_PATH
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ToolchainError(f"无法读取调试环境配置：{path}") from exc
    if not isinstance(root, dict):
        raise ToolchainError("debug_agent.yaml 根节点必须是对象")
    environment = _mapping(root.get("environment"), "environment")
    legacy = _mapping(root.get("device_capture"), "device_capture")
    deveco_value = _first_text(
        environment.get("deveco_studio_home"),
        legacy.get("deveco_home"),
    )
    deveco_home = resolve_config_path(deveco_value) if deveco_value else None
    node_value = _first_text(environment.get("node_executable"), legacy.get("node_home"))
    node_executable, node_source = _resolve_node(node_value, deveco_home)
    return Toolchain(
        deveco_studio_home=deveco_home,
        node_executable=node_executable,
        node_source=node_source,
        java_home=_derived(deveco_home, "jbr"),
        sdk_home=_derived(deveco_home, "sdk"),
        hdc=_derived(deveco_home, "sdk/default/openharmony/toolchains/hdc.exe"),
        hvigor=_derived(deveco_home, "tools/hvigor/bin/hvigorw.js"),
        ohpm=_derived(deveco_home, "tools/ohpm/bin/ohpm.bat"),
        emulator=_derived(deveco_home, "tools/emulator/Emulator.exe"),
    )


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolchainError(f"debug_agent.yaml 的 {name} 必须是对象")
    return value


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_config_path(
    value: str,
    *,
    deveco_home: Path | None = None,
    base: Path = REPOSITORY_ROOT,
) -> Path:
    """Resolve a path and the single supported debug-config path reference."""

    expanded = value.strip()
    if DEVECO_HOME_REFERENCE in expanded:
        if not expanded.startswith(DEVECO_HOME_REFERENCE):
            raise ToolchainError(
                f"{DEVECO_HOME_REFERENCE} 只能用于路径开头：{value}"
            )
        if deveco_home is None:
            raise ToolchainError(
                f"路径引用了 {DEVECO_HOME_REFERENCE}，但未配置 DevEco Studio"
            )
        suffix = expanded.removeprefix(DEVECO_HOME_REFERENCE).lstrip("/\\")
        candidate = deveco_home / Path(suffix)
    else:
        candidate = Path(expanded).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _node_candidate(path: Path) -> Path:
    if path.is_dir():
        executable_name = "node.exe" if os.name == "nt" else "node"
        return path / executable_name
    return path


def _resolve_node(value: str | None, deveco_home: Path | None) -> tuple[Path, str]:
    if value is not None:
        configured = _node_candidate(resolve_config_path(value, deveco_home=deveco_home))
        if not configured.is_file():
            raise ToolchainError(f"显式配置的 Node.js 不存在：{configured}")
        return configured, "configured"
    if deveco_home is not None:
        bundled = _node_candidate(deveco_home / "tools" / "node")
        if bundled.is_file():
            return bundled, "deveco"
    discovered = shutil.which("node")
    if discovered:
        return Path(discovered).resolve(), "path"
    if deveco_home is not None:
        raise ToolchainError(
            f"DevEco Studio 未包含 Node.js，且系统 PATH 中也未找到 Node：{deveco_home}"
        )
    raise ToolchainError("未配置 Node.js 或 DevEco Studio，且系统 PATH 中未找到 Node")


def _derived(root: Path | None, relative: str) -> Path | None:
    return root / Path(relative) if root is not None else None
