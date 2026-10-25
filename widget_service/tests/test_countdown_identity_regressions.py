"""13 条真实请求回归：身份、完整事实、确定性状态、修复门禁及实际绑定刷新。"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api.schemas import GenerateWidgetCardRequest
from config.config import get_settings
from core.errors import GenerationStatus
from custom.a2ui_model_client import A2UIModelClient
from services.artifact_store import ArtifactStore
from services.card_validation.compact_validation.semantic.countdown_facts import (
    countdown_plan_errors,
    countdown_source_errors,
)
from services.compact_plan import evaluate_compact_plan_coverage
from services.dynamic_presentation import (
    apply_presentation_colors,
    normalize_presentation_source,
    presentation_rules,
)
from services.generation_pipeline import DesignCompactProcessor, DslProcessingContext
from services.presentation_expressions import countdown_readout, recognize_countdown
from services.protocol_registry import A2UIProtocolRegistry
from services.required_content import explicit_content_members
from services.widget_generation_service import WidgetGenerationService
from test_dynamic_presentation import runtime as runtime

_FIXTURE = Path(__file__).parent / "fixtures/countdown_batch_20261024_004814.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8")).get("cases")
assert isinstance(_CASES, list) and len(_CASES) == 13
_NAMES = {
    "Q005": ("中秋节", "国庆节"), "Q011": ("国庆中秋",),
    "Q039": ("出发去石家庄看妈妈",), "Q041": ("回石家庄看妈妈",),
    "Q069": ("上楼提醒",), "Q100": ("本周日",), "Q101": ("本周日",),
    "Q105": ("周日约饭",), "Q106": ("杭州2天1晚秋日漫游",),
    "Q107": ("杭州漫游",), "Q108": ("杭州",), "Q109": ("杭州",),
    "Q119": ("生日",),
}


def _case(identifier: str) -> dict:
    return copy.deepcopy(next(case for case in _CASES if case.get("id") == identifier))


def _source(case: dict) -> str:
    source = case.get("deliveredSource") or case.get("sources")[-1].get("source")
    assert isinstance(source, str)
    return source


def _process(case: dict, *, warnings: bool = False):
    card = case.get("card")
    assert isinstance(card, dict)
    return DesignCompactProcessor().process(_source(case), DslProcessingContext(
        size=card.get("suggestSize"), card_spec=card, task_spec=case.get("task"),
        compact_plan=case.get("sources")[-1].get("plan"), colorless_layout=True,
        plan_information_coverage_as_warning=warnings,
        protocol_profile=A2UIProtocolRegistry("a2ui-form-rom6.0-v1").get_profile(),
    ))


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.get("id"))
def test_all_thirteen_recorded_requests_keep_identity_and_real_validation_gates(monkeypatch, case):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    for source in case.get("sources"):
        digest = hashlib.sha256(source.get("source").encode()).hexdigest()
        assert digest == source.get("sourceSha256")
    delivered = case.get("deliveredSource")
    if isinstance(delivered, str):
        assert hashlib.sha256(delivered.encode()).hexdigest() == case.get("deliveredSourceSha256")
    result = _process(case)
    identifier = case.get("id")
    if identifier == "Q107":
        missing = sum("Countdown request fact missing" in issue.message for issue in result.errors)
        assert missing == 9
    elif identifier in {"Q108", "Q109"}:
        assert result.errors  # 真实容量、组件或事实问题不因放宽句式而放行。
        assert not any("expected 1, got 3" in issue.message for issue in result.errors)
    else:
        assert not result.errors, result.issues
        for name in _NAMES.get(identifier, ()):
            assert name in result.source_dsl
        assert "目标日" not in result.source_dsl
        assert "距离距" not in result.source_dsl
        assert "已经" not in result.source_dsl
        assert normalize_presentation_source(
            result.source_dsl, case.get("card"), case.get("task"),
        ) == result.source_dsl


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.get("id"))
def test_recorded_event_names_and_states_refresh_in_local_renderer(runtime, case):
    card, task = case.get("card"), case.get("task")
    normalized = normalize_presentation_source(_source(case), card, task)
    displays = []
    for line in normalized.splitlines():
        row = json.loads(line)
        if len(row) < 3:
            continue
        for value in row[2].values():
            display = recognize_countdown(value)
            if display is not None:
                displays.append({"source": value, "path": display.path, "kind": display.kind,
                                 "unit": display.unit, "status": display.status})
    assert displays
    values = (-8, -1, 0, 1, 7, 8)
    updates = []
    for value in values:
        data = {}
        for rule in presentation_rules(card, task):
            if rule.kind != "countdown":
                continue
            node = data
            parts = rule.path.strip("/").split("/")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
        updates.append(data)
    sources = []
    for display in displays:
        sources.append(display.get("source"))
    resolved = runtime(sources, updates)
    for number, entries in zip(values, resolved, strict=True):
        for display, entry in zip(displays, entries, strict=True):
            if display.get("status"):
                state = "还剩" if number > 0 else "已过"
                expected = "今天" if number == 0 else state + str(abs(number)) + "天"
                assert entry == expected
            elif display.get("kind") == "number":
                expected = str(abs(number)) + "天" if display.get("unit") else abs(number)
                assert entry == expected
            else:
                state = "今天" if number == 0 else "还剩" if number > 0 else "已过"
                assert entry.endswith(" · " + state)
                assert "目标日" not in entry
    assert runtime(sources, [{}]) == [[""] * len(sources)]


@pytest.mark.parametrize("identifier,count", [("Q107", 9), ("Q108", 10), ("Q109", 10)])
def test_explicit_itinerary_is_protected_before_and_after_plan(identifier, count, monkeypatch):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    case = _case(identifier)
    card, task = case.get("card"), case.get("task")
    assert len(explicit_content_members(task.get("userQuery"))) == count
    incomplete = {"info_required": [{"dataId": "/data/countdown/countdownDays"}]}
    assert len(countdown_plan_errors(incomplete, card, task)) == count
    source = _source(_case("Q107"))
    assert len(countdown_source_errors(source, card, task)) == count
    case["deliveredSource"] = source
    case.get("card")["suggestSize"] = "2x2"
    case.get("task")["size"] = "2x2"
    assert any("Countdown request fact missing" in error.message for error in _process(
        case, warnings=True,
    ).errors)


def test_day_and_time_location_members_cannot_be_satisfied_by_other_members():
    case = _case("Q109")
    task, card = case.get("task"), case.get("card")
    complete = {"info_required": []}
    for fact in explicit_content_members(task.get("userQuery")):
        complete.get("info_required").append({"text": fact.section + ":" + fact.text})
    assert not countdown_plan_errors(complete, card, task)
    complete.get("info_required")[0]["text"] = "Day2 09:00-11:30灵隐寺祈福"
    assert len(countdown_plan_errors(complete, card, task)) == 1
    complete.get("info_required")[0]["text"] = "Day10 09:00-11:30灵隐寺祈福"
    assert len(countdown_plan_errors(complete, card, task)) == 1


@pytest.mark.parametrize("identifier", ["Q108", "Q109"])
def test_recorded_complete_plan_can_associate_day_without_rewriting_source_text(identifier):
    case = _case(identifier)
    plan = case.get("sources")[-1].get("plan")
    assert not countdown_plan_errors(plan, case.get("card"), case.get("task"))
    broken = copy.deepcopy(plan)
    broken.get("info_required")[1]["text"] = "遗漏具体内容"
    assert countdown_plan_errors(broken, case.get("card"), case.get("task"))


def test_model_control_only_date_caption_does_not_duplicate_numeric_fact(monkeypatch):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    case = _case("Q108")
    source = case.get("sources")[-1].get("source")
    normalized = normalize_presentation_source(source, case.get("card"), case.get("task"))
    coverage = evaluate_compact_plan_coverage(
        normalized, case.get("sources")[-1].get("plan"), case.get("task"),
    )
    assert not coverage.duplicate_facts
    assert "距离2026年10月17日还剩天" not in normalized
    assert "2026年10月17日" in normalized


def test_new_full_text_readout_gets_unit_checks_and_state_color(runtime):
    from services.card_validation.display_unit_rules import display_unit_references

    case = _case("Q105")
    path = "/data/countdown/countdownDays"
    value = countdown_readout(path)
    assert display_unit_references(value) == [path]
    source = json.dumps({"updateComponents": {"components": [
        {"id": "value", "component": "Text", "content": value,
         "styles": {"fontColor": "#FF111111"}},
    ]}}, ensure_ascii=False)
    colored = apply_presentation_colors(source, case.get("card"), case.get("task"))
    updates = []
    for number in (-8, -1, 0, 1, 7, 8):
        updates.append({"data": {"countdown": {"countdownDays": number}}})
    color = json.loads(colored).get("updateComponents").get("components")[0].get("styles")
    colors = runtime(color, updates)
    assert [item.get("fontColor") for item in colors] == [
        "#FF111111", "#FF111111", "#FFB3261E", "#FFB3261E", "#FFB3261E", "#FF111111",
    ]
    assert apply_presentation_colors(
        source, case.get("card"), case.get("task"), uniform=True,
    ) == source


def test_required_information_cannot_be_downgraded_or_authorized_away():
    plan = {"info_required": [
        {"text": "约饭", "priority": "must"}, {"text": "可选提示", "priority": "should"},
    ]}
    source = '["root","Text",{"content":"标题"}]'
    result = evaluate_compact_plan_coverage(
        source, plan, {}, allowed_omitted_fact_indexes=(0, 1),
        information_errors_as_warnings=True, protect_required_information=True,
    )
    assert len(result.errors) == 1
    assert not result.warnings


@pytest.mark.parametrize("component,props", [
    ("EmphasisText", {"mainText": {"path": "/data/countdown/countdownDays"},
                      "secondaryText": "周日约饭"}),
    ("DataDisplay", {"value": {"path": "/data/countdown/countdownDays"},
                     "label": "周日约饭", "supportingText": "天"}),
])
def test_countdown_identity_can_use_local_label_without_forcing_a_title(component, props, runtime):
    case = _case("Q105")
    source = "\n".join(json.dumps(row, ensure_ascii=False) for row in [
        ["root", "Column", {}, ["readout"]], ["readout", component, props],
    ])
    result = normalize_presentation_source(source, case.get("card"), case.get("task"))
    assert "SingleLineTitle" not in result
    assert "周日约饭" in result
    assert normalize_presentation_source(result, case.get("card"), case.get("task")) == result
    display = json.loads(result.splitlines()[1])[2]
    value = runtime(display, [{"data": {"countdown": {"countdownDays": -2}}}])[0]
    assert "已过" in json.dumps(value, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.get("id"))
async def test_thirteen_requests_through_service_with_fixed_model_and_real_validators(
    case, monkeypatch,
):
    settings = get_settings()
    for key, value in (
        ("enable_a2ui_model_mock", False), ("enable_artifact_validation", True),
        ("enable_validation_failure_retry", False), ("enable_photo_background_theme", False),
        ("enable_single_step_generation_fallback", False),
        ("enable_compact_dsl_interface_retry", False), ("enable_model_failure_retry", False),
    ):
        monkeypatch.setattr(settings, key, value)
    monkeypatch.setattr(WidgetGenerationService, "_enable_card_template", lambda _self: False)
    monkeypatch.setattr(WidgetGenerationService, "_enable_jsx_generation", lambda _self: False)
    calls = []
    saved = []
    rows = []
    for line in _source(case).splitlines():
        row = json.loads(line)
        if len(row) >= 3:
            for prop, value in row[2].items():
                display = recognize_countdown(value)
                if display is None:
                    continue
                if display.kind == "prefix":
                    row[2][prop] = display.target
                else:
                    row[2][prop] = {"path": display.path}
        rows.append(row)
    fixed_source = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)

    async def generate(_client, _messages, profile=None, **_kwargs):
        profile = profile or {}
        calls.append(profile.get("id"))
        if profile.get("id") == "compact-info-plan":
            return json.dumps({
                "name": "submit_card_plan", "arguments": case.get("sources")[-1].get("plan"),
            })
        if profile.get("format") == "raw-json":
            return '{"themeId":"gradient-blue"}'
        return fixed_source

    async def save(_store, artifact):
        saved.append(artifact)
        return SimpleNamespace(artifactUrl="https://obs.test/countdown.md", artifactDigest="test")

    monkeypatch.setattr(A2UIModelClient, "generate", generate)
    monkeypatch.setattr(ArtifactStore, "save", save)
    card, task = case.get("card"), case.get("task")
    assets = []
    for asset in task.get("assetCandidates", []):
        assets.append(asset.get("id"))
    events = []
    for event in task.get("eventCandidates", []):
        events.append({"capabilityId": event.get("id"), "action": {
            "call": event.get("call"), "args": event.get("args"),
        }})
    request = GenerateWidgetCardRequest(
        uid="countdown-identity-regression", prdVer="11.7.8.339",
        device={"romVersion": "7.0"}, userQuery=task.get("userQuery"),
        title=card.get("title"), description=card.get("description"),
        size=card.get("suggestSize"), candidateDataBindings=card.get("dataBindings"),
        candidateAssetIds=assets, candidateEventCandidates=events,
    )
    result = await WidgetGenerationService().generate_widget_card_compact_dsl(request)
    if case.get("id") in {"Q107", "Q108", "Q109"}:
        assert result.status != GenerationStatus.SUCCESS
        assert not saved
    else:
        assert result.status == GenerationStatus.SUCCESS, result
        assert len(saved) == 1
        for name in _NAMES.get(case.get("id"), ()):
            assert name in saved[0].genui
        assert "已经" not in saved[0].genui
        assert len(calls) == 3
