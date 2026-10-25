"""请求局部的属性来源记录，不通过生成ID或颜色值猜测落点。"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import wraps

from .compiler import text_expression
from .expressions import compile_expression
from .models import PresentationError, PresentationPlan, PropertyTarget
from .planning import available_fields, validate_plan


@dataclass
class ConversionContext:
    plan: PresentationPlan
    fields: dict[str, str]
    targets: dict[str, PropertyTarget] = field(default_factory=dict)
    origins: dict[str, str] = field(default_factory=dict)


_ACTIVE: ContextVar[ConversionContext | None] = ContextVar("presentation_conversion", default=None)


@dataclass(frozen=True)
class MarkerReference:
    rule_id: str
    slot: str


def processing_scope(method):
    @wraps(method)
    def run(processor, source: str, context):
        from services.compact_high_level_components import CompactDslConversionError

        raw = (context.compact_plan or {}).get("presentation")
        if raw is None:
            return method(processor, source, context)
        try:
            plan = validate_plan(raw, context.task_spec, context.card_spec)
            active = ConversionContext(plan, available_fields(context.task_spec, context.card_spec))
            token = _ACTIVE.set(active)
            try:
                check_source_slots(source, context)
                result = method(processor, source, context)
                if not result.errors:
                    check_resolved_coverage(result.source_dsl, active, context)
                    for rule in plan.rules:
                        for slot in rule.default.texts:
                            marker = "@presentation:" + rule.id + ":" + slot
                            if marker not in active.targets:
                                raise PresentationError("PRESENTATION_SLOT", "计划文字槽未落实")
                return replace(
                    result,
                    presentation_targets=list(active.targets.values()),
                    presentation_origins=dict(active.origins),
                )
            finally:
                _ACTIVE.reset(token)
        except PresentationError as exc:
            return processor._validation_failure(source, (str(exc),), code=exc.code)
        except CompactDslConversionError as exc:
            return processor._validation_failure(source, (str(exc),))

    return run


def check_resolved_coverage(source: str, active: ConversionContext, context) -> None:
    from services.compact_plan import evaluate_compact_plan_coverage

    def resolve(value):
        result = value
        if isinstance(value, str) and value.startswith("@presentation:"):
            marker = _marker(value)
            for rule in active.plan.rules:
                if rule.id == marker.rule_id:
                    result = text_expression(rule, marker.slot, active.fields)
        elif isinstance(value, dict):
            result = {}
            for name, child in value.items():
                result[name] = resolve(child)
        elif isinstance(value, list):
            result = []
            for child in value:
                result.append(resolve(child))
        return result

    lines = []
    for line in source.splitlines():
        if line.strip():
            lines.append(json.dumps(resolve(json.loads(line)), ensure_ascii=False))
    required = any(rule.requirement == "required" for rule in active.plan.rules)
    omitted = () if required else context.allowed_omitted_plan_fact_indexes
    warnings_only = False if required else context.plan_information_coverage_as_warning
    coverage = evaluate_compact_plan_coverage(
        "\n".join(lines),
        context.compact_plan,
        context.task_spec,
        allowed_omitted_fact_indexes=omitted,
        information_errors_as_warnings=warnings_only,
    )
    if coverage.errors:
        raise PresentationError("PRESENTATION_COVERAGE", "; ".join(coverage.errors))


def register_origins(origins: dict[str, str]) -> None:
    active = _ACTIVE.get()
    if active is not None:
        active.origins.update(origins)


def register_rows(source_id: str, rows: list) -> None:
    active = _ACTIVE.get()
    if active is not None:
        origin = active.origins.get(source_id, source_id)
        for row in rows:
            active.origins[row.component_id] = origin


def register_nodes(source_id: str, nodes: list[dict]) -> None:
    active = _ACTIVE.get()
    if active is not None:
        origin = active.origins.get(source_id, source_id)
        for node in nodes:
            active.origins[node.get("id")] = origin


def check_source_slots(source: str, context) -> None:
    from services.compact_dsl_a2ui_converter import ComponentRow, parse_compact_dsl_rows

    allowed = {
        "SecondaryBody": {"items[].value"},
        "EmphasisText": {"secondaryText"},
        "InfoBlock": {"secondaryText"},
    }

    def visit(value, path: str, component: ComponentRow) -> None:
        if isinstance(value, str) and value.startswith("@presentation:"):
            if path not in allowed.get(component.component_type, set()):
                raise PresentationError("PRESENTATION_SLOT", "状态文案不能占用标题、数值或动作")
        elif isinstance(value, dict):
            for name, item in value.items():
                visit(item, path + "." + name if path else name, component)
        elif isinstance(value, list):
            for item in value:
                visit(item, path + "[]", component)

    rows = parse_compact_dsl_rows(
        source,
        expand_layouts=False,
        size=context.size,
        skip_compact_dsl_validation=context.skip_compact_dsl_validation,
    )
    for row in rows:
        if isinstance(row, ComponentRow):
            visit(row.props, "", row)


def _marker(value: str) -> MarkerReference:
    parts = value.split(":")
    if len(parts) != 3:
        raise PresentationError("PRESENTATION_SLOT", "文字槽引用格式非法")
    return MarkerReference(parts[1], parts[2])


def marker_preview(value: str) -> str:
    active = _ACTIVE.get()
    if active is None or not value.startswith("@presentation:"):
        return value
    marker = _marker(value)
    for rule in active.plan.rules:
        if rule.id != marker.rule_id:
            continue
        texts = []
        for outcome in [rule.default, rule.unknown, *rule.states]:
            expression = outcome.texts.get(marker.slot)
            if expression is None:
                raise PresentationError("PRESENTATION_SLOT", "文字槽未登记")
            if expression.op == "literal":
                texts.append(str(expression.value))
            else:
                texts.append(compile_expression(expression, active.fields).source)
        from services.card_validation.base import estimate_text_width

        return max(texts, key=lambda text: estimate_text_width(text, 1.0))
    raise PresentationError("PRESENTATION_SLOT", "文字槽规则不存在")


def resolve_component(node: dict) -> None:
    active = _ACTIVE.get()
    if active is None:
        return
    content = node.get("content")
    if not isinstance(content, str) or not content.startswith("@presentation:"):
        return
    if node.get("component") != "Text":
        raise PresentationError("PRESENTATION_SLOT", "文字槽只能落到文字内容")
    marker = _marker(content)
    for rule in active.plan.rules:
        if rule.id != marker.rule_id:
            continue
        identity = node.get("id")
        target = PropertyTarget(
            component_id=identity,
            property="content",
            role="text:" + marker.rule_id + ":" + marker.slot,
            source_id=active.origins.get(identity, identity),
        )
        previous = active.targets.get(content)
        if previous is not None and previous.component_id != identity:
            raise PresentationError("PRESENTATION_SLOT", "文案槽不能有多个落点")
        active.targets[content] = target
        node["content"] = text_expression(rule, marker.slot, active.fields)
        return
    raise PresentationError("PRESENTATION_SLOT", "文字槽规则不存在")
