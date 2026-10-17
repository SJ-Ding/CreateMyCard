from __future__ import annotations

import ast
import re
from typing import Any


class UpstreamToolError(RuntimeError):
    """正式 WebSocket 调用或业务包络解析失败。"""


_HEADER_PATTERN = re.compile(
    r"^type=(?P<type>'(?:\\.|[^'])*') "
    r"tool=(?P<tool>'(?:\\.|[^'])*') "
    r"operation=(?P<operation>'(?:\\.|[^'])*') "
    r"requestId=(?P<request_id>None|'(?:\\.|[^'])*')$"
)


def parse_legacy_stream_content(stream_content: str) -> dict[str, Any]:
    """安全解析正式路由返回的旧版 Pydantic 消息字符串。"""
    header_start = stream_content.find("type=")
    if header_start < 0:
        raise UpstreamToolError("final streamContent 缺少业务消息")
    legacy_content = stream_content[header_start:]
    try:
        header_text, data_and_tail = legacy_content.split(" data=", 1)
        data_text, status_and_tail = data_and_tail.rsplit(" status=", 1)
        status_text, error_code_and_tail = status_and_tail.split(" errorCode=", 1)
        error_code_text, error_text = error_code_and_tail.split(" error=", 1)
    except ValueError as exc:
        raise UpstreamToolError("final streamContent 格式不完整") from exc
    match = _HEADER_PATTERN.fullmatch(header_text)
    if match is None:
        raise UpstreamToolError("final streamContent 头部不受支持")
    try:
        return {
            "type": ast.literal_eval(match.group("type")),
            "tool": ast.literal_eval(match.group("tool")),
            "operation": ast.literal_eval(match.group("operation")),
            "requestId": ast.literal_eval(match.group("request_id")),
            "data": ast.literal_eval(data_text),
            "status": ast.literal_eval(status_text),
            "errorCode": ast.literal_eval(error_code_text),
            "error": ast.literal_eval(error_text),
        }
    except (SyntaxError, ValueError, TypeError) as exc:
        raise UpstreamToolError("final streamContent 含非法字面量") from exc

