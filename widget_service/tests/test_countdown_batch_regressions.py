"""冻结失败批次的真实 Plan／DSL，验证增强与正式校验的衔接。"""

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
from services.card_validation import CompactDslValidationError
from services.compact_plan import evaluate_compact_plan_coverage
from services.dynamic_presentation import normalize_presentation_source
from services.generation_pipeline import DesignCompactProcessor, DslProcessingContext
from services.presentation_expressions import countdown_number, countdown_prefix, countdown_readout
from services.protocol_registry import A2UIProtocolRegistry
from services.widget_generation_service import WidgetGenerationService

_FIXTURE = Path(__file__).parent / "fixtures/countdown_batch_20261023_223950.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8")).get("cases")
assert isinstance(_CASES, list)
_REMAINING_ERRORS = {
    "Q005-2": "COUNTDOWN_TARGET",
    "Q005-4": "COUNTDOWN_TARGET",
    "Q039-1": "vertical layout requires",
    "Q039-3": "vertical layout requires",
    "Q041-1": "vertical layout requires",
    "Q041-3": "vertical layout requires",
    "Q108-1": "requires title and time",
    "Q108-2": "Plan static text is not fully visible",
    "Q109-1": "vertical layout requires",
    "Q109-2": "vertical layout requires",
    "Q109-3": "SummaryList.items requires 2 to 3 entries",
    "Q109-4": "Plan static text is not fully visible",
    "Q107-1": "Countdown request fact missing",
    "Q107-2": "Countdown request fact missing",
    "Q107-3": "Countdown request fact missing",
}
_DAY = "/data/countdown/countdownDays"


def _case(identifier: str) -> dict:
    result = next(item for item in _CASES if item.get("id") == identifier)
    return copy.deepcopy(result)


def _process(case: dict):
    card = case.get("card")
    assert isinstance(card, dict)
    return DesignCompactProcessor().process(case.get("source"), DslProcessingContext(
        size=card.get("suggestSize"), card_spec=card, task_spec=case.get("task"),
        compact_plan=case.get("plan"), colorless_layout=True,
        protocol_profile=A2UIProtocolRegistry("a2ui-form-rom6.0-v1").get_profile(),
    ))


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.get("id"))
def test_recorded_countdown_sources_keep_real_gates_without_expression_false_positives(
    monkeypatch, case,
):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    source = case.get("source")
    assert isinstance(source, str)
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == case.get("sourceSha256")
    result = _process(case)
    messages = "\n".join(issue.message for issue in result.errors)
    assert "expected 1, got 6" not in messages
    assert not any("单焦点 S-center" in issue.message for issue in result.errors)
    remaining = _REMAINING_ERRORS.get(case.get("id"))
    if remaining is not None:
        assert remaining in messages
    else:
        assert not result.errors, result.issues
        assert result.standard_dsl
        assert "还剩" in result.source_dsl and "已过" in result.source_dsl
        normalized = normalize_presentation_source(source, case.get("card"), case.get("task"))
        assert normalize_presentation_source(
            normalized, case.get("card"), case.get("task"),
        ) == normalized


@pytest.mark.parametrize("change", ["none", "duplicate", "control_only", "arbitrary"])
def test_plan_counts_display_slots_but_keeps_true_duplicates(change):
    case = _case("Q101-1")
    prefix = countdown_prefix(_DAY, "本周日")
    number = countdown_number(_DAY)
    rows = [
        ["root", "Column", {}, ["title", "value"]],
        ["title", "SingleLineTitle", {"title": prefix}],
        ["value", "EmphasizedData", {"value": number, "unit": "天"}],
    ]
    if change == "duplicate":
        rows[0][3].append("other")
        rows.append(["other", "EmphasizedData", {"value": number, "unit": "天"}])
    elif change == "control_only":
        rows[2][2]["value"] = "未知"
    elif change == "arbitrary":
        rows[2][2]["value"] = "{{ ${" + _DAY + "} + ${" + _DAY + "} }}"
    source = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    result = evaluate_compact_plan_coverage(source, case.get("plan"), case.get("task"))
    if change == "none":
        assert not result.errors
    elif change == "control_only":
        assert result.errors and not result.duplicate_facts
    else:
        assert result.duplicate_facts
        fact = result.duplicate_facts[0]
        assert fact.get("actualVisibleCount") == 2


def test_dynamic_target_name_and_literal_fact_remain_visible_to_plan():
    case = _case("Q101-1")
    rows = [
        ["root", "Column", {}, ["title", "value"]],
        ["title", "SingleLineTitle", {"title": countdown_prefix(
            _DAY, "/data/countdown/name", target_path=True,
        )}],
        ["value", "EmphasizedData", {"value": countdown_number(_DAY), "unit": "天"}],
    ]
    plan = copy.deepcopy(case.get("plan"))
    facts = plan.get("info_required")
    assert isinstance(facts, list)
    facts.append({"dataId": "/data/countdown/name", "requirement": "目标名称"})
    result = evaluate_compact_plan_coverage(
        "\n".join(json.dumps(row) for row in rows), plan, case.get("task"),
    )
    assert not result.errors
    literal_case = _case("Q106-2")
    normalized = normalize_presentation_source(
        literal_case.get("source"), literal_case.get("card"), literal_case.get("task"),
    )
    result = evaluate_compact_plan_coverage(
        normalized, literal_case.get("plan"), literal_case.get("task"),
    )
    assert not result.errors


