# -*- coding: utf-8 -*-
"""Compact DSL 编辑意图规划与最小编辑面测试。"""

import json

import pytest

from services.compact_dsl_a2ui_converter import ComponentRow
from services.compact_edit_agent import (
    EditIntentError,
    build_edit_surface,
    parse_edit_plan,
    validate_edit_conformance,
)


def _source(*, label: str = "今日天气", font_size: int = 14) -> str:
    rows = [
        ComponentRow("root", "Column", {}, ("label", "button")),
        ComponentRow("label", "Text", {"content": label, "fontSize": font_size}),
        ComponentRow("button", "Button", {"label": "详情", "onClick": [{"call": "open"}]}),
    ]
    return "\n".join(
        json.dumps(
            [row.component_id, row.component_type, row.props, list(row.children)]
            if row.children
            else [row.component_id, row.component_type, row.props],
            ensure_ascii=False,
        )
        for row in rows
    )


def _plan(operation: dict, intent_kind: str = "text") -> str:
    return json.dumps(
        {
            "schemaVersion": 1,
            "decision": "supported",
            "intentKind": intent_kind,
            "operations": [operation],
            "reasonCode": "TARGET_MATCHED",
        },
        ensure_ascii=False,
    )


def test_surface_redacts_dynamic_and_protected_fields() -> None:
    source = _source().replace('"今日天气"', '"{{ ${/data/weather/name} }}"')
    surface = build_edit_surface(source, "2x2")
    label = surface.node("label")
    assert label is not None
    assert "content" not in label.editable_fields
    assert "content" in label.protected_fields
    assert "event" in surface.protected_categories


def test_parse_text_plan_and_conformance() -> None:
    source = _source()
    surface = build_edit_surface(source, "2x2")
    plan = parse_edit_plan(
        _plan(
            {
                "kind": "set_text",
                "target": "label",
                "field": "content",
                "value": "今日空气质量",
            }
        ),
        surface,
        max_operations=8,
    )
    generated = _source(label="今日空气质量")
    result = validate_edit_conformance(source, generated, plan)
    assert result.valid
    assert plan.field_mask == ("components.label.content",)


def test_conformance_rejects_unapproved_event_change() -> None:
    source = _source()
    surface = build_edit_surface(source, "2x2")
    plan = parse_edit_plan(
        _plan(
            {
                "kind": "set_style",
                "target": "label",
                "field": "fontSize",
                "value": 16,
            },
            intent_kind="visual",
        ),
        surface,
        max_operations=8,
    )
    generated = _source(font_size=16).replace('"open"', '"delete"')
    result = validate_edit_conformance(source, generated, plan)
    assert not result.valid
    assert any("onClick" in item for item in result.errors)


def test_parse_rejects_unknown_operation_and_new_layout_children() -> None:
    source = _source()
    surface = build_edit_surface(source, "2x2")
    with pytest.raises(EditIntentError):
        parse_edit_plan(
            _plan(
                {
                    "kind": "delete_node",
                    "target": "label",
                    "value": None,
                }
            ),
            surface,
            max_operations=8,
        )
    with pytest.raises(EditIntentError):
        parse_edit_plan(
            _plan(
                {
                    "kind": "set_layout",
                    "target": "root",
                    "value": ["label", "button", "new"],
                },
                intent_kind="layout",
            ),
            surface,
            max_operations=8,
        )
