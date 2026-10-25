"""与样本编号和业务名称无关的表达式及原文保护回归。"""

from __future__ import annotations

import json

import pytest

from services.card_validation import CompactDslValidationError
from services.card_validation.compact_validation.semantic.display import display_name_errors
from services.card_validation.display_unit_rules import DisplayUnitRule, matching_unit_literal_count
from services.compact_plan import (
    CompactPlanValidationError,
    evaluate_compact_plan_coverage,
    parse_compact_plan_call,
)
from services.dynamic_presentation import normalize_presentation_source
from services.expression_semantics import expression_display_paths, expression_literals
from services.presentation_expressions import (
    countdown_number,
    countdown_prefix,
    recognize_countdown,
)
from services.required_content import explicit_content_members, missing_content_members
from test_dynamic_presentation import context


@pytest.mark.parametrize("path", ["/data/release/countdownDays", "/data/exam/countdownDays"])
@pytest.mark.parametrize("name", ["距离感", "Launch Alpha", "项目阶段乙"])
def test_syntax_variants_and_boundaries_preserve_names(path, name):
    original = countdown_prefix(path, name)
    body = original.removeprefix("{{").removesuffix("}}")
    variants = [original, "{{\n(" + body + ")\n}}", original.replace(" ? ", "?")]
    for variant in variants:
        display = recognize_countdown(variant)
        assert display is not None
        assert display.target == name and display.path == path
        assert expression_display_paths(variant) == ()


@pytest.mark.parametrize("expression,expected", [
    ("${/data/n} > 0 ? ${/data/n} : -${/data/n}", ("/data/n",)),
    ("${/data/n} > 0 ? '接近' : '过去'", ()),
    ("${/data/n} + ${/data/n}", ("/data/n", "/data/n")),
    ("${/data/a} ? ${/data/b} : ${/data/c}", ("/data/b", "/data/c")),
    ("size(${/data/items})", ("/data/items",)),
])
def test_display_references_follow_syntax_roles(expression, expected):
    assert expression_display_paths("{{ " + expression + " }}") == expected


@pytest.mark.parametrize("literal,expected", [
    ("'a\\nb'", "a\nb"), ("'a\\'b'", "a'b"),
    ("'a\\\\nb'", "a\\nb"), ("'a\nb'", "a\nb"),
])
def test_literal_decoding_preserves_existing_escapes_and_multiline_text(literal, expected):
    expression = "{{ ${/data/n} + " + literal + " }}"
    assert expression_literals(expression) == (expected,)


@pytest.mark.parametrize(
    "heading", ["Day1", "第一阶段", "Release Alpha", "周末清单", "研发包括验收"],
)
@pytest.mark.parametrize("body", ["检查资料、确认时间", "\n- 检查资料\n- 确认时间"])
def test_explicit_members_are_independent_of_domain_and_heading(heading, body):
    query = heading + "：" + body
    members = explicit_content_members(query)
    assert [member.text for member in members] == ["检查资料", "确认时间"]
    evidence = [(heading, ("检查资料",)), (heading, ("确认时间",))]
    assert not missing_content_members(query, evidence)
    assert len(missing_content_members(query, evidence[:1])) == 1


def test_branch_units_count_rendered_paths_without_evaluating_conditions():
    rule = DisplayUnitRule(("天",), False)
    value = "{{ ${/data/n} > 0 ? ${/data/n} + '天' : -${/data/n} + '天' }}"
    assert matching_unit_literal_count(value, rule) == 1
    assert matching_unit_literal_count("{{ ${/data/n} + '天' + '天' }}", rule) == 2
    assert matching_unit_literal_count("{{ ${/data/n} + ('天' + '天') }}", rule) == 2
    assert matching_unit_literal_count("{{ size('天') + ${/data/n} }}", rule) == 0


@pytest.mark.parametrize("name", ["孩子的生日", "我的答辩", "距离感"])
def test_grounded_identity_is_data_associated_without_sentence_regex(name):
    card, task = context()
    task["userQuery"] = "请做一张“" + name + "”卡片"
    card["title"] = "倒计时"
    path = "/data/countdown/countdownDays"
    plan = parse_compact_plan_call(json.dumps({"name": "submit_card_plan", "arguments": {
        "info_required": [{"dataId": path, "requirement": "目标天数", "displayName": name}],
    }}, ensure_ascii=False), task, card_spec=card).plan
    source = '\n'.join(json.dumps(row, ensure_ascii=False) for row in [
        ["root", "Layout", {"layout": "S-center"}, ["value"]],
        ["value", "DataDisplay", {"value": {"path": path}, "supportingText": "天"}],
    ])
    normalized = normalize_presentation_source(source, card, task, plan)
    assert name in normalized
    assert normalize_presentation_source(normalized, card, task, plan) == normalized
    assert not evaluate_compact_plan_coverage(normalized, plan, task).errors
    plan.get("info_required")[0]["displayName"] = "不存在的事件"
    assert display_name_errors(plan, card, task)


def test_name_is_required_in_visible_content_and_cannot_be_replaced_by_only_number():
    source = json.dumps(["root", "Text", {"content": countdown_number("/data/n")}])
    result = evaluate_compact_plan_coverage(source, {"info_required": [
        {"dataId": "/data/n", "displayName": "项目里程碑"},
    ]}, {})
    assert result.errors


def test_generated_identity_is_not_reparsed_as_a_natural_language_sentence():
    card, task = context()
    path = "/data/countdown/countdownDays"
    name = "距离太阳还剩"
    source = '\n'.join(json.dumps(row, ensure_ascii=False) for row in [
        ["root", "Layout", {"layout": "S-title-content"}, ["title", "value"]],
        ["title", "SingleLineTitle", {"title": countdown_prefix(path, name)}],
        ["value", "EmphasizedData", {"value": countdown_number(path), "unit": "天"}],
    ])
    normalized = normalize_presentation_source(source, card, task)
    title = json.loads(normalized.splitlines()[1])[2].get("title")
    display = recognize_countdown(title)
    assert display is not None and display.target == name


def test_generated_title_cannot_be_reassigned_to_another_countdown():
    card, task = context()
    path = "/data/countdown/countdownDays"
    source = '\n'.join(json.dumps(row, ensure_ascii=False) for row in [
        ["root", "Layout", {"layout": "S-title-content"}, ["title", "value"]],
        ["title", "SingleLineTitle", {
            "title": countdown_prefix("/data/another/countdownDays", "项目甲"),
        }],
        ["value", "EmphasizedData", {"value": {"path": path}, "unit": "天"}],
    ])
    with pytest.raises(CompactDslValidationError, match="COMPACT_DISPLAY_COUNTDOWN_TARGET"):
        normalize_presentation_source(source, card, task)


@pytest.mark.parametrize("value", [None, False, 12, ""])
def test_optional_name_does_not_coerce_invalid_values_or_treat_null_as_absence(value):
    card, task = context()
    output = json.dumps({"name": "submit_card_plan", "arguments": {"info_required": [{
        "dataId": "/data/countdown/countdownDays", "requirement": "天数", "displayName": value,
    }]}})
    with pytest.raises(CompactPlanValidationError, match="displayName"):
        parse_compact_plan_call(output, task, card_spec=card)
