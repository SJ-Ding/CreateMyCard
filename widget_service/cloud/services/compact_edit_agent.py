# -*- coding: utf-8 -*-
"""Bounded intent planning for Compact DSL edits.

The planner is deliberately kept separate from the DSL generator.  It only
returns a small, whitelisted edit plan; the normal Design Compact model still
produces a complete token and the existing validators remain authoritative.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from config.config import get_settings
from custom.a2ui_model_client import A2UIModelClient
from custom.model_runtime import ModelExecutionRuntime
from models.generation import ModelRequestContext
from services.compact_dsl_a2ui_converter import (
    ComponentRow,
    DataRow,
    parse_compact_dsl_rows,
)

_RAW_JSON_PROFILE = {"id": "compact-edit-intent", "format": "raw-json"}
_DYNAMIC_MARKERS = ("${", "{{", "$__dataModel", "$item")
_STYLE_FIELDS = frozenset(
    {
        "alignContent",
        "alignItems",
        "alignSelf",
        "aspectRatio",
        "backgroundColor",
        "borderColor",
        "borderRadius",
        "borderWidth",
        "color",
        "constraintSize",
        "fillColor",
        "fontColor",
        "fontSize",
        "fontWeight",
        "height",
        "justifyContent",
        "layoutWeight",
        "margin",
        "maxFontSize",
        "maxHeight",
        "maxLines",
        "maxWidth",
        "minFontSize",
        "minHeight",
        "minWidth",
        "objectFit",
        "opacity",
        "padding",
        "space",
        "textAlign",
        "textOverflow",
        "visibility",
        "width",
    }
)
_PROTECTED_FIELDS = frozenset({"onClick", "src", "action", "event"})
_TEXT_FIELDS = frozenset({"content", "label"})
_PLAN_KEYS = frozenset({"schemaVersion", "decision", "intentKind", "operations", "reasonCode"})
_SUPPORTED_INTENTS = frozenset({"visual", "text", "layout", "size", "mixed"})
_SUPPORTED_OPERATIONS = frozenset({"set_style", "set_text", "set_layout", "set_size"})
_VALID_SIZES = frozenset({"2x2", "2x4"})


class EditIntentError(ValueError):
    """Raised when the structured intent cannot be safely used."""


class EditIntentDecision(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class EditSurfaceNode:
    component_id: str
    component_type: str
    children: tuple[str, ...]
    editable_fields: tuple[str, ...]
    protected_fields: tuple[str, ...]
    static_values: dict[str, Any]

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "id": self.component_id,
            "type": self.component_type,
            "children": list(self.children),
            "editableFields": list(self.editable_fields),
            "protectedFields": list(self.protected_fields),
            "staticValues": self.static_values,
        }


@dataclass(frozen=True)
class EditSurface:
    size: str | None
    nodes: tuple[EditSurfaceNode, ...]
    protected_categories: tuple[str, ...]

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "nodes": [node.to_prompt_dict() for node in self.nodes],
            "protectedCategories": list(self.protected_categories),
        }

    @property
    def node_ids(self) -> frozenset[str]:
        return frozenset(node.component_id for node in self.nodes)

    def node(self, component_id: str) -> EditSurfaceNode | None:
        for node in self.nodes:
            if node.component_id == component_id:
                return node
        return None


@dataclass(frozen=True)
class EditOperation:
    kind: str
    target: str
    field: str | None
    value: Any

    def to_prompt_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "target": self.target,
            "value": self.value,
        }
        if self.field is not None:
            payload["field"] = self.field
        return payload


@dataclass(frozen=True)
class EditPlan:
    schema_version: int
    decision: EditIntentDecision
    intent_kind: str
    operations: tuple[EditOperation, ...]
    reason_code: str
    field_mask: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.decision is EditIntentDecision.SUPPORTED

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "intentKind": self.intent_kind,
            "operations": [item.to_prompt_dict() for item in self.operations],
            "fieldMask": list(self.field_mask),
            "reasonCode": self.reason_code,
        }


@dataclass
class EditLoopContext:
    """Request-scoped budget and plan state reused by interface retries."""

    plan: EditPlan | None = None
    intent_calls: int = 0
    generation_calls: int = 0


@dataclass(frozen=True)
class EditConformanceResult:
    valid: bool
    errors: tuple[str, ...] = ()


def build_edit_surface(compact_dsl: str, size: str | None) -> EditSurface:
    """Build a redacted semantic surface from a complete Design Token."""
    rows = parse_compact_dsl_rows(compact_dsl)
    nodes: list[EditSurfaceNode] = []
    has_binding = False
    has_event = False
    has_asset = False
    for row in rows:
        if isinstance(row, DataRow):
            has_binding = True
            continue
        if not isinstance(row, ComponentRow):
            continue
        editable_fields: list[str] = []
        protected_fields: list[str] = []
        static_values: dict[str, Any] = {}
        for key, value in row.props.items():
            if key in _PROTECTED_FIELDS:
                protected_fields.append(key)
                if key == "onClick":
                    has_event = True
                if key == "src":
                    has_asset = True
                continue
            if _contains_dynamic_value(value):
                protected_fields.append(key)
                has_binding = True
                continue
            if key in _TEXT_FIELDS:
                editable_fields.append(key)
                static_values[key] = _safe_static_value(value)
                continue
            if key in _STYLE_FIELDS:
                editable_fields.append(key)
                static_values[key] = _safe_static_value(value)
        nodes.append(
            EditSurfaceNode(
                component_id=row.component_id,
                component_type=row.component_type,
                children=tuple(row.children),
                editable_fields=tuple(sorted(set(editable_fields))),
                protected_fields=tuple(sorted(set(protected_fields))),
                static_values=static_values,
            )
        )
    protected: list[str] = []
    if has_binding:
        protected.append("data")
    if has_event:
        protected.append("event")
    if has_asset:
        protected.append("asset")
    return EditSurface(size=size, nodes=tuple(nodes), protected_categories=tuple(protected))


def parse_edit_plan(raw_output: str, surface: EditSurface, *, max_operations: int) -> EditPlan:
    """Parse and validate one strict planner response."""
    try:
        payload = json.loads(raw_output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EditIntentError("intent model returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise EditIntentError("intent model response must be an object")
    unknown = set(payload) - _PLAN_KEYS
    if unknown:
        raise EditIntentError(f"intent plan contains unknown fields: {sorted(unknown)}")
    schema_version = payload.get("schemaVersion")
    decision = payload.get("decision")
    intent_kind = payload.get("intentKind")
    reason_code = payload.get("reasonCode")
    operations_payload = payload.get("operations", [])
    if schema_version != 1 or not isinstance(decision, str) or not isinstance(intent_kind, str):
        raise EditIntentError("intent plan header is invalid")
    try:
        decision_value = EditIntentDecision(decision)
    except ValueError as exc:
        raise EditIntentError("intent plan decision is invalid") from exc
    if not isinstance(reason_code, str) or not reason_code:
        raise EditIntentError("intent plan reasonCode is required")
    if not isinstance(operations_payload, list):
        raise EditIntentError("intent plan operations must be an array")
    if len(operations_payload) > max_operations:
        raise EditIntentError("intent plan contains too many operations")
    if decision_value is not EditIntentDecision.SUPPORTED:
        return EditPlan(1, decision_value, intent_kind, (), reason_code)
    if intent_kind not in _SUPPORTED_INTENTS:
        raise EditIntentError("supported intent kind is not registered")
    if not operations_payload:
        raise EditIntentError("supported intent must contain operations")
    operations: list[EditOperation] = []
    field_mask: list[str] = []
    for item in operations_payload:
        operation = _parse_operation(item, surface)
        if not _operation_matches_intent(operation.kind, intent_kind):
            raise EditIntentError("operation does not match intent kind")
        operations.append(operation)
        field_mask.append(_field_mask(operation))
    return EditPlan(
        schema_version=1,
        decision=decision_value,
        intent_kind=intent_kind,
        operations=tuple(operations),
        reason_code=reason_code,
        field_mask=tuple(dict.fromkeys(field_mask)),
    )


def project_edit_plan(
    request: Any,
    plan: EditPlan,
) -> Any:
    """Apply only metadata operations to a private request copy."""
    updates: dict[str, Any] = {}
    for operation in plan.operations:
        if operation.kind == "set_size":
            updates["size"] = operation.value
        if operation.kind != "set_text":
            continue
        if operation.target == "card.title":
            updates["title"] = operation.value
        elif operation.target == "card.description":
            updates["description"] = operation.value
    if not updates:
        return request
    return request.model_copy(update=updates)


def validate_edit_conformance(
    source_dsl: str,
    generated_dsl: str,
    plan: EditPlan,
) -> EditConformanceResult:
    """Ensure the full generated token only changed approved semantic fields."""
    try:
        source_rows = parse_compact_dsl_rows(source_dsl)
        generated_rows = parse_compact_dsl_rows(generated_dsl)
    except Exception as exc:  # parser errors are converted to a stable result
        return EditConformanceResult(False, (f"compact token parse failed: {type(exc).__name__}",))
    source_components = {
        row.component_id: row for row in source_rows if isinstance(row, ComponentRow)
    }
    generated_components = {
        row.component_id: row for row in generated_rows if isinstance(row, ComponentRow)
    }
    errors: list[str] = []
    if set(source_components) != set(generated_components):
        errors.append("component set changed")
    source_data = [row for row in source_rows if isinstance(row, DataRow)]
    generated_data = [row for row in generated_rows if isinstance(row, DataRow)]
    if source_data != generated_data:
        errors.append("data rows changed")
    allowed_by_target: dict[str, set[str]] = {}
    layout_targets: set[str] = set()
    for operation in plan.operations:
        if operation.kind == "set_style":
            allowed_by_target.setdefault(operation.target, set()).add(operation.field or "")
        elif operation.kind == "set_text":
            allowed_by_target.setdefault(operation.target, set()).add(operation.field or "content")
        elif operation.kind == "set_layout":
            layout_targets.add(operation.target)
    for component_id, source in source_components.items():
        generated = generated_components.get(component_id)
        if generated is None or generated.component_type != source.component_type:
            continue
        allowed_fields = allowed_by_target.get(component_id, set())
        source_props = source.props
        generated_props = generated.props
        all_props = set(source_props) | set(generated_props)
        for prop in all_props:
            if prop in allowed_fields:
                continue
            if source_props.get(prop) != generated_props.get(prop):
                errors.append(f"unauthorized property changed: {component_id}.{prop}")
        if component_id not in layout_targets and source.children != generated.children:
            errors.append(f"unauthorized layout changed: {component_id}")
    has_metadata_operation = any(
        item.kind == "set_size" or item.target.startswith("card.")
        for item in plan.operations
    )
    if not errors and source_rows == generated_rows and not has_metadata_operation:
        errors.append("edit produced no effective change")
    return EditConformanceResult(not errors, tuple(errors))


class CompactEditIntentClient:
    """Dedicated raw-JSON planner client."""

    def __init__(
        self,
        *,
        backend: str,
        runtime: ModelExecutionRuntime | None,
        request_context: ModelRequestContext,
    ) -> None:
        self._client = A2UIModelClient(
            use_mock=False,
            backend=backend,
            runtime=runtime,
            request_context=request_context,
            operation_name="generateWidgetCardCompactDsl.editIntent",
        )

    async def plan(
        self,
        user_query: str,
        surface: EditSurface,
        request_diff: dict[str, Any],
        *,
        max_operations: int,
    ) -> EditPlan:
        prompt = [
            {
                "role": "system",
                "content": (
                    "你是卡片编辑意图规划器。只能输出一个 JSON 对象，不能输出 DSL、"
                    "解释、Markdown 或未注册字段。只允许 set_style、set_text、"
                    "set_layout、set_size；不能新增/删除节点，不能修改数据绑定、事件或素材。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "userQuery": user_query,
                        "surface": surface.to_prompt_dict(),
                        "requestDiff": request_diff,
                        "operationLimit": max_operations,
                        "maxOutputTokens": get_settings().compact_edit_intent_max_tokens,
                        "modelName": get_settings().compact_edit_intent_model_name,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        timeout_seconds = get_settings().compact_edit_intent_request_timeout_seconds
        raw_output = await asyncio.wait_for(
            self._client.generate(
                prompt,
                _RAW_JSON_PROFILE,
                suppress_prompt_log=True,
                phase="edit_intent",
            ),
            timeout=timeout_seconds,
        )
        return parse_edit_plan(raw_output, surface, max_operations=max_operations)

    async def aclose(self) -> None:
        await self._client.aclose()


def _parse_operation(payload: Any, surface: EditSurface) -> EditOperation:
    if not isinstance(payload, dict):
        raise EditIntentError("edit operation must be an object")
    allowed = {"kind", "target", "field", "value"}
    unknown = set(payload) - allowed
    if unknown:
        raise EditIntentError(f"edit operation contains unknown fields: {sorted(unknown)}")
    kind = payload.get("kind")
    target = payload.get("target")
    field = payload.get("field")
    value = payload.get("value")
    if kind not in _SUPPORTED_OPERATIONS or not isinstance(target, str) or not target:
        raise EditIntentError("edit operation header is invalid")
    if kind == "set_size":
        if target != "card" or value not in _VALID_SIZES:
            raise EditIntentError("set_size target or value is invalid")
        return EditOperation(kind, target, None, value)
    if kind == "set_text" and target in {"card.title", "card.description"}:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise EditIntentError("card text value is invalid")
        return EditOperation(kind, target, field or "content", value)
    node = surface.node(target)
    if node is None:
        raise EditIntentError(f"edit target does not exist: {target}")
    if kind == "set_style":
        if field not in node.editable_fields or field in node.protected_fields:
            raise EditIntentError(f"style field is not editable: {target}.{field}")
        if value is None or _contains_dynamic_value(value):
            raise EditIntentError(f"style value is invalid: {target}.{field}")
        return EditOperation(kind, target, field, value)
    if kind == "set_text":
        if field not in _TEXT_FIELDS or field not in node.editable_fields:
            raise EditIntentError(f"text field is not editable: {target}.{field}")
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise EditIntentError("component text value is invalid")
        return EditOperation(kind, target, field, value)
    if kind == "set_layout":
        if target not in surface.node_ids or not isinstance(value, list):
            raise EditIntentError("layout target or value is invalid")
        if set(value) != set(node.children) or len(value) != len(node.children):
            raise EditIntentError("layout operation may only reorder existing children")
        return EditOperation(kind, target, None, tuple(value))
    raise EditIntentError("edit operation is not registered")


def _field_mask(operation: EditOperation) -> str:
    if operation.kind == "set_size":
        return "card.size"
    if operation.kind == "set_layout":
        return f"components.{operation.target}.children"
    if operation.target.startswith("card."):
        return operation.target
    return f"components.{operation.target}.{operation.field}"


def _operation_matches_intent(kind: str, intent_kind: str) -> bool:
    if intent_kind == "mixed":
        return kind in _SUPPORTED_OPERATIONS
    if intent_kind == "visual":
        return kind == "set_style"
    if intent_kind == "text":
        return kind == "set_text"
    if intent_kind == "layout":
        return kind == "set_layout"
    if intent_kind == "size":
        return kind == "set_size"
    return False


def _contains_dynamic_value(value: Any) -> bool:
    if isinstance(value, str):
        return any(marker in value for marker in _DYNAMIC_MARKERS)
    if isinstance(value, dict):
        if "path" in value or "binding" in value:
            return True
        return any(_contains_dynamic_value(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_dynamic_value(child) for child in value)
    return False


def _safe_static_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_static_value(item) for item in value[:16]]
    if isinstance(value, dict):
        return {
            str(key): _safe_static_value(item)
            for key, item in list(value.items())[:16]
        }
    return str(value)[:128]
