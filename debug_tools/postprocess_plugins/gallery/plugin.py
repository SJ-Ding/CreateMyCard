"""浏览器渲染画廊后处理插件实现。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from debug_tools.runtime_environment import Toolchain, load_optional_toolchain, load_toolchain
from debug_tools.subprocesses import run_node_script

GalleryCapture = Callable[[str, Path], Awaitable[dict[str, Any]]]


class BatchGalleryManager:
    """为截图插件提供浏览器采集与运行目录解析。"""

    def __init__(
        self,
        output_root: Path,
        capture_base_url: str,
        *,
        capture: GalleryCapture | None = None,
        toolchain: Toolchain | None = None,
    ) -> None:
        self.output_root = output_root.resolve()
        self.capture_base_url = capture_base_url.rstrip("/")
        self.capture = capture or self._capture_with_browser
        self.toolchain = toolchain or load_optional_toolchain()

    async def _capture_with_browser(self, capture_url: str, output_dir: Path) -> dict[str, Any]:
        toolchain = self.toolchain or load_toolchain()
        script = Path(__file__).resolve().parents[2] / "scripts" / "capture_batch_gallery.mjs"
        if not script.is_file():
            raise RuntimeError("画廊截图脚本不存在")
        await run_node_script(
            toolchain.node_executable,
            script,
            ["--url", capture_url, "--output", str(output_dir)],
            cwd=script.parents[1],
            env=toolchain.environment(),
            timeout=900.0,
            label="画廊截图",
        )
        return self._read_json(output_dir / "capture.json")

    def _safe_run_dir(self, run_id: str) -> Path:
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        if not run_id or any(character not in allowed for character in run_id):
            raise KeyError(run_id)
        run_dir = (self.output_root / run_id).resolve()
        if not run_dir.is_relative_to(self.output_root) or not run_dir.is_dir():
            raise KeyError(run_id)
        return run_dir

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取画廊输入: {path.name}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"画廊输入不是对象: {path.name}")
        return value