def test_mixed_event_names_are_not_discarded_during_normalization():
    case = _case("Q005-1")
    source = case.get("source")
    assert isinstance(source, str)
    source = source.replace("中秋节倒计时", "另一个事件")
    source = source.replace(
        "${/data/countdown/midAutumn/countdownDays} + '天'",
        "'中秋节 ' + ${/data/countdown/midAutumn/countdownDays} + '天'",
    )
    with pytest.raises(CompactDslValidationError, match="COUNTDOWN_TARGET"):
        normalize_presentation_source(source, case.get("card"), case.get("task"))


def test_generated_info_block_prefix_cannot_use_regular_ellipsis_exemption(monkeypatch):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    case = _case("Q005-1")
    source = case.get("source")
    assert isinstance(source, str)
    case["source"] = source.replace("中秋节倒计时", "非常重要的项目最终完整交付纪念日")
    result = _process(case)
    assert any("text width overflow" in issue.message for issue in result.errors)


def test_info_block_raw_day_binding_gets_exactly_one_inline_unit(monkeypatch):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    case = _case("Q005-1")
    rows = []
    for line in case.get("source", "").splitlines():
        row = json.loads(line)
        if len(row) > 2 and row[0] == "first":
            row[2]["primaryText"] = {"path": "/data/countdown/midAutumn/countdownDays"}
        rows.append(row)
    case["source"] = "\n".join(json.dumps(row) for row in rows)
    result = _process(case)
    assert not result.errors, result.issues
    values = []
    for line in result.source_dsl.splitlines():
        row = json.loads(line)
        if row[0] == "first":
            values.append(row[2].get("primaryText"))
    assert values == [countdown_readout("/data/countdown/midAutumn/countdownDays")]


@pytest.mark.parametrize("kind", ["unit", "inline_unit", "different_path", "plain_title"])
def test_single_focus_layout_exception_requires_same_canonical_day_and_unit(kind):
    from services.card_validation.compact_validation.semantic.plan_contracts import (
        is_normalized_countdown_focus,
    )
    from services.compact_dsl_a2ui_converter import parse_compact_dsl_rows
    from services.compact_high_level_components import ComponentRow

    case = _case("Q011-2")
    source = normalize_presentation_source(case.get("source"), case.get("card"), case.get("task"))
    if kind == "unit":
        source = source.replace('"unit":"天"', '"unit":"%"')
    elif kind == "inline_unit":
        source = source.replace(countdown_number(_DAY), countdown_number(_DAY, "天"))
    elif kind == "different_path":
        source = source.replace(countdown_number(_DAY), countdown_number("/data/other/days"))
    else:
        source = source.replace(countdown_prefix(_DAY, "国庆中秋节"), "普通标题")
    rows = []
    for row in parse_compact_dsl_rows(source, size="2x2"):
        if isinstance(row, ComponentRow):
            rows.append(row)
    assert not is_normalized_countdown_focus(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["Q005-1", "Q011-2", "Q069-1"])
async def test_fixed_recorded_plan_and_model_output_pass_full_service(
    monkeypatch, identifier,
):
    case = _case(identifier)
    settings = get_settings()
    for name, value in (
        ("enable_a2ui_model_mock", False), ("enable_artifact_validation", True),
        ("enable_validation_failure_retry", False), ("enable_photo_background_theme", False),
    ):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(WidgetGenerationService, "_enable_card_template", lambda _self: False)
    monkeypatch.setattr(WidgetGenerationService, "_enable_jsx_generation", lambda _self: False)
    calls = []
    saved = []

    async def generate(_client, _messages, profile=None, **_kwargs):
        profile = profile or {}
        calls.append(profile.get("id"))
        if profile.get("id") == "compact-info-plan":
            return json.dumps({"name": "submit_card_plan", "arguments": case.get("plan")})
        if profile.get("format") == "raw-json":
            return '{"themeId":"gradient-blue"}'
        return case.get("source")

    async def save(_store, artifact):
        saved.append(artifact)
        return SimpleNamespace(artifactUrl="https://obs.test/countdown.md", artifactDigest="test")

    monkeypatch.setattr(A2UIModelClient, "generate", generate)
    monkeypatch.setattr(ArtifactStore, "save", save)
    card, task = case.get("card"), case.get("task")
    assert isinstance(card, dict) and isinstance(task, dict)
    assets = []
    for asset in task.get("assetCandidates", []):
        assets.append(asset.get("id"))
    request = GenerateWidgetCardRequest(
        uid="countdown-regression", prdVer="11.7.7.330", device={"romVersion": "7.0"},
        userQuery=task.get("userQuery"), title=card.get("title"),
        description=card.get("description"), size=card.get("suggestSize"),
        candidateDataBindings=card.get("dataBindings"), candidateAssetIds=assets,
    )
    response = await WidgetGenerationService().generate_widget_card_compact_dsl(request)
    assert response.status == GenerationStatus.SUCCESS, response
    assert len(saved) == 1
    assert calls[0] == "compact-info-plan"
    assert len(calls) == 3
    artifact = saved[0]
    assert "还剩" in artifact.genui and "已过" in artifact.genui
    assert "#FFB3261E" in artifact.genui or "#FFFF6B6B" in artifact.genui
