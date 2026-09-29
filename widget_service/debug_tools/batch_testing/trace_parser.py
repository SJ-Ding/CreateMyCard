"""按 UID 读取并归一化微服务生成轨迹。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

_UID_PATTERN = re.compile(
    r"^(?P<task_type>[A-Za-z]+)-(?P<identifier>[A-Za-z0-9]{8})-"
    r"(?P<request_sequence>\d{5})-(?P<retry_count>\d{3})$"
)
TraceLineParser = Callable[[dict[str, Any], int], dict[str, Any]]


def _model_trace_record(payload: dict[str, Any], sequence: int) -> dict[str, Any]:
    model_output = payload.get("model_output")
    if not isinstance(model_output, str):
        raise ValueError("model_output 必须是字符串")
    return {
        "kind": "model_output",
        "sequence": sequence,
        "summary": f"模型输出 #{sequence}",
        "data": {"modelOutput": model_output},
    }


def _generic_trace_record(payload: dict[str, Any], sequence: int) -> dict[str, Any]:
    kind = payload.get("type") or payload.get("kind") or "json_record"
    return {
        "kind": str(kind),
        "sequence": sequence,
        "summary": f"记录 #{sequence}",
        "data": payload,
    }


class TraceParserRegistry:
    """按轨迹文件名注册解析器；未知文件仍由通用 JSONL 解析器处理。"""

    def __init__(self) -> None:
        self._parsers: dict[str, TraceLineParser] = {
            "model_trace.jsonl": _model_trace_record,
        }

    def register(self, file_name: str, parser: TraceLineParser) -> None:
        self._parsers[file_name] = parser

    def parse_file(self, path: Path) -> tuple[dict[str, Any], list[str]]:
        parser = self._parsers.get(path.name, _generic_trace_record)
        records: list[dict[str, Any]] = []
        warnings: list[str] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return (
                {"name": path.name, "recordCount": 0, "records": []},
                [f"{path.name}: 读取失败（{type(exc).__name__}）"],
            )
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("JSONL 根节点必须是对象")
                records.append(parser(payload, len(records) + 1))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                warnings.append(
                    f"{path.name}:{line_number}: 解析失败（{type(exc).__name__}: {exc}）"
                )
        return (
            {"name": path.name, "recordCount": len(records), "records": records},
            warnings,
        )


class TraceBundleReader:
    """将一个请求 UID 对应目录中的所有轨迹文件汇总为稳定结构。"""

    def __init__(
        self,
        trace_root: Path,
        registry: TraceParserRegistry | None = None,
    ) -> None:
        self.trace_root = trace_root.resolve()
        self.registry = registry or TraceParserRegistry()

    def directory_for_uid(self, uid: str) -> Path | None:
        match = _UID_PATTERN.fullmatch(uid)
        if match is None:
            return None
        return self.trace_root.joinpath(
            match.group("task_type"),
            match.group("identifier"),
            match.group("request_sequence"),
            match.group("retry_count"),
        )

    def read(self, uid: str) -> dict[str, Any]:
        trace_directory = self.directory_for_uid(uid)
        if trace_directory is None:
            return self._empty(uid, "parse_error", ["UID 格式不合法"])
        if not trace_directory.is_dir():
            return self._empty(uid, "missing", ["未找到对应 trace 目录"])
        try:
            paths = sorted(path for path in trace_directory.iterdir() if path.is_file())
        except OSError as exc:
            return self._empty(
                uid,
                "parse_error",
                [f"trace 目录读取失败（{type(exc).__name__}）"],
            )
        if not paths:
            return self._empty(uid, "missing", ["trace 目录中没有记录文件"])
        sections: list[dict[str, Any]] = []
        warnings: list[str] = []
        for path in paths:
            section, section_warnings = self.registry.parse_file(path)
            sections.append(section)
            warnings.extend(section_warnings)
        record_count = sum(int(section.get("recordCount", 0)) for section in sections)
        status = "parse_error" if warnings else "available"
        return {
            "uid": uid,
            "status": status,
            "recordCount": record_count,
            "sections": sections,
            "warnings": warnings,
        }

    @staticmethod
    def _empty(uid: str, status: str, warnings: list[str]) -> dict[str, Any]:
        return {
            "uid": uid,
            "status": status,
            "recordCount": 0,
            "sections": [],
            "warnings": warnings,
        }
