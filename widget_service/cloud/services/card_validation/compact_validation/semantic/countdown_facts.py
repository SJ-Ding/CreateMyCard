"""显式分组成员的规划完整性与可见内容覆盖，不按业务名称识别事实。"""

from __future__ import annotations

from typing import Any

from services.expression_semantics import expression_literals, expression_tree
from services.required_content import explicit_content_members, missing_content_members


def _literal_parts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        if expression_tree(value) is not None:
            return expression_literals(value)
        return () if "{{" in value else (value,)
    parts = []
    if isinstance(value, dict):
        if set(value) == {"path"}:
            return ()
        for child in value.values():
            parts.extend(_literal_parts(child))
    elif isinstance(value, list):
        for child in value:
            parts.extend(_literal_parts(child))
    return tuple(parts)


def countdown_plan_errors(plan: dict, card: dict, task: dict) -> tuple[str, ...]:
    if not any(binding.get("capabilityId") == "GetCountdownDays"
               for binding in card.get("dataBindings") or []):
        return ()
    members = explicit_content_members(task.get("userQuery", ""))
    fragments = []
    for fact in plan.get("info_required") or []:
        text = fact.get("text")
        if not isinstance(text, str):
            continue
        context = fact.get("requirement", "")
        if any(member.has_section(text) for member in members):
            context = ""
        fragments.append((context, (text,)))
    return tuple("Countdown request fact missing from Plan: " + fact
                 for fact in missing_content_members(task.get("userQuery", ""), fragments))


def countdown_source_errors(source: str, card: dict, task: dict) -> tuple[str, ...]:
    from services.compact_dsl_a2ui_converter import parse_compact_dsl_rows
    from services.compact_high_level_components import ComponentRow

    if not any(binding.get("capabilityId") == "GetCountdownDays"
               for binding in card.get("dataBindings") or []):
        return ()
    text_keys = {"title", "label", "value", "mainText", "secondaryText", "content", "time",
                 "location", "primaryText", "supportingText", "unit"}
    components = {}
    parents = {}
    for row in parse_compact_dsl_rows(source, expand_layouts=False):
        if isinstance(row, ComponentRow):
            components[row.component_id] = row
            for child in row.children:
                parents[child] = row.component_id
    fragments = []
    pending = ["root"]
    visited = set()
    while pending:
        identifier = pending.pop()
        if identifier in visited:
            continue
        visited.add(identifier)
        row = components.get(identifier)
        if row is None:
            continue
        pending.extend(row.children)
        parent = components.get(parents.get(identifier))
        labels = []
        if parent is not None:
            for sibling_id in parent.children:
                sibling = components.get(sibling_id)
                if sibling is not None and sibling.component_type in {
                    "SingleLineTitle", "DoubleLineTitle",
                }:
                    labels.extend(_literal_parts(sibling.props.get("title")))
        context = " ".join(labels)
        parts = []
        for key, value in row.props.items():
            if key in text_keys:
                parts.extend(_literal_parts(value))
        if parts:
            fragments.append((context, tuple(parts)))
        items = row.props.get("items")
        if isinstance(items, list):
            for item in items:
                fragments.append((context, _literal_parts(item)))
    return tuple("Countdown request fact missing from visible content: " + fact
                 for fact in missing_content_members(task.get("userQuery", ""), fragments))
