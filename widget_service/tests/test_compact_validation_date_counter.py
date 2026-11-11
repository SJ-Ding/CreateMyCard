"""验证日期计数关系词的展示约束和修复反馈。"""

import json
from typing import Any

import pytest

from services.card_validation.compact_validation.api import validate_compact_dsl
from services.card_validation.compact_validation.diagnostics import CompactDslValidationError
from services.generation_pipeline import DesignCompactProcessor

_ROOT = "/data/customCounter"
_RELATION = "${/data/customCounter/relationText}"
_DATE = "${/data/customCounter/targetDateText}"
_DAYS = "${/data/customCounter/days}"
_CODE = "COMPACT_DISPLAY_DATE_COUNTER_RELATION_TEXT"


def _validate(
    content: Any, capability_id: str = "GetDateCounter", *, slot: str = "content",
) -> None:
    display = ["display", "Text", {"content": content}]
    if slot == "supportingText":
        display = [
            "display", "DataDisplay",
            {"label": "运动会", "value": {"path": f"{_ROOT}/days"}, "supportingText": content},
        ]
    elif slot == "mainText":
        display = ["display", "EmphasisText", {"mainText": content, "fontSize": 18}]
    rows = [
        ["root", "Column", {}, ["display"]],
        display,
        [f"{_ROOT}/relationText", "还剩"],
        [f"{_ROOT}/targetDateText", "2026/11/09"],
        [f"{_ROOT}/days", 3],
    ]
    task = {
        "size": "2x2",
        "dataModelSchema": {
            "data": {
                "customCounter": {
                    "relationText": {"type": "string"},
                    "targetDateText": {"type": "string"},
                    "days": {"type": "integer"},
                },
            },
        },
    }
    card = {
        "suggestSize": "2x2",
        "dataBindings": [{"capabilityId": capability_id, "writeResultTo": _ROOT}],
    }
    source = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    validate_compact_dsl(source, task_spec=task, card_spec=card)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ({"path": f"{_ROOT}/relationText"}, "单独展示"),
        ("{{ " + _RELATION + " }}", "单独展示"),
        ("{{ " + _RELATION + " + ' · ' + " + _DATE + " }}", "targetDateText 拼接"),
        ("{{ " + _DATE + " + ' · ' + " + _RELATION + " }}", "targetDateText 拼接"),
    ],
)
def test_invalid_relation_text_is_located_and_reaches_quality_repair(
    content: Any, reason: str,
) -> None:
    with pytest.raises(CompactDslValidationError) as caught:
        _validate(content)
    error = caught.value
    diagnostic = next(item for item in error.diagnostics if item.code == _CODE)
    assert diagnostic.component_id == "display"
    assert diagnostic.property_path == "/content"
    assert reason in diagnostic.message
    assert diagnostic.fix_hint
    issues = DesignCompactProcessor._validation_failure("", error.errors, groups=error.groups)
    feedback = str([item.to_prompt_payload() for item in issues.errors])
    for wording in ("枚举关联词", "事件名", "days", "不得把 relationText 与 targetDateText 拼接"):
        assert wording in feedback


@pytest.mark.parametrize(
    "content",
    [
        "{{ '运动会 ' + " + _RELATION + " }}",
        "{{ " + _RELATION + " + ' ' + " + _DAYS + " + ' 天' }}",
        "{{ " + _DAYS + " == 0 ? " + _RELATION + " : " + _DATE + " }}",
        {"path": f"{_ROOT}/targetDateText"},
    ],
)
def test_allowed_relation_and_date_uses(content: Any) -> None:
    _validate(content)


def test_same_named_field_from_another_capability_is_out_of_scope() -> None:
    _validate({"path": f"{_ROOT}/relationText"}, capability_id="OtherCapability")


def test_supporting_text_is_located_before_expansion() -> None:
    content = "{{ " + _RELATION + " + ' · ' + " + _DATE + " }}"
    with pytest.raises(CompactDslValidationError) as caught:
        _validate(content, slot="supportingText")
    diagnostic = next(item for item in caught.value.diagnostics if item.code == _CODE)
    assert diagnostic.component_id == "display"
    assert diagnostic.property_path == "/supportingText"


@pytest.mark.parametrize(
    "content",
    [
        {"path": f"{_ROOT}/relationText"},
        "{{ " + _RELATION + " + ' · ' + " + _DATE + " }}",
    ],
)
def test_emphasis_text_main_text_is_checked(content: Any) -> None:
    with pytest.raises(CompactDslValidationError) as caught:
        _validate(content, slot="mainText")
    diagnostic = next(item for item in caught.value.diagnostics if item.code == _CODE)
    assert diagnostic.component_id == "display"
    assert diagnostic.property_path == "/mainText"
    assert diagnostic.fix_hint


def test_emphasis_text_allows_relation_with_event_name() -> None:
    _validate("{{ '出发 ' + " + _RELATION + " }}", slot="mainText")
