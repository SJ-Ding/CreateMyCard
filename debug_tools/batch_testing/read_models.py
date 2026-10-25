"""运行查询的文件签名与历史执行索引，不重新导入或改写证据。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


def file_signature(path: Path) -> tuple[int, int] | None:
    result = None
    try:
        stat = path.stat()
        result = (stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        _LOGGER.debug("批跑查询文件不可用 %s: %s", path, exc)
    return result


def evidence_signature(run_dir: Path) -> tuple:
    signatures = []
    patterns = (
        "*/attempt_*/request.json",
        "*/attempt_*/result.json",
        "*/attempt_*/trace/source/trace.jsonl",
        "trace_blobs/*",
    )
    for pattern in patterns:
        for path in sorted(run_dir.glob(pattern)):
            signatures.append((str(path), file_signature(path)))
    return tuple(signatures)


class ExecutionIndex:
    """每轮列表只扫描一次目录，未变化的记录不重读；原文件保持原状。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[Path, tuple[object, dict[str, Any]]] = {}
        self.by_task: dict[str, list[dict[str, Any]]] = {}

    def refresh(self) -> None:
        current: dict[Path, tuple[object, dict[str, Any]]] = {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for path in self.root.glob("*.json"):
            signature = file_signature(path)
            cached = self.files.get(path)
            record = None
            if cached is not None and cached[0] == signature:
                record = cached[1]
            else:
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict):
                        record = value
                except (OSError, ValueError) as exc:
                    _LOGGER.warning("执行索引读取失败 %s: %s", path, exc)
            if record is None:
                continue
            current[path] = (signature, record)
            task_id = str(record.get("taskId") or "")
            grouped.setdefault(task_id, []).append(record)
        self.files = current
        self.by_task = grouped

    def records(self, task_id: str) -> list[dict[str, Any]]:
        return list(self.by_task.get(task_id, []))
