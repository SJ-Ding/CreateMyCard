"""展示规则的真实表达式、共享状态、存档和编辑回归。"""

import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from config.config import get_settings
from models.artifact import ArtifactMeta, GenerationPlan, WidgetArtifact
from services.compact_dsl_a2ui_converter import convert_compact_dsl_to_a2ui
from services.compact_plan import build_compact_plan_tool
from services.post_layout_theme import components, decode
from services.presentation.compiler import compile_artifact, digest
from services.presentation.expressions import compile_expression, display_references
from services.presentation.models import Expression, PresentationError, PresentationPlan
from services.presentation.planning import available_fields, scenario_choices, validate_plan
from services.presentation.runtime import (
    compile_with_policy,
    editing_base,
    recompile_edit,
    verify_archive,
)
from services.protocol_registry import A2UIProtocolRegistry
from services.source_artifact_repository import SourceArtifactRepository
from services.theme_style_roles import layout_phase

ROOT = Path(__file__).resolve().parents[2]
DAY = "/data/countdown/countdownDays"


@pytest.fixture
def raw_plan():
    path = ROOT / "widget_service/cloud/data/presentation_presets.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    presets = payload.get("presets")
    assert isinstance(presets, list)
    return {"rules": copy.deepcopy(presets)}


@pytest.fixture
def artifact():
    source = "\n".join(
        [
            '["root","Column",{"width":"matchParent","height":"matchParent"},["title","days"]]',
            '["title","SingleLineTitle",{"title":"毕业倒计时"}]',
            '["days","EmphasizedData",{"value":{"path":"/data/countdown/countdownDays"},"unit":"天"}]',
            '["/data/countdown/countdownDays",101]',
        ]
    )
    with layout_phase():
        genui = convert_compact_dsl_to_a2ui(source, size="2x2")
    return WidgetArtifact(
        genui=genui,
        cardSpec={
            "title": "毕业倒计时",
            "description": "距离毕业的天数",
            "suggestSize": "2x2",
            "dataBindings": [
                {
                    "capabilityId": "GetCountdownDays",
                    "arguments": {"targetDate": "2027-02-02"},
                    "writeResultTo": "/data/countdown",
                }
            ],
        },
        taskSpec={
            "userQuery": "每100天采用主题色彩",
            "size": "2x2",
            "dataModelSchema": {
                "data": {
                    "countdown": {
                        "countdownDays": {
                            "type": "integer",
                            "sampleValue": 101,
                        }
                    }
                }
            },
        },
        effectiveCapabilities={"data": ["GetCountdownDays"]},
        generationPlan=GenerationPlan(
            candidateDataBindings=[
                {
                    "capabilityId": "GetCountdownDays",
                    "arguments": {"targetDate": "2027-02-02"},
                    "writeResultTo": "/data/countdown",
                    "candidateOutputFields": ["/countdownDays"],
                }
            ]
        ),
        meta=ArtifactMeta(
            protocolProfileId="a2ui-form-rom6.0-v1",
            capabilityRegistryVersion="app-11.7.7.300_rom-7.0",
            createdAt=1,
        ),
    )


class RecordingValidator:
    def __init__(self):
        self.sources = []

    def validate(self, artifact, profile):
        del profile
        self.sources.append(artifact.genui)
        assert "@theme:" not in artifact.genui
        return []


def compile_card(artifact, raw_plan, validator=None):
    plan = validate_plan(raw_plan, artifact.taskSpec, artifact.cardSpec)
    return compile_artifact(artifact, plan, validator or RecordingValidator(), {})


