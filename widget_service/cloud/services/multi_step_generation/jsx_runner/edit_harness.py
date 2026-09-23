from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..jsx_to_a2ui.parser.jsx_ast import JSXElement
from ..jsx_to_a2ui.parser.jsx_parser import parse_jsx
from .workflow import _serialize_jsx


class EditHarnessError(RuntimeError):
    """编辑 harness 的可预期失败。"""


@dataclass(frozen=True, slots=True)
class EditHarnessConfig:
    deadline_seconds: float = 15.0
    max_model_calls: int = 2
    max_operations: int = 4
    model_timeout_seconds: float = 4.5
    repair_timeout_seconds: float = 4.0
    minimum_repair_budget_seconds: float = 7.5


@dataclass(slots=True)
class EditDraft:
    revision: int = 0
    applied_operations: list[dict[str, Any]] = field(default_factory=list)
    failed_operation: dict[str, Any] | None = None


@dataclass(slots=True)
class JsxDraft:
    source: str
    root: JSXElement
    decision: dict[str, Any]
    snapshot: dict[str, Any]
    data_context: dict[str, Any] = field(default_factory=dict)


def _walk(node: JSXElement, node_id: str = "n0") -> list[tuple[str, JSXElement, str | None]]:
    result: list[tuple[str, JSXElement, str | None]] = [(node_id, node, None)]
    for index, child in enumerate(node.children):
        if not isinstance(child, JSXElement):
            continue
        child_id = f"{node_id}.{index}"
        result.extend(_walk(child, child_id))
    return result


def build_jsx_surface(source: str, *, decision: dict[str, Any] | None = None) -> dict[str, Any]:
    root = parse_jsx(source.strip())
    nodes = []
    for node_id, node, _parent in _walk(root):
        nodes.append(
            {
                "nodeId": node_id,
                "tag": node.tag,
                "props": sorted(node.props),
                "childCount": sum(isinstance(child, JSXElement) for child in node.children),
                "editableProps": [
                    prop
                    for prop in ("appearance", "direction", "gap", "flex", "width", "height")
                    if prop in node.props
                ],
            }
        )
    return {
        "root": root.tag,
        "decision": decision or {},
        "nodes": nodes,
        "allowedOperations": [
            "set_theme",
            "set_prop",
            "move_within_parent",
            "add_data_field",
            "remove_data_field",
        ],
    }


def _find_node(root: JSXElement, node_id: str) -> JSXElement:
    for current_id, node, _parent in _walk(root):
        if current_id == node_id:
            return node
    raise EditHarnessError(f"unknown nodeId: {node_id}")


def _find_parent(root: JSXElement, node_id: str) -> tuple[JSXElement, int]:
    if node_id == "n0":
        raise EditHarnessError("root node cannot be moved")
    parts = node_id.split(".")
    parent_id = ".".join(parts[:-1])
    index = int(parts[-1])
    return _find_node(root, parent_id), index


def apply_jsx_operations(
    draft: JsxDraft,
    operations: list[dict[str, Any]],
    *,
    max_operations: int = 4,
) -> dict[str, Any]:
    if not 1 <= len(operations) <= max_operations:
        return {"status": "failed", "code": "INVALID_OPERATION_BATCH"}
    applied: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict):
            return {"status": "failed", "code": "INVALID_OPERATION"}
        kind = operation.get("kind")
        try:
            if kind == "set_theme":
                theme = operation.get("value")
                if theme not in {
                    "solid-blue",
                    "solid-orange",
                    "solid-green",
                    "solid-cyan",
                    "solid-purple",
                    "orb-orange",
                    "orb-blue",
                    "orb-purple",
                    "orb-green",
                }:
                    raise EditHarnessError("unsupported theme")
                draft.root.props["appearance"] = theme
                changed = {"node": "n0", "property": "appearance", "value": theme}
            elif kind == "set_prop":
                node_id = operation.get("target")
                prop = operation.get("property")
                value = operation.get("value")
                if not isinstance(node_id, str) or prop not in {
                    "appearance",
                    "direction",
                    "gap",
                    "flex",
                    "width",
                    "height",
                }:
                    raise EditHarnessError("property is not editable")
                node = _find_node(draft.root, node_id)
                node.props[prop] = value
                changed = {"node": node_id, "property": prop, "value": value}
            elif kind == "move_within_parent":
                node_id = operation.get("target")
                parent_id = operation.get("parent")
                to_index = operation.get("toIndex")
                if (
                    not isinstance(node_id, str)
                    or not isinstance(parent_id, str)
                    or not isinstance(to_index, int)
                ):
                    raise EditHarnessError("move operation is malformed")
                parent, old_index = _find_parent(draft.root, node_id)
                if parent_id != ".".join(node_id.split(".")[:-1]):
                    raise EditHarnessError("cross-parent move is not supported")
                if not 0 <= to_index < len(parent.children):
                    raise EditHarnessError("target index is outside the parent")
                child = parent.children.pop(old_index)
                parent.children.insert(to_index, child)
                changed = {"node": node_id, "parent": parent_id, "toIndex": to_index}
            elif kind in {"add_data_field", "remove_data_field"}:
                field_ref = operation.get("fieldRef")
                if not isinstance(field_ref, str) or not field_ref.strip():
                    raise EditHarnessError("fieldRef is required")
                fields = draft.data_context.get("data", [])
                field = next(
                    (
                        item
                        for item in fields
                        if isinstance(item, dict) and item.get("id") == field_ref
                    ),
                    None,
                )
                if field is None:
                    raise EditHarnessError("fieldRef is not present in the current data context")
                target_id = operation.get("target")
                candidates = [
                    (node_id, node)
                    for node_id, node, _parent in _walk(draft.root)
                    if node.tag in {"SecondaryBody", "TableText"}
                    and isinstance(node.props.get("items"), list)
                ]
                if isinstance(target_id, str):
                    candidates = [
                        (node_id, node)
                        for node_id, node in candidates
                        if node_id == target_id
                    ]
                if len(candidates) != 1:
                    raise EditHarnessError(
                        "data field requires exactly one compatible items component"
                    )
                node_id, node = candidates[0]
                items = list(node.props["items"])
                if kind == "add_data_field":
                    if any(
                        isinstance(item, dict)
                        and isinstance(item.get("dataIds"), dict)
                        and item["dataIds"].get("value") == field_ref
                        for item in items
                    ):
                        raise EditHarnessError("data field is already displayed")
                    value = field.get("value")
                    items.append(
                        {
                            "label": operation.get("label", ""),
                            "value": "" if value is None else str(value),
                            "dataIds": {"value": field_ref},
                        }
                    )
                else:
                    kept = [
                        item
                        for item in items
                        if not (
                            isinstance(item, dict)
                            and isinstance(item.get("dataIds"), dict)
                            and item["dataIds"].get("value") == field_ref
                        )
                    ]
                    if len(kept) == len(items):
                        raise EditHarnessError("data field is not displayed")
                    items = kept
                node.props["items"] = items
                changed = {"node": node_id, "fieldRef": field_ref, "kind": kind}
            else:
                raise EditHarnessError(f"unsupported operation: {kind!r}")
        except (EditHarnessError, ValueError) as exc:
            draft.source = _serialize_jsx(draft.root)
            draft.snapshot = build_jsx_surface(draft.source, decision=draft.decision)
            return {
                "status": "repair_required",
                "code": "OPERATION_REJECTED",
                "failedOperation": operation,
                "error": str(exc),
                "applied": applied,
                "surface": draft.snapshot,
            }
        applied.append({"opId": operation.get("opId"), "status": "applied", "changed": changed})
    draft.source = _serialize_jsx(draft.root)
    draft.snapshot = build_jsx_surface(draft.source, decision=draft.decision)
    return {
        "status": "applied",
        "revision": draft.snapshot.get("revision", 0),
        "operations": applied,
        "source": draft.source,
        "surface": draft.snapshot,
    }


ModelRequest = Callable[
    [list[dict[str, Any]], list[dict[str, Any]], float],
    Awaitable[dict[str, Any]],
]
OperationExecutor = Callable[[list[dict[str, Any]], EditDraft, float], Awaitable[dict[str, Any]]]
SurfaceBuilder = Callable[[EditDraft], dict[str, Any]]


def edit_tools() -> list[dict[str, Any]]:
    """返回编辑模型可见的两个工具，避免暴露创建链路的资源读取工具。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "inspect_edit_surface",
                "description": "Read the smallest relevant editable card surface.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string"},
                        "target": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "execute_edit_sequence",
                "description": (
                    "Submit at most four ordered atomic edit operations. "
                    "The harness executes them sequentially and validates the final candidate."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "baseRevision": {"type": "integer", "minimum": 0},
                        "operations": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": {"type": "object"},
                        },
                        "finish": {"type": "string", "enum": ["commit_if_valid", "continue"]},
                    },
                    "required": ["baseRevision", "operations"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _tool_message(call_id: str, result: dict[str, Any]) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
    }


def _call_payload(response: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    calls = response.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        raise EditHarnessError("编辑模型必须恰好调用一个工具")
    call = calls[0]
    if not isinstance(call, dict):
        raise EditHarnessError("编辑工具调用格式非法")
    function = call.get("function")
    if not isinstance(function, dict):
        raise EditHarnessError("编辑工具调用缺少 function")
    name = function.get("name")
    arguments = function.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, str):
        raise EditHarnessError("编辑工具调用缺少名称或参数")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise EditHarnessError("编辑工具参数不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise EditHarnessError("编辑工具参数必须是对象")
    return str(call.get("id") or "edit_call"), {"name": name, "arguments": parsed}


class EditHarness:
    """两轮编辑调度器；执行器和模型客户端均可替换，便于单元测试。"""

    def __init__(self, config: EditHarnessConfig | None = None) -> None:
        self.config = config or EditHarnessConfig()

    async def run(
        self,
        *,
        messages: list[dict[str, Any]],
        build_surface: SurfaceBuilder,
        request_model: ModelRequest,
        execute_operations: OperationExecutor,
    ) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + self.config.deadline_seconds
        draft = EditDraft()
        tools = edit_tools()
        messages = list(messages)
        model_calls = 0
        trace: list[dict[str, Any]] = []

        def remaining() -> float:
            return max(0.0, deadline - time.monotonic())

        def ensure_budget(minimum: float = 0.05) -> None:
            if remaining() < minimum:
                raise EditHarnessError("编辑超过时间预算")

        messages.append(
            {
                "role": "user",
                "content": json.dumps(build_surface(draft), ensure_ascii=False),
            }
        )
        while model_calls < self.config.max_model_calls:
            ensure_budget()
            model_calls += 1
            timeout = (
                self.config.model_timeout_seconds
                if model_calls == 1
                else self.config.repair_timeout_seconds
            )
            timeout = min(timeout, remaining())
            try:
                response = await asyncio.wait_for(
                    request_model(messages, tools, timeout),
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                raise EditHarnessError("编辑模型请求超时") from exc
            trace.append(
                {
                    "stage": "model",
                    "call": model_calls,
                    "remainingMs": int(remaining() * 1000),
                }
            )
            call_id, call = _call_payload(response)
            messages.append({"role": "assistant", "tool_calls": [response["tool_calls"][0]]})

            if call["name"] == "inspect_edit_surface":
                surface = build_surface(draft)
                result = {"status": "ok", "revision": draft.revision, "surface": surface}
                messages.append(_tool_message(call_id, result))
                if model_calls >= self.config.max_model_calls:
                    raise EditHarnessError("编辑上下文读取已耗尽模型请求次数")
                messages.append({"role": "user", "content": "请基于刚才的编辑界面调用 execute_edit_sequence。"})
                continue

            if call["name"] != "execute_edit_sequence":
                result = {"status": "failed", "code": "UNSUPPORTED_EDIT_TOOL"}
                messages.append(_tool_message(call_id, result))
                continue

            arguments = call["arguments"]
            operations = arguments.get("operations")
            base_revision = arguments.get("baseRevision")
            if (
                not isinstance(operations, list)
                or not 1 <= len(operations) <= self.config.max_operations
            ):
                result = {"status": "failed", "code": "INVALID_OPERATION_BATCH"}
            elif base_revision != draft.revision:
                result = {
                    "status": "failed",
                    "code": "STALE_REVISION",
                    "revision": draft.revision,
                }
            else:
                result = await execute_operations(operations, draft, remaining())
            messages.append(_tool_message(call_id, result))
            trace.append(
                {
                    "stage": "execute",
                    "result": result,
                    "remainingMs": int(remaining() * 1000),
                }
            )
            if result.get("status") == "committed":
                return {
                    "status": "committed",
                    "result": result,
                    "model_calls": model_calls,
                    "elapsed_seconds": time.monotonic() - started,
                    "trace": trace,
                }
            if (
                model_calls >= self.config.max_model_calls
                or remaining() < self.config.minimum_repair_budget_seconds
            ):
                raise EditHarnessError("编辑操作未通过且剩余时间不足以修复")
            messages.append({
                "role": "user",
                "content": json.dumps(
                    {
                        "status": "repair_required",
                        "revision": draft.revision,
                        "feedback": result,
                        "remainingMs": int(remaining() * 1000),
                        "instruction": "只修复失败或未执行的操作，不要重复已成功操作。",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            })

        raise EditHarnessError("编辑模型请求次数已耗尽")
