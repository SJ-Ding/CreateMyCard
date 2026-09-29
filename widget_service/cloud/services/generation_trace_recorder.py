# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations
import json
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from app.logger import logger

_MODULE = "[Generation Trace]"
_UID_PATTERN = re.compile(
    r"^(?P<task_type>[A-Za-z]+)-(?P<identifier>[A-Za-z0-9]{8})-"
    r"(?P<request_sequence>\d{5})-(?P<retry_count>\d{3})$"
)
_TRACE_FILE_NAME = "model_output.jsonl"
_MODEL_TRACE_FILE_NAME = "model_trace.jsonl"

class TraceRecordAbstract:
    """轨迹记录抽象类，提供可替换的记录实现。"""
    file_name: str = _TRACE_FILE_NAME

    def to_jsonl(self) -> str:
        """将记录格式化为一行紧凑 JSONL。"""
        raise NotImplementedError("TraceRecordAbstract.to_jsonl must be implemented")

class _RequestTraceWriter:
    """负责一次生成请求的文件追加操作。"""

    def __init__(self, trace_directory: Path) -> None:
        self._trace_directory = trace_directory
        self._write_lock = Lock()

    def write(self, record: TraceRecordAbstract) -> None:
        try:
            jsonl = record.to_jsonl()
            output_path = self._trace_directory / record.file_name
            with self._write_lock:
                self._trace_directory.mkdir(parents=True, exist_ok=True)
                with output_path.open("a", encoding="utf-8", newline="") as output_file:
                    output_file.write(jsonl)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                f"{_MODULE} trace_record_failed exception_type={type(exc).__name__}"
            )



class GenerationTraceRecorder:
    """为每次生成请求提供独立的轨迹记录器。"""
    enabled: bool = False
    uid: str | None = None
    writer: _RequestTraceWriter | None = None

    def bind_request(
        self,
        uid: str,
        *,
        enabled: bool,
        trace_root: Path,
    ):
        """按请求 UID 绑定写入器；功能关闭或 UID 非法时绑定空实现。"""
        self.uid = uid
        match = _UID_PATTERN.fullmatch(uid)
        self.writer = None
        if not enabled:
            logger.info(f"{_MODULE} trace_binding_skipped reason=disabled")
            return
        if not match:
            logger.warning(f"{_MODULE} trace_binding_skipped reason=invalid_uid")
            return
        self.enabled = True

        try:
            trace_dir = trace_root.joinpath(
                    match.group("task_type"),
                    match.group("identifier"),
                    match.group("request_sequence"),
                    match.group("retry_count"),
                ).resolve()
            self.writer = _RequestTraceWriter(trace_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                f"{_MODULE} trace_binding_skipped reason=invalid_path "
                f"exception_type={type(exc).__name__}"
            )
            self.writer = None
            return

    def write(self, record: TraceRecordAbstract) -> None:
        if not self.enabled or self.writer is None:
            return
        if not isinstance(record, TraceRecordAbstract):
            logger.warning(f"{_MODULE} trace_record_skipped reason=invalid_record_type")
            return
        try:
            self.writer.write(record)
        except Exception as exc:
            logger.warning(
                f"{_MODULE} trace_record_failed exception_type={type(exc).__name__}"
            )

    def record_model_output(self, model_output: str) -> None:
        """记录模型输出轨迹。"""
        record = ModelOutputTraceRecord(model_output=model_output)
        self.write(record)


@dataclass(frozen=True)
class ModelOutputTraceRecord(TraceRecordAbstract):
    """模型输出轨迹中的一条记录。"""
    file_name: str = _MODEL_TRACE_FILE_NAME
    model_output: str = ""

    def to_jsonl(self) -> str:
        """将记录格式化为一行紧凑 JSONL。"""
        payload = {"model_output": self.model_output}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"