def evaluate(source, days):
    node = shutil.which("node")
    if node is None:
        pytest.skip("本地没有Node运行时")
    module = (ROOT / "debug_tools/card_renderer/runtime/interactions/src/expression.ts").as_uri()
    script = f"""
import {{ evaluateExpression }} from {json.dumps(module)};
const input = JSON.parse(await new Response(process.stdin).text());
const outputs = [];
for (const days of input.days) {{
  outputs.push(evaluateExpression(input.expression.slice(2,-2), () => days));
}}
process.stdout.write(JSON.stringify(outputs));
"""
    result = subprocess.run(
        [node, "--experimental-strip-types", "--input-type=module", "-e", script],
        input=json.dumps({"expression": source, "days": days}),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_hundred_day_boundaries_use_actual_sdk(artifact, raw_plan):
    result = compile_card(artifact, raw_plan)
    root = next(item for item in components(decode(result.genui)) if item.get("id") == "root")
    expression = root.get("styles", {}).get("backgroundColor")
    assert isinstance(expression, str)
    days = [None, 0, -100, -200, 1, 99, 100, 101, 199, 200, 201, 300, 1000]
    values = evaluate(expression, days)
    assert len(values) == len(days)
    ordinary, milestone = values[0], values[6]
    assert ordinary != milestone
    for index, day in enumerate(days):
        expected = day is not None and day > 0 and day % 100 == 0
        assert values[index] == (milestone if expected else ordinary)


def test_shared_state_checks_are_independent_of_property_count(artifact, raw_plan):
    validator = RecordingValidator()
    before = copy.deepcopy(artifact)
    result = compile_card(artifact, raw_plan, validator)
    assert len(validator.sources) == 3  # 两个不同的完整主题，以及最终表达式产物
    archive = result.presentationPlan
    assert archive is not None
    assert len(archive.targets) >= 3
    assert artifact == before
    assert digest(result.genui) == archive.compiled_digest


@pytest.mark.parametrize("denominator", [0, False, 2.5, "100"])
def test_modulo_rejects_invalid_constant(artifact, denominator):
    expression = Expression(
        op="%", args=[Expression(op="ref", value=DAY), Expression(op="literal", value=denominator)]
    )
    with pytest.raises(PresentationError):
        compile_expression(expression, available_fields(artifact.taskSpec, artifact.cardSpec))


@pytest.mark.parametrize("op", ["&&", "+", ">", "size"])
def test_types_are_checked_before_compiling(op):
    args = [Expression(op="literal", value=True), Expression(op="literal", value=2)]
    if op == "size":
        args = args[:1]
    with pytest.raises(PresentationError):
        compile_expression(Expression(op=op, args=args), {})


def test_unauthorized_field_is_rejected(artifact, raw_plan):
    artifact.cardSpec["dataBindings"] = []
    with pytest.raises(PresentationError, match="PRESENTATION_FIELD"):
        compile_card(artifact, raw_plan)


def test_numeric_string_schema_is_not_coerced(artifact, raw_plan):
    artifact.taskSpec = {
        "dataModelSchema": {
            "data": {
                "countdown": {
                    "countdownDays": {"type": "string"},
                }
            }
        }
    }
    with pytest.raises(PresentationError, match="PRESENTATION_TYPE"):
        compile_card(artifact, raw_plan)


@pytest.mark.parametrize("value", [1e-20, 1e20, -1e-20])
def test_numeric_literals_do_not_emit_unsupported_exponents(value):
    from services.presentation.expressions import wrapped

    expression = Expression(
        op="+", args=[Expression(op="ref", value=DAY), Expression(op="literal", value=value)]
    )
    compiled = compile_expression(expression, {DAY: "number"})
    assert "e" not in compiled.source
    assert wrapped(compiled.source)


def test_huge_integer_is_a_named_error():
    with pytest.raises(PresentationError, match="PRESENTATION_TYPE"):
        compile_expression(Expression(op="literal", value=10**1000), {})


def test_state_limit_is_bounded_and_optional_rules_can_drop(artifact, raw_plan):
    rules = raw_plan.get("rules")
    assert isinstance(rules, list)
    prototype = rules[0]
    for index in range(1, 4):
        rule = copy.deepcopy(prototype)
        rule["id"] = f"additional-{index}"
        rules.append(rule)
    with pytest.raises(PresentationError, match="PRESENTATION_COMPLEXITY"):
        validate_plan(raw_plan, artifact.taskSpec)
    last = rules[-1]
    last.update(origin="model", requirement="optional")
    plan = validate_plan(raw_plan, artifact.taskSpec)
    assert len(plan.rules) == 3
    assert len(list(scenario_choices(plan))) == 27


def test_model_rule_cannot_claim_required(artifact, raw_plan):
    rule = raw_plan.get("rules")[0]
    rule["origin"] = "model"
    with pytest.raises(PresentationError, match="PRESENTATION_REQUIREMENT"):
        validate_plan(raw_plan, artifact.taskSpec)


def test_missing_explicit_rule_is_not_success(artifact):
    with pytest.raises(PresentationError, match="PRESENTATION_REQUIRED"):
        validate_plan({}, artifact.taskSpec, require_requested=True)


def test_tool_schema_refs_are_valid_and_switch_is_explicit(monkeypatch, artifact):
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_dynamic_presentation", True)
    tool = build_compact_plan_tool(artifact.taskSpec)
    function = tool.get("function")
    assert isinstance(function, dict)
    schema = function.get("parameters")
    assert isinstance(schema, dict)
    Draft202012Validator.check_schema(schema)
    properties = schema.get("properties")
    assert isinstance(properties, dict)
    assert "presentation" in properties
    monkeypatch.setattr(settings, "enable_dynamic_presentation", False)
    function = build_compact_plan_tool(artifact.taskSpec).get("function")
    assert isinstance(function, dict)
    schema = function.get("parameters")
    assert isinstance(schema, dict)
    assert "presentation" not in schema.get("properties", {})


def test_edit_retains_typography_changes_and_can_remove_rules(artifact, raw_plan):
    dynamic = compile_card(artifact, raw_plan)
    baseline = editing_base(dynamic)
    records = decode(baseline.genui)
    target = next(item for item in components(records) if item.get("component") == "Text")
    styles = target.get("styles")
    assert isinstance(styles, dict)
    styles["fontWeight"] = 700
    baseline.genui = "\n".join(json.dumps(row, ensure_ascii=False) for row in records)
    archive = dynamic.presentationPlan
    assert archive is not None
    edited = recompile_edit(dynamic, baseline, archive.plan, RecordingValidator(), {})
    assert "700" in edited.genui
    verify_archive(edited)
    removed = recompile_edit(
        edited,
        editing_base(edited),
        PresentationPlan(),
        RecordingValidator(),
        {},
    )
    assert removed.presentationPlan is None
    assert "% 100" not in removed.genui
    assert "@theme:" not in removed.genui


def test_edit_threshold_is_generic(artifact, raw_plan):
    dynamic = compile_card(artifact, raw_plan)
    changed = copy.deepcopy(raw_plan)
    rule = changed.get("rules")[0]
    when = rule.get("states")[0].get("when")
    assert isinstance(when, dict)
    when.get("args")[1].get("args")[0].get("args")[1]["value"] = 30
    new_plan = validate_plan(changed, artifact.taskSpec)
    edited = recompile_edit(dynamic, editing_base(dynamic), new_plan, RecordingValidator(), {})
    assert "% 30" in edited.genui
    assert "% 100" not in edited.genui


def test_archive_mismatch_is_rejected(artifact, raw_plan):
    dynamic = compile_card(artifact, raw_plan)
    dynamic.genui += "\n"
    with pytest.raises(PresentationError, match="PRESENTATION_DIGEST"):
        editing_base(dynamic)


def test_archive_cannot_inject_actions_via_saved_layout(artifact, raw_plan):
    dynamic = compile_card(artifact, raw_plan)
    archive = dynamic.presentationPlan
    assert archive is not None
    records = decode(archive.layout)
    root = next(node for node in components(records) if node.get("id") == "root")
    root["onClick"] = [{"call": "invented", "args": {}}]
    archive.layout = "\n".join(json.dumps(row) for row in records)
    with pytest.raises(PresentationError, match="PRESENTATION_ARCHIVE"):
        editing_base(dynamic)


def test_optional_group_rolls_back_and_required_group_fails(artifact, raw_plan):
    class Reject:
        def validate(self, artifact, profile):
            return ["invalid palette"]

    with pytest.raises(PresentationError, match="PRESENTATION_REQUIRED_FAILED"):
        compile_with_policy(artifact, raw_plan, Reject(), {})
    rule = raw_plan.get("rules")[0]
    rule.update(origin="model", requirement="optional")
    recovered = compile_with_policy(artifact, raw_plan, Reject(), {})
    assert recovered.presentationPlan is None
    assert recovered.genui == artifact.genui


def test_conditions_do_not_count_as_visible_facts():
    assert display_references("{{ ${/data/countdown/countdownDays} > 0 ? '等待' : '今天' }}") == ()
    assert display_references("{{ ${/data/a} ? ${/data/b} : ${/data/b} }}") == ("/data/b",)


@pytest.mark.parametrize("slot_component", ["EmphasisText", "SingleLineTitle"])
def test_planned_text_slots_have_owned_conversion_targets(artifact, raw_plan, slot_component):
    from services.generation_pipeline import DesignCompactProcessor, DslProcessingContext

    plan = validate_plan(raw_plan, artifact.taskSpec, artifact.cardSpec)
    rule = plan.rules[0]
    rule.states[0].theme = None
    rule.default.theme = None
    rule.unknown.theme = None
    rule.states[0].texts = {"note": Expression(op="literal", value="整百日")}
    rule.default.texts = {"note": Expression(op="literal", value="继续期待")}
    rule.unknown.texts = {"note": Expression(op="literal", value="暂未更新")}
    marker = "@presentation:" + rule.id + ":note"
    props = {"mainText": "毕业", "secondaryText": marker}
    if slot_component == "SingleLineTitle":
        props = {"title": marker}
    rows = [
        ["root", "Column", {"width": "matchParent", "height": "matchParent"}, ["status"]],
        ["status", slot_component, props],
        [DAY, 101],
    ]
    context = DslProcessingContext(
        size="2x2",
        card_spec=artifact.cardSpec,
        task_spec={**artifact.taskSpec, "appVersion": "11.7.7.331"},
        protocol_profile={},
        colorless_layout=True,
        skip_compact_dsl_validation=True,
        compact_plan={
            "presentation": plan.model_dump(mode="json"),
            "info_required": [
                {
                    "requirement": "展示毕业",
                    "priority": "must",
                    "text": "毕业",
                }
            ],
        },
    )
    result = DesignCompactProcessor().process(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        context,
    )
    if slot_component == "SingleLineTitle":
        assert any(issue.code == "PRESENTATION_SLOT" for issue in result.errors)
        return
    assert not result.errors, result.errors
    assert len(result.presentation_targets) == 1
    target = result.presentation_targets[0]
    assert target.source_id == "status"
    assert marker not in result.standard_dsl
    artifact.genui = result.standard_dsl
    compiled = compile_artifact(
        artifact, plan, RecordingValidator(), {}, text_targets=result.presentation_targets
    )
    baseline = editing_base(compiled)
    assert "@theme:" not in baseline.genui
    assert "继续期待" in baseline.genui


def test_explicit_fixed_colors_survive_unrelated_edit(artifact, raw_plan):
    artifact.taskSpec["userQuery"] = "所有文字统一红色"
    original = compile_card(artifact, raw_plan)
    baseline = editing_base(original)
    baseline.taskSpec["userQuery"] = "间距稍微增大"
    archive = original.presentationPlan
    assert archive is not None
    edited = recompile_edit(original, baseline, archive.plan, RecordingValidator(), {})
    final_archive = edited.presentationPlan
    assert final_archive is not None
    assert final_archive.color_overrides == archive.color_overrides


def test_array_condition_preserves_empty_collection():
    from services.presentation.expressions import present, wrapped

    task = {
        "dataModelSchema": {
            "data": {
                "calendar": {
                    "events": [
                        {
                            "title": {"type": "string"},
                        }
                    ]
                }
            }
        }
    }
    fields = available_fields(task)
    path = "/data/calendar/events"
    assert fields.get(path) == "array"
    expression = Expression(
        op="==",
        args=[
            Expression(op="size", args=[Expression(op="ref", value=path)]),
            Expression(op="literal", value=0),
        ],
    )
    compiled = compile_expression(expression, fields)
    source = wrapped(f"({present(compiled.references, fields)} && {compiled.source})")
    assert evaluate(source, [[], [1], None]) == [True, False, False]


def test_archive_roundtrip_uses_optional_block(artifact, raw_plan):
    result = compile_card(artifact, raw_plan)
    payload = result.model_dump(mode="json", exclude_none=True)
    names = {
        "schema": {"schemaVersion": "widget-artifact-v2"},
        "cardspec": payload.get("cardSpec"),
        "taskspec": payload.get("taskSpec"),
        "meta": payload.get("meta"),
        "effectivecapabilities": {},
        "removedcapabilities": [],
        "generationplan": payload.get("generationPlan"),
        "presentationplan": payload.get("presentationPlan"),
    }
    blocks = [f"```genui\n{result.genui}\n```"]
    for name, value in names.items():
        blocks.append("```" + name + "\n" + json.dumps(value) + "\n```")
    parsed = SourceArtifactRepository().parse_document("\n".join(blocks)).artifact
    assert parsed.presentationPlan == result.presentationPlan
    verify_archive(parsed)


def test_full_validator_accepts_all_hundred_day_states(artifact, raw_plan):
    from services.validator import ArtifactValidator

    profile = A2UIProtocolRegistry("a2ui-form-rom6.0-v1").get_profile()
    plan = validate_plan(raw_plan, artifact.taskSpec, artifact.cardSpec)
    result = compile_artifact(artifact, plan, ArtifactValidator(), profile)
    assert result.presentationPlan is not None


def test_local_palette_does_not_change_root_background(artifact, raw_plan):
    plan = validate_plan(raw_plan, artifact.taskSpec, artifact.cardSpec)
    rule = plan.rules[0]
    rule.scope = "local"
    identities = []
    for node in components(decode(artifact.genui)):
        if node.get("component") == "Text":
            identities.append(node.get("id"))
    rule.local_targets = identities
    result = compile_card(artifact, plan)
    root = next(node for node in components(decode(result.genui)) if node.get("id") == "root")
    assert "{{" not in root.get("styles", {}).get("backgroundColor", "")
    assert "% 100" in result.genui


def test_higher_priority_global_state_wins(artifact, raw_plan):
    plan = validate_plan(raw_plan, artifact.taskSpec, artifact.cardSpec)
    urgent = plan.rules[0].model_copy(deep=True)
    urgent.id = "warning"
    urgent.states[0].priority = "warning"
    urgent.states[0].theme = "solid-purple"
    plan.rules.append(urgent)
    result = compile_card(artifact, plan)
    root = next(node for node in components(decode(result.genui)) if node.get("id") == "root")
    expression = root.get("styles", {}).get("backgroundColor")
    assert isinstance(expression, str)
    values = evaluate(expression, [101, 100, 99])
    assert values == ["#FFF1F6FE", "#FFF6F2FF", "#FFF1F6FE"]


@pytest.mark.asyncio
@pytest.mark.parametrize("compile_failure", [False, True])
@pytest.mark.parametrize("app,rom", [("11.7.7.331", "7.0"), ("11.7.8.340", "6.0")])
async def test_real_create_pipeline_saves_required_plan(
    monkeypatch, raw_plan, compile_failure, app, rom
):
    from api.schemas import GenerateWidgetCardRequest
    from core.errors import GenerationStatus
    from custom.a2ui_model_client import A2UIModelClient
    from services.artifact_store import ArtifactStore
    from services.widget_generation_service import WidgetGenerationService

    settings = get_settings()
    monkeypatch.setattr(settings, "enable_dynamic_presentation", True)
    monkeypatch.setattr(settings, "enable_a2ui_model_mock", False)
    monkeypatch.setattr(settings, "enable_artifact_validation", False)
    monkeypatch.setattr(settings, "enable_generation_trace_recording", False)
    monkeypatch.setattr(WidgetGenerationService, "_enable_card_template", lambda _self: True)
    monkeypatch.setattr(WidgetGenerationService, "_enable_jsx_generation", lambda _self: True)
    source = "\n".join(
        [
            '["root","Layout",{"layout":"S-title-content"},["title","days"]]',
            '["title","SingleLineTitle",{"title":"毕业倒计时"}]',
            '["days","EmphasizedData",{"value":{"path":"/data/countdown/countdownDays"},"unit":"天"}]',
            '["/data/countdown/countdownDays",101]',
        ]
    )
    calls = []
    saved = []

    async def generate(_client, messages, profile=None, **kwargs):
        del messages
        calls.append(kwargs.get("phase"))
        if (profile or {}).get("format") == "raw-json":
            return json.dumps(
                {
                    "name": "submit_card_plan",
                    "arguments": {
                        "info_required": [
                            {
                                "requirement": "剩余天数",
                                "priority": "must",
                                "dataId": DAY,
                                "componentHints": ["EmphasizedData"],
                            }
                        ],
                        "layoutHints": ["S-title-content"],
                        "presentation": raw_plan,
                    },
                },
                ensure_ascii=False,
            )
        return source

    async def save(_store, result):
        saved.append(result)
        return SimpleNamespace(
            artifactUrl="https://artifact.test/presentation.md", artifactDigest="sha256:compiled"
        )

    async def forbidden(*_args, **_kwargs):
        pytest.fail("必需联动不能被模板提前返回")

    monkeypatch.setattr(A2UIModelClient, "generate", generate)
    monkeypatch.setattr(ArtifactStore, "save", save)
    monkeypatch.setattr("services.template_generation.TemplateSourceGenerator.__call__", forbidden)
    if compile_failure:

        def reject(*_args, **_kwargs):
            raise PresentationError("PRESENTATION_BRANCH", "模拟非法主题分支")

        monkeypatch.setattr("services.presentation.runtime.compile_artifact", reject)
    request = GenerateWidgetCardRequest(
        uid="presentation-test",
        prdVer=app,
        device={"romVersion": rom},
        userQuery="毕业倒计时，每100天采用主题色彩",
        size="2x2",
        title="毕业倒计时",
        description="距离毕业的天数",
        candidateDataBindings=[
            {
                "capabilityId": "GetCountdownDays",
                "arguments": {"targetDate": "2027-02-02"},
                "writeResultTo": "/data/countdown",
                "candidateOutputFields": ["/countdownDays"],
            }
        ],
    )
    response = await WidgetGenerationService().generate_widget_card_compact_dsl(request)
    if compile_failure:
        assert response.status == GenerationStatus.FAILED
        assert not saved
        return
    assert response.status == GenerationStatus.SUCCESS, response
    assert len(saved) == 1
    assert saved[0].presentationPlan is not None
    assert "theme" not in calls
    assert "% 100" in saved[0].genui
    verify_archive(saved[0])


@pytest.mark.asyncio
async def test_actual_store_preserves_optional_plan_block(
    monkeypatch, tmp_path, artifact, raw_plan
):
    from services import artifact_store

    result = compile_card(artifact, raw_plan)
    monkeypatch.setattr(get_settings(), "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(get_settings(), "enable_generation_trace_recording", False)

    async def upload(path):
        assert Path(path).is_file()
        return "https://artifact.test/compiled.md"

    monkeypatch.setattr(artifact_store.file_obs, "upload_file", upload)
    saved = await artifact_store.ArtifactStore().save(result)
    assert saved.artifactUrl
    files = list(tmp_path.glob("artifact_*.md"))
    assert len(files) == 1
    document = files[0].read_text(encoding="utf-8")
    assert "```presentationplan\n" in document
    parsed = SourceArtifactRepository().parse_document(document).artifact
    assert parsed.presentationPlan == result.presentationPlan
    verify_archive(parsed)


def test_twenty_five_properties_still_share_two_visible_states(artifact, raw_plan):
    records = decode(artifact.genui)
    nodes = components(records)
    root = next(node for node in nodes if node.get("id") == "root")
    children = root.get("children")
    assert isinstance(children, list)
    for index in range(25):
        identity = "status-" + str(index)
        nodes.append(
            {
                "id": identity,
                "component": "Text",
                "content": "状态",
                "styles": {"fontColor": "@theme:secondary"},
            }
        )
        children.append(identity)
    artifact.genui = "\n".join(json.dumps(row, ensure_ascii=False) for row in records)
    validator = RecordingValidator()
    compiled = compile_card(artifact, raw_plan, validator)
    assert compiled.presentationPlan is not None
    assert len(compiled.presentationPlan.targets) >= 25
    assert len(validator.sources) == 3
    after = decode(compiled.genui)
    assert records[0] == after[0]
    assert records[2] == after[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_real_edit_pipeline_updates_only_period(monkeypatch, artifact, raw_plan, enabled):
    from api.schemas import GenerateWidgetCardRequest
    from core.errors import GenerationStatus
    from custom.a2ui_model_client import A2UIModelClient
    from services.artifact_store import ArtifactStore
    from services.source_artifact_repository import SourceArtifactLoadResult
    from services.widget_generation_service import WidgetGenerationService

    dynamic = compile_card(artifact, raw_plan)
    changed = validate_plan(raw_plan, artifact.taskSpec, artifact.cardSpec)
    denominator = changed.rules[0].states[0].when.args[1].args[0].args[1]
    assert denominator.op == "literal"
    denominator.value = 30
    loaded = SourceArtifactLoadResult(
        artifact=dynamic,
        design_token=None,
        artifact_digest="sha256:source",
        url_hash="source",
        read_latency_ms=0.0,
        parse_latency_ms=0.0,
        download_mode="mock",
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_widget_edit", True)
    monkeypatch.setattr(settings, "enable_dynamic_presentation", enabled)
    monkeypatch.setattr(settings, "enable_semantic_widget_edit", True)
    monkeypatch.setattr(settings, "enable_generation_trace_recording", False)
    saved = []
    calls = []

    async def generate(_client, messages, profile=None, **kwargs):
        del messages, profile
        calls.append(kwargs.get("phase"))
        return json.dumps(
            {
                "outcome": "ready",
                "explanation": "改为每30天切换",
                "targetSize": "2x2",
                "targets": ["root"],
                "dimensions": ["palette"],
                "presentation": changed.model_dump(mode="json"),
                "presentationOnly": True,
            },
            ensure_ascii=False,
        )

    async def save(_store, result):
        saved.append(result)
        return SimpleNamespace(
            artifactUrl="https://artifact.test/edited.md", artifactDigest="sha256:edited"
        )

    monkeypatch.setattr(SourceArtifactRepository, "load", lambda *_args: loaded)
    monkeypatch.setattr(A2UIModelClient, "generate", generate)
    monkeypatch.setattr(ArtifactStore, "save", save)
    request = GenerateWidgetCardRequest(
        uid="presentation-edit",
        prdVer="11.7.7.331",
        device={"romVersion": "7.0"},
        userQuery="把主题周期改为每30天，其他不变",
        sourceArtifactUrl="https://old.test/card.md",
    )
    response = await WidgetGenerationService().generate_widget_card_compact_dsl(request)
    if not enabled:
        assert response.status == GenerationStatus.UNSUPPORTED
        assert not calls
        assert not saved
        return
    assert response.status == GenerationStatus.SUCCESS, response
    assert calls == ["edit_intent"]
    assert len(saved) == 1
    assert "% 30" in saved[0].genui
    assert "% 100" not in saved[0].genui
    verify_archive(saved[0])


@pytest.mark.parametrize("enabled", [False, True])
def test_release_uses_only_feature_switch(monkeypatch, enabled):
    from services.presentation.release import presentation_enabled

    monkeypatch.setattr(get_settings(), "enable_dynamic_presentation", enabled)
    assert presentation_enabled() is enabled


@pytest.mark.asyncio
async def test_disabled_switch_rejects_required_create(monkeypatch):
    from api.schemas import GenerateWidgetCardRequest
    from core.errors import GenerationStatus
    from custom.a2ui_model_client import A2UIModelClient
    from services.widget_generation_service import WidgetGenerationService

    monkeypatch.setattr(get_settings(), "enable_dynamic_presentation", False)

    async def forbidden(*_args, **_kwargs):
        pytest.fail("关闭动态展示后不能调用模型生成必需联动")

    monkeypatch.setattr(A2UIModelClient, "generate", forbidden)
    request = GenerateWidgetCardRequest(
        uid="presentation-switch-test",
        prdVer="11.7.8.340",
        device={"romVersion": "6.0"},
        userQuery="毕业倒计时，每100天采用主题色彩",
        size="2x2",
        title="毕业倒计时",
        description="距离毕业的天数",
    )
    response = await WidgetGenerationService().generate_widget_card_compact_dsl(request)
    assert response.status == GenerationStatus.UNSUPPORTED
