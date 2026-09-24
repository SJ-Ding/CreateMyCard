from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

_SENSITIVE = re.compile(
    r"(access[_-]?key|api[_-]?key|authorization|secret|signature|token|password)", re.I
)


def configure_debug_logging(level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("debug_agent")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[DebugAgent] %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def safe_value(value: Any, *, trace: bool = False, limit: int = 160) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            result[str(key)] = (
                "<redacted>"
                if _SENSITIVE.search(str(key))
                else safe_value(item, trace=trace, limit=limit)
            )
        return result
    if isinstance(value, list):
        return [safe_value(v, trace=trace, limit=limit) for v in value[:20]]
    if _SENSITIVE.search(str(value)):
        return "<redacted>"
    if isinstance(value, str):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        if trace:
            return {"text": value[:limit], "chars": len(value), "sha256_12": digest}
        if len(value) <= limit:
            return value
        return {"chars": len(value), "sha256_12": digest}
    return value


class DebugLogger:
    def __init__(self, *, skill: str = "", trace: bool = False, limit: int = 160) -> None:
        self.logger = configure_debug_logging("DEBUG" if trace else "INFO")
        self.skill = skill
        self.trace = trace
        self.limit = limit

    def event(self, name: str, **fields: Any) -> None:
        payload = {"event": name, "skill": self.skill}
        for key, value in fields.items():
            payload[key] = safe_value(value, trace=self.trace, limit=self.limit)
        self.logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def timed(self, name: str, **fields: Any) -> tuple[float, str, dict[str, Any]]:
        return time.perf_counter(), name, fields

    def finish(self, started: tuple[float, str, dict[str, Any]], **fields: Any) -> None:
        started_at, name, initial = started
        self.event(
            name,
            **initial,
            **fields,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )

