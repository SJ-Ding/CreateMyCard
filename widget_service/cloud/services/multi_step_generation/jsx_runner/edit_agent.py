from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

if "." in (__package__ or ""):
    from ..jsx_to_a2ui.exceptions import ParseError
    from ..jsx_to_a2ui.parser.jsx_ast import JSXElement
    from ..jsx_to_a2ui.parser.jsx_parser import extract_card_functions, parse_jsx
else:
    from jsx_to_a2ui.exceptions import ParseError
    from jsx_to_a2ui.parser.jsx_ast import JSXElement
    from jsx_to_a2ui.parser.jsx_parser import extract_card_functions, parse_jsx
from .resources import GenerationResources
from .validation import validate_generated_card
from .workflow import ConversionError, OrderedWorkflowState, _serialize_jsx


class EditAgentError(RuntimeError):
    """编辑 agent 的可预期失败。"""


@dataclass(frozen=True, slots=True)
class EditAgentConfig:
    max_model_calls: int = 2
    max_operations: int = 4


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
    raise EditAgentError(f"unknown nodeId: {node_id}")


def _find_parent(root: JSXElement, node_id: str) -> tuple[JSXElement, int]:
    if node_id == "n0":
        raise EditAgentError("root node cannot be moved")
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
                    raise EditAgentError("unsupported theme")
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
                    raise EditAgentError("property is not editable")
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
                    raise EditAgentError("move operation is malformed")
                parent, old_index = _find_parent(draft.root, node_id)
                if parent_id != ".".join(node_id.split(".")[:-1]):
                    raise EditAgentError("cross-parent move is not supported")
                if not 0 <= to_index < len(parent.children):
                    raise EditAgentError("target index is outside the parent")
                child = parent.children.pop(old_index)
                parent.children.insert(to_index, child)
                changed = {"node": node_id, "parent": parent_id, "toIndex": to_index}
            elif kind in {"add_data_field", "remove_data_field"}:
                field_ref = operation.get("fieldRef")
                if not isinstance(field_ref, str) or not field_ref.strip():
                    raise EditAgentError("fieldRef is required")
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
                    raise EditAgentError("fieldRef is not present in the current data context")
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
                    raise EditAgentError(
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
                        raise EditAgentError("data field is already displayed")
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
                        raise EditAgentError("data field is not displayed")
                    items = kept
                node.props["items"] = items
                changed = {"node": node_id, "fieldRef": field_ref, "kind": kind}
            else:
                raise EditAgentError(f"unsupported operation: {kind!r}")
        except (EditAgentError, ValueError) as exc:
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
    [list[dict[str, Any]], list[dict[str, Any]]],
    Awaitable[dict[str, Any]],
]
OperationExecutor = Callable[[list[dict[str, Any]], EditDraft], Awaitable[dict[str, Any]]]
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
                    "The agent executes them sequentially and validates the final candidate."
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
        raise EditAgentError("编辑模型必须恰好调用一个工具")
    call = calls[0]
    if not isinstance(call, dict):
        raise EditAgentError("编辑工具调用格式非法")
    function = call.get("function")
    if not isinstance(function, dict):
        raise EditAgentError("编辑工具调用缺少 function")
    name = function.get("name")
    arguments = function.get("arguments")
    if not isinstance(name, str) or not isinstance(arguments, str):
        raise EditAgentError("编辑工具调用缺少名称或参数")
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        raise EditAgentError("编辑工具参数不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise EditAgentError("编辑工具参数必须是对象")
    return str(call.get("id") or "edit_call"), {"name": name, "arguments": parsed}


class EditAgent:
    """两轮编辑调度器；执行器和模型客户端均可替换，便于单元测试。"""

    def __init__(self, config: EditAgentConfig | None = None) -> None:
        self.config = config or EditAgentConfig()

    async def run(
        self,
        *,
        messages: list[dict[str, Any]],
        build_surface: SurfaceBuilder,
        request_model: ModelRequest,
        execute_operations: OperationExecutor,
    ) -> dict[str, Any]:
        started = time.monotonic()
        draft = EditDraft()
        tools = edit_tools()
        messages = list(messages)
        model_calls = 0
        trace: list[dict[str, Any]] = []

        messages.append(
            {
                "role": "user",
                "content": json.dumps(build_surface(draft), ensure_ascii=False),
            }
        )
        while model_calls < self.config.max_model_calls:
            model_calls += 1
            response = await request_model(messages, tools)
            trace.append(
                {
                    "stage": "model",
                    "call": model_calls,
                }
            )
            call_id, call = _call_payload(response)
            messages.append({"role": "assistant", "tool_calls": [response["tool_calls"][0]]})

            if call["name"] == "inspect_edit_surface":
                surface = build_surface(draft)
                result = {"status": "ok", "revision": draft.revision, "surface": surface}
                messages.append(_tool_message(call_id, result))
                if model_calls >= self.config.max_model_calls:
                    raise EditAgentError("编辑上下文读取已耗尽模型请求次数")
                messages.append(
                    {"role": "user", "content": "请基于刚才的编辑界面调用 execute_edit_sequence。"}
                )
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
                result = await execute_operations(operations, draft)
            messages.append(_tool_message(call_id, result))
            trace.append(
                {
                    "stage": "execute",
                    "result": result,
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
            if model_calls >= self.config.max_model_calls:
                raise EditAgentError("编辑操作未通过且模型请求次数已耗尽")
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "status": "repair_required",
                            "revision": draft.revision,
                            "feedback": result,
                            "instruction": "只修复失败或未执行的操作，不要重复已成功操作。",
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )

        raise EditAgentError("编辑模型请求次数已耗尽")


class JsxEditAgent:
    """独立的 JSX 二次编辑 Agent；不复用创建 Agent 的生成循环。"""

    def __init__(
        self,
        *,
        model: str,
        client: Any,
        max_tokens: int = 4096,
        browser_validation: bool = False,
        validation_enabled: bool = True,
        validate_dynamic_values: bool = True,
        enable_dynamic_data_binding: bool = True,
        edit_max_model_calls: int = 2,
        edit_max_operations: int = 4,
        resources: GenerationResources | None = None,
        verbose: bool = True,
    ) -> None:
        if edit_max_model_calls < 1:
            raise ValueError("edit_max_model_calls must be positive")
        if not 1 <= edit_max_operations <= 4:
            raise ValueError("edit_max_operations must be between 1 and 4")
        self.model = model
        self.client = client
        self.max_tokens = max_tokens
        self.browser_validation = browser_validation
        self.validation_enabled = validation_enabled
        self.validate_dynamic_values = validate_dynamic_values
        self.enable_dynamic_data_binding = enable_dynamic_data_binding
        self.edit_max_model_calls = edit_max_model_calls
        self.edit_max_operations = edit_max_operations
        self.resources = resources or GenerationResources()
        self.verbose = verbose

    async def render(
        self,
        task: dict[str, Any],
        component_name: str,
        previous_jsx: str,
        compile_context: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """执行基于历史 JSX 的编辑入口，创建入口继续使用 render。"""
        if not previous_jsx.strip():
            raise ValueError("previous_jsx must be a non-empty string")
        started = time.monotonic()
        validation_enabled = getattr(self, "validation_enabled", True)
        state = OrderedWorkflowState(
            component_name,
            resources=getattr(self, "resources", None) or GenerationResources(),
            compile_context=compile_context,
            prompt_task=task,
            defer_browser_validation=validation_enabled,
            validation_enabled=validation_enabled,
            validate_layout_budget=True,
            validate_dynamic_values=getattr(self, "validate_dynamic_values", True),
            enable_dynamic_data_binding=getattr(self, "enable_dynamic_data_binding", True),
        )
        source = previous_jsx.strip()
        try:
            parsed_root = parse_jsx(source)
        except (ConversionError, ParseError):
            cards = extract_card_functions(source)
            if len(cards) != 1:
                raise ValueError(
                    "previous_jsx must contain exactly one editable Card expression"
                ) from None
            parsed_root = next(iter(cards.values()))
            source = _serialize_jsx(parsed_root)
        jsx_draft = JsxDraft(
            source=source,
            root=parsed_root,
            decision=task.get("decision") if isinstance(task.get("decision"), dict) else {},
            snapshot=build_jsx_surface(source),
            data_context=compile_context or {},
        )
        system = (
            "你是 JSX 卡片编辑 Agent。只能调用 inspect_edit_surface 或 execute_edit_sequence。"
            "一次 execute_edit_sequence 最多提交 4 个有序原子操作；不要输出 JSX、解释或 Markdown。"
            "只能使用编辑界面提供的 nodeId、属性和槽位；跨父容器移动、任意 CSS、"
            "任意颜色和新增数据源均拒绝。"
            "操作成功后保持已完成修改；修复时不要重复已成功操作。"
        )
        user = json.dumps(
            {
                "editInstruction": task.get("userQuery", ""),
                "card": {
                    "size": task.get("size"),
                    "decision": jsx_draft.decision,
                    "surface": jsx_draft.snapshot,
                },
                "dataContext": compile_context or {},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        async def request_model(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
        ) -> dict[str, Any]:
            request = {
                "model": self.model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "max_tokens": min(self.max_tokens, 1000),
            }
            response = await self.client.chat.completions.create(**request)
            choice = response.choices[0]
            message = choice.message
            calls = getattr(message, "tool_calls", ())
            payload_calls = []
            for call in calls:
                function = getattr(call, "function", None)
                payload_calls.append(
                    {
                        "id": getattr(call, "id", "edit_call"),
                        "type": "function",
                        "function": {
                            "name": getattr(function, "name", ""),
                            "arguments": getattr(function, "arguments", "{}"),
                        },
                    }
                )
            return {"tool_calls": payload_calls}

        def build_surface(_draft: EditDraft) -> dict[str, Any]:
            return jsx_draft.snapshot

        async def execute_operations(
            operations: list[dict[str, Any]],
            current: EditDraft,
        ) -> dict[str, Any]:
            result = apply_jsx_operations(jsx_draft, operations)
            if result.get("status") != "applied":
                applied = result.get("applied", [])
                if applied:
                    current.revision += 1
                    current.applied_operations.extend(applied)
                    result["revision"] = current.revision
                current.failed_operation = result.get("failedOperation")
                return result
            current.revision += 1
            current.applied_operations.extend(result.get("operations", []))
            decision = jsx_draft.decision
            validation = state.submit_card_jsx(
                jsx_draft.source,
                decision,
                task.get("coverage", []),
                task.get("unmetRequirements", []),
            )
            if not validation.get("ok"):
                return {
                    "status": "repair_required",
                    "revision": current.revision,
                    "operations": result.get("operations", []),
                    "findings": validation.get("findings", []),
                    "error": validation.get("error", "JSX 编辑校验失败"),
                    "commitAllowed": False,
                }
            if getattr(self, "browser_validation", False) and validation_enabled:
                try:
                    browser_report = await validate_generated_card(
                        source=jsx_draft.source,
                        task=task,
                        component_name=component_name,
                        decision=decision,
                        browser=True,
                        infrastructure_retries=0,
                    )
                except Exception as exc:
                    return {
                        "status": "repair_required",
                        "revision": current.revision,
                        "operations": result.get("operations", []),
                        "code": "BROWSER_VALIDATION_FAILED",
                        "error": str(exc),
                        "commitAllowed": False,
                    }
                if not browser_report.get("ok", True):
                    return {
                        "status": "repair_required",
                        "revision": current.revision,
                        "operations": result.get("operations", []),
                        "code": "BROWSER_VALIDATION_FAILED",
                        "findings": browser_report.get("findings", []),
                        "commitAllowed": False,
                    }
                state.apply_rendered_layout(browser_report.get("renderedLayout"))
            if state.pending_submission is not None:
                state.accept_pending_submission()
            accepted = state.submission
            if accepted is None:
                return {"status": "repair_required", "code": "NO_EDIT_SUBMISSION"}
            return {
                "status": "committed",
                "revision": current.revision,
                "operations": result.get("operations", []),
                "source": accepted.source,
                "jsx": accepted.jsx,
                "a2ui": accepted.messages,
                "decision": accepted.decision,
                "compile_context": accepted.compile_context,
                "browser_validation": (
                    "passed" if getattr(self, "browser_validation", False) else "skipped"
                ),
            }

        edit_agent = EditAgent(
            EditAgentConfig(
                max_model_calls=self.edit_max_model_calls,
                max_operations=self.edit_max_operations,
            )
        )
        result = await edit_agent.run(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            build_surface=build_surface,
            request_model=request_model,
            execute_operations=execute_operations,
        )
        committed = result.get("result", {})
        committed["turns"] = result.get("model_calls", 0)
        committed["elapsed_seconds"] = time.monotonic() - started
        committed["repair_calls"] = max(0, int(result.get("model_calls", 0)) - 1)
        committed["failed_submissions"] = 0
        committed["warnings"] = []
        if trace_callback is not None:
            trace_callback({"status": "completed", "turn_trace": result.get("trace", [])})
        return committed

