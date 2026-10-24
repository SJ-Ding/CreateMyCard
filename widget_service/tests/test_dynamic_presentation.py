"""确定性增强、生产处理器边界及真实本地渲染绑定求值回归。"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.schemas import GenerateWidgetCardRequest
from config.config import get_settings
from core.errors import GenerationStatus
from custom.a2ui_model_client import A2UIModelClient
from services.card_validation import CompactDslValidationError, validate_card
from services.compact_composition_context import small_card_display_text
from services.dynamic_presentation import (
    add_presentation_dependencies,
    apply_presentation_colors,
    normalize_presentation_source,
    presentation_excluded_roots,
    presentation_rules,
)
from services.generation_pipeline import DesignCompactProcessor, DslProcessingContext
from services.post_layout_theme import apply_theme, components, decode, presets, theme_artifact
from services.presentation_expressions import (
    conditional_colors,
    countdown_number,
    countdown_prefix,
    presentation_policy,
)
from services.protocol_registry import A2UIProtocolRegistry
from services.source_artifact_repository import SourceArtifactRepository
from services.widget_generation_service import WidgetGenerationService
from utils.upload_file_obs import UploadFileOSMS

ROOT = Path(__file__).resolve().parents[2]
DAY = "/data/countdown/countdownDays"
PHONE = "/data/phoneBattery/batterySOC"


def context(capability="GetCountdownDays", root="/data/countdown", fields=None):
    if fields is None:
        fields = {"countdownDays": {"type": "integer", "sampleValue": 30, "description": "天数"}}
    card = {
        "suggestSize": "2x2",
        "dataBindings": [{"capabilityId": capability, "writeResultTo": root, "arguments": {}}],
    }
    task = {
        "size": "2x2", "userQuery": "制作倒计时卡片", "appVersion": "11.7.7.330",
        "dataModelSchema": {"data": {root.removeprefix("/data/"): fields}},
    }
    return card, task


def compact(title="春节", value=None):
    rows = [
        ["root", "Layout", {"layout": "S-title-content"}, ["title", "hero"]],
        ["title", "SingleLineTitle", {"title": title}],
        ["hero", "EmphasizedData", {"value": value or {"path": DAY}, "unit": "天"}],
        [DAY, 30],
    ]
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)


def standard(path=PHONE, *, dark=False, unit="%"):
    color = "#FFFFFFFF" if dark else "#FF111111"
    nodes = [
        {"id": "root", "component": "Column", "styles": {},
         "children": ["label", "value", "unit", "action"]},
        {"id": "label", "component": "Text", "content": "读数", "styles": {"fontColor": color}},
        {"id": "value", "component": "Text", "content": {"path": path},
         "styles": {"fontColor": color}},
        {"id": "unit", "component": "Text", "content": unit, "styles": {"fontColor": color}},
        {"id": "action", "component": "Button", "label": "打开", "styles": {"fontColor": color}},
    ]
    return json.dumps({"updateComponents": {"components": nodes}}, ensure_ascii=False)


def nodes_by_id(source):
    result = {}
    for node in components(decode(source)):
        result[node.get("id")] = node
    return result


@pytest.fixture(scope="module")
def runtime():
    """调用实际 Web 渲染器使用的解析、DataModel 图和属性绑定解析代码。"""
    node = shutil.which("node")
    assert node is not None, "本地渲染回归需要 Node.js"
    sdk = ROOT / "render/genui-sdk"

    def evaluate(source, updates):
        script = """
const fs = require('node:fs');
const { UIGraph } = require('./graph/src/graph.ts');
const { resolvePathBindingsInValue } = require('./interactions/src/resolve-path-bindings.ts');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const graph = new UIGraph();
const values = [];
for (const update of input.updates) {
  graph.setDataModelValue('card', '/', update);
  values.push(resolvePathBindingsInValue(input.source,
    path => graph.getDataModelValue('card', path)));
}
process.stdout.write(JSON.stringify(values));
"""
        result = subprocess.run(
            [node, "--require", "tsx/cjs", "-e", script],
            cwd=sdk, input=json.dumps({"source": source, "updates": updates}),
            text=True, encoding="utf-8", capture_output=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    return evaluate


@pytest.mark.parametrize("size", ["2x2", "2x4"])
def test_countdown_boundaries_refresh_same_expressions(runtime, size):
    card, task = context()
    task["size"] = size
    source = normalize_presentation_source(compact(), card, task)
    assert normalize_presentation_source(source, card, task) == source
    assert "abs(" not in source
    expressions = {"prefix": countdown_prefix(DAY, "春节"), "number": countdown_number(DAY)}
    values = (-8, -1, 0, 1, 7, 8)
    updates = []
    for value in values:
        updates.append({"data": {"countdown": {"countdownDays": value}}})
    resolved = runtime(expressions, updates)
    for value, result in zip(values, resolved, strict=True):
        assert result.get("number") == abs(value)
        assert result.get("prefix") == "距离春节" + ("已经" if value < 0 else "还剩")
    assert runtime(expressions, [{}]) == [{"prefix": "", "number": ""}]


@pytest.mark.parametrize("dark", [False, True])
@pytest.mark.parametrize("formatted", [False, True])
def test_phone_low_color_boundaries_and_unit(runtime, dark, formatted):
    card, task = context("GetPhoneBatteryInfo", "/data/phoneBattery", {
        "batterySOC": {"type": "integer"}, "batterySOCText": {"type": "string"},
    })
    path = "/data/phoneBattery/batterySOCText" if formatted else PHONE
    source = standard(path, dark=dark)
    result = apply_presentation_colors(source, card, task)
    assert apply_presentation_colors(result, card, task) == result
    nodes = nodes_by_id(result)
    value = nodes.get("value")
    unit = nodes.get("unit")
    label = nodes.get("label")
    action = nodes.get("action")
    assert value and unit and label and action
    colors = {"value": value.get("styles"), "unit": unit.get("styles")}
    updates = []
    for number in (0, 9, 10, 100):
        updates.append({"data": {"phoneBattery": {"batterySOC": number}}})
    resolved = runtime(colors, updates)
    policy = presentation_policy()
    red = policy.dark_red if dark else policy.light_red
    normal = "#FFFFFFFF" if dark else "#FF111111"
    for number, entry in zip((0, 9, 10, 100), resolved, strict=True):
        expected = red if number < 10 else normal
        assert entry.get("value") == {"fontColor": expected}
        assert entry.get("unit") == {"fontColor": expected}
    assert label.get("styles") == action.get("styles") == {"fontColor": normal}
    assert runtime(colors, [{}])[0].get("value") == {"fontColor": normal}
    assert apply_presentation_colors(source, card, task, uniform=True) == source


def test_countdown_colors_restore_after_date(runtime):
    card, task = context()
    source = standard(DAY, unit="天")
    nodes = nodes_by_id(apply_presentation_colors(source, card, task))
    value = nodes.get("value")
    assert value
    updates = []
    for number in (-8, -1, 0, 1, 7, 8):
        updates.append({"data": {"countdown": {"countdownDays": number}}})
    resolved = runtime(value.get("styles"), updates)
    for number, entry in zip((-8, -1, 0, 1, 7, 8), resolved, strict=True):
        expected = "#FFB3261E" if 0 <= number <= 7 else "#FF111111"
        assert entry.get("fontColor") == expected


@pytest.mark.parametrize("field", ["batteryLevel", "leftBatteryLevel", "rightBatteryLevel"])
def test_earphone_zero_is_real_data_only_when_connected(runtime, field):
    card, task = context("GetEarphoneInfo", "/data/earphone", {
        field: {"type": "integer"}, "isConnected": {"type": "boolean"},
    })
    nodes = nodes_by_id(apply_presentation_colors(standard("/data/earphone/" + field), card, task))
    value = nodes.get("value")
    assert value
    updates = []
    for connected in (False, True):
        updates.append({"data": {"earphone": {field: 0, "isConnected": connected}}})
    resolved = runtime(value.get("styles"), updates)
    assert resolved == [{"fontColor": "#FF111111"}, {"fontColor": "#FFB3261E"}]


def test_projected_text_adds_only_registered_numeric_dependency():
    card, task = context("GetPhoneBatteryInfo", "/data/phoneBattery", {
        "batterySOCText": {"type": "string", "sampleValue": "68%"},
    })
    capability = {"id": "GetPhoneBatteryInfo", "outputSchema": {"properties": {
        "batterySOC": {"type": "integer", "description": "电量", "sampleValue": 68},
    }}}
    original = copy.deepcopy(task)
    enriched = add_presentation_dependencies(task, card, [capability])
    assert task == original
    assert presentation_rules(card, enriched)
    assert not presentation_rules(card, add_presentation_dependencies(task, card, []))
    source = standard("/data/phoneBattery/batterySOCText")
    assert apply_presentation_colors(source, card, task) == source


def test_countdown_prefix_budget_keeps_complete_target_and_both_branches():
    _, task = context()
    name = "非常重要的项目最终交付纪念日"
    assert small_card_display_text(countdown_prefix(DAY, name), task) == "距离" + name + "还剩"
    assert small_card_display_text(countdown_number(DAY), task) == "30"


def test_ambiguous_countdown_title_is_not_silently_rewritten(monkeypatch):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    card, task = context()
    source = compact().splitlines()
    source[0] = json.dumps([
        "root", "Column", {}, ["title", "other", "hero"],
    ])
    source.append('["other","SingleLineTitle",{"title":"另一个目标"}]')
    with pytest.raises(CompactDslValidationError, match="COUNTDOWN_TARGET") as captured:
        normalize_presentation_source("\n".join(source), card, task)
    assert captured.value.diagnostics
    assert captured.value.diagnostics[0].category == "display"
    result = DesignCompactProcessor().process("\n".join(source), DslProcessingContext(
        size="2x2", card_spec=card, task_spec=task, protocol_profile={}, colorless_layout=True,
    ))
    assert result.errors
    diagnostics = result.errors[0].to_prompt_payload().get("compactDiagnostics")
    assert diagnostics and diagnostics[0].get("fixHint")


@pytest.mark.parametrize("fusion", [False, True])
def test_processor_only_normalizes_actual_fusion_source(monkeypatch, fusion):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    card, task = context()
    result = DesignCompactProcessor().process(compact(), DslProcessingContext(
        size="2x2", card_spec=card, task_spec=task,
        protocol_profile=A2UIProtocolRegistry("a2ui-form-rom6.0-v1").get_profile(),
        colorless_layout=fusion,
    ))
    assert not result.errors, result.issues
    assert ("距离" in result.source_dsl) == fusion
    assert ("距离" in result.standard_dsl) == fusion


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("uniform", [False, True])
async def test_theme_injects_colors_before_each_validation(monkeypatch, fallback, uniform):
    card, task = context("GetPhoneBatteryInfo", "/data/phoneBattery", {
        "batterySOC": {"type": "integer"},
    })
    source = standard()
    theme = next(t for t in presets(task) if t.get("id") == "gradient-blue")
    audit = {"theme": theme, "measurement": {"complete": False}}
    if uniform:
        audit["userColors"] = {"text": {"color": "#FF0000FF"}}
    selected = apply_theme(source, theme, task, audit.get("userColors", {}))
    monkeypatch.setattr(
        "services.post_layout_theme.select_theme", AsyncMock(return_value=(selected, audit)),
    )
    monkeypatch.setattr("services.post_layout_theme.trace_record", Mock())
    artifact = SimpleNamespace(
        genui=source, cardSpec=card, taskSpec=task,
        meta=SimpleNamespace(generationMode="create"),
        model_copy=lambda **_: SimpleNamespace(
            genui=source, cardSpec=card.copy(), taskSpec=task.copy(),
            model_dump=lambda **_: {},
        ),
    )
    seen = []

    def validate(result, _profile):
        value = nodes_by_id(result.genui).get("value")
        assert value
        colors = conditional_colors(value.get("styles", {}).get("fontColor"))
        assert bool(colors) != uniform
        seen.append(result.genui)
        return ["test: rejected selected theme"] if fallback and len(seen) == 1 else []

    await theme_artifact(artifact, task, Mock(), SimpleNamespace(validate=validate), {})
    assert len(seen) == (2 if fallback else 1)


def test_conditional_color_contrast_checks_the_unselected_branch():
    foreground = "{{ ${/data/low} ? '#FFFFFFFF' : '#FF000000' }}"
    rows = [
        {"createSurface": {"surfaceId": "card"}},
        {"updateComponents": {"root": "root", "components": [
            {"id": "root", "component": "Column", "children": ["value"],
             "styles": {"backgroundColor": "#FFFFFFFF"}},
            {"id": "value", "component": "Text", "content": "测试",
             "styles": {"fontColor": foreground}},
        ]}},
        {"updateDataModel": {"path": "/", "value": {"data": {"low": False}}}},
    ]
    report = validate_card(dsl_text="\n".join(json.dumps(row) for row in rows))
    assert report.has_code("VISUAL.CONTRAST")


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["GetCountdownDays", "GetPhoneBatteryInfo"])
@pytest.mark.parametrize("uniform", [False, True])
async def test_service_create_and_edit_compile_without_model_expressions(
    monkeypatch, tmp_path, capability, uniform,
):
    settings = get_settings()
    for name, value in (
        ("WORKSPACE_ROOT", tmp_path), ("artifact_base_url", "https://obs.test/widget"),
        ("enable_widget_edit", True), ("enable_artifact_download_mock", True),
        ("enable_a2ui_model_mock", True), ("enable_artifact_validation", True),
        ("enable_validation_failure_retry", False), ("enable_photo_background_theme", False),
    ):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(WidgetGenerationService, "_enable_card_template", lambda _self: False)
    monkeypatch.setattr(WidgetGenerationService, "_enable_jsx_generation", lambda _self: False)
    monkeypatch.setattr("services.artifact_store.file_obs", UploadFileOSMS(
        base_url=settings.artifact_base_url, mock_storage_dir=tmp_path / "mock_obs",
    ))
    day = capability == "GetCountdownDays"
    root = "/data/countdown" if day else "/data/phoneBattery"
    field = "/countdownDays" if day else "/batterySOCText"
    source = compact()
    if not day:
        source = '\n'.join([
            '["root","Layout",{"layout":"S-center"},["reading"]]',
            '["reading","DataDisplay",{"label":"手机电量",'
            '"value":{"path":"/data/phoneBattery/batterySOCText"},'
            '"supportingText":"当前状态"}]',
            '["/data/phoneBattery/batterySOCText","68%"]',
        ])
    calls = []

    async def generate(_client, _prompt, profile=None, **_kwargs):
        format_name = (profile or {}).get("format")
        calls.append(format_name)
        if _kwargs.get("phase") == "edit_intent":
            return json.dumps({
                "explanation": "调整卡片圆角", "targetSize": "2x2",
                "dimensions": ["shape"], "targets": ["root"],
            })
        if _kwargs.get("phase") == "edit_execute":
            from services.single_step_edit import source_digest

            old = nodes_by_id(original.artifact.genui).get("root", {}).get("styles", {})
            before = {"exists": "borderRadius" in old}
            if "borderRadius" in old:
                before["value"] = old.get("borderRadius")
            return json.dumps({"sourceDigest": source_digest(original.artifact.genui), "changes": [{
                "componentId": "root", "property": "styles.borderRadius",
                "before": before, "value": 18,
            }]})
        return '{"themeId":"gradient-blue"}' if format_name == "raw-json" else source

    monkeypatch.setattr(A2UIModelClient, "generate", generate)
    service = WidgetGenerationService()
    query = "制作春节倒计时卡片" if day else "制作手机电量卡片"
    color_query = "，所有文字使用蓝色" if uniform else ""
    request = GenerateWidgetCardRequest(
        uid="test-user", prdVer="11.7.7.330", device={"romVersion": "7.0"},
        userQuery=query + color_query, title="春节" if day else "手机电量", description="动态读数",
        candidateDataBindings=[{
            "capabilityId": capability, "writeResultTo": root,
            "arguments": {"targetDate": "2027-02-06"} if day else {},
            "candidateOutputFields": [field],
        }],
    )
    created = await service.generate_widget_card_compact_dsl(request)
    assert created.status == GenerationStatus.SUCCESS, created
    assert created.artifactUrl
    original = await asyncio.to_thread(SourceArtifactRepository().load, created.artifactUrl)
    edited = await service.generate_widget_card_compact_dsl(GenerateWidgetCardRequest(
        uid="test-user", prdVer="11.7.7.330", device={"romVersion": "7.0"},
        userQuery="调整布局" + color_query, sourceArtifactUrl=created.artifactUrl,
    ))
    assert edited.status == GenerationStatus.SUCCESS, edited
    assert edited.artifactUrl and edited.artifactUrl != created.artifactUrl
    updated = await asyncio.to_thread(SourceArtifactRepository().load, edited.artifactUrl)
    for artifact in (original.artifact, updated.artifact):
        assert ("距离" in artifact.genui) == day
        conditional = []
        for node in components(decode(artifact.genui)):
            colors = conditional_colors(node.get("styles", {}).get("fontColor"))
            if colors:
                conditional.append(colors)
        assert bool(conditional) != uniform
        if not day and not uniform:
            schema = artifact.taskSpec.get("dataModelSchema", {}).get("data", {})
            numeric = schema.get("phoneBattery", {}).get("batterySOC")
            assert numeric and numeric.get("type") == "integer"
    assert calls == ["compact-dsl", "raw-json", "raw-json", "raw-json"]


@pytest.mark.parametrize("query,expected", [
    ("所有文字使用蓝色", True),
    ("字体红色", True),
    ("电量低于10%时显示红色字体", False),
    ("临近日期之前字体红色", False),
    ("电量低于10%时变红，但所有文字统一使用蓝色", True),
])
def test_uniform_color_does_not_misread_conditional_requests(query, expected):
    from services.dynamic_presentation import uniform_text_request

    assert uniform_text_request(query) is expected


@pytest.mark.parametrize("title,target", [
    ("倒计时", "目标日"), ("距离春节还剩", "春节"), ("O'Brian旅行", "O'Brian旅行"),
])
def test_countdown_targets_are_escaped_and_normalized_without_guessing(runtime, title, target):
    card, task = context()
    result = normalize_presentation_source(compact(title=title), card, task)
    title_props = json.loads(result.splitlines()[1])[2]
    value = title_props.get("title")
    assert runtime(value, [{"data": {"countdown": {"countdownDays": -1}}}]) == [
        "距离" + target + "已经",
    ]


def test_dependency_seeding_does_not_replace_none_or_duplicate_connection():
    card, task = context("GetEarphoneInfo", "/data/earphone", {
        "batteryLevel": {"type": "integer", "sampleValue": 80},
        "leftBatteryLevel": {"type": "integer", "sampleValue": 76},
        "rightBatteryLevel": {"type": "integer", "sampleValue": 78},
        "isConnected": {"type": "boolean", "sampleValue": True},
    })
    source = '\n'.join([
        '["root","EmphasisText",{"mainText":"耳机"}]',
        '["/data/earphone/batteryLevel",null]',
    ])
    result = normalize_presentation_source(source, card, task)
    values = {}
    connection_count = 0
    for line in result.splitlines():
        row = json.loads(line)
        if len(row) == 2:
            values[row[0]] = row[1]
            if row[0] == "/data/earphone/isConnected":
                connection_count += 1
    assert connection_count == 1
    assert "/data/earphone/batteryLevel" in values
    assert values.get("/data/earphone/batteryLevel") is None


@pytest.mark.parametrize("inline,sibling,missing,duplicate", [
    (False, False, True, False),
    (False, True, False, False),
    (True, False, False, False),
    (True, True, False, True),
])
def test_generated_countdown_still_checks_units(inline, sibling, missing, duplicate):
    card, _task = context()
    nodes = [
        {"id": "root", "component": "Column", "children": ["prefix", "value"]},
        {"id": "prefix", "component": "Text", "content": countdown_prefix(DAY, "春节")},
        {"id": "value", "component": "Text",
         "content": countdown_number(DAY, "天" if inline else "")},
    ]
    if sibling:
        root = nodes[0]
        children = root.get("children")
        assert isinstance(children, list)
        children.append("unit")
        nodes.append({"id": "unit", "component": "Text", "content": "天"})
    records = [
        {"createSurface": {"surfaceId": "card"}},
        {"updateComponents": {"root": "root", "components": nodes}},
        {"updateDataModel": {"path": "/", "value": {"data": {"countdown": {"countdownDays": 30}}}}},
    ]
    capability = {"id": "GetCountdownDays", "outputSchema": {"type": "object", "properties": {
        "countdownDays": {"type": "integer", "displayUnits": ["天"], "unitIncluded": False},
    }}}
    reporter = validate_card(artifact={
        "genui": "\n".join(json.dumps(row, ensure_ascii=False) for row in records),
        "cardSpec": card, "effectiveCapabilities": {"data": [capability]},
    })
    assert reporter.has_code("DISPLAY_UNIT_MISSING") is missing
    assert reporter.has_code("DISPLAY_UNIT_DUPLICATED") is duplicate


def test_boolean_display_control_still_colors_earphone_readout(runtime):
    card, task = context("GetEarphoneInfo", "/data/earphone", {
        "leftBatteryLevel": {"type": "integer"}, "isConnected": {"type": "boolean"},
    })
    source = standard("/data/earphone/leftBatteryLevel")
    records = decode(source)
    for node in components(records):
        if node.get("id") == "value":
            node["content"] = (
                "{{ ${/data/earphone/isConnected} ? ${/data/earphone/leftBatteryLevel} "
                "+ '%' : '未连接' }}"
            )
    result = apply_presentation_colors(json.dumps(records[0], ensure_ascii=False), card, task)
    node = nodes_by_id(result).get("value")
    assert node
    assert runtime(node.get("styles"), [{"data": {"earphone": {
        "isConnected": True, "leftBatteryLevel": 9,
    }}}]) == [{"fontColor": "#FFB3261E"}]


def test_wide_countdown_keeps_prefix_inside_its_own_region(monkeypatch):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    card, task = context()
    task["size"] = card["suggestSize"] = "2x4"
    rows = [
        ["root", "Layout", {"layout": "W-split-panels"}, ["left", "right"]],
        ["left", "Region", {"variant": "title-content"}, ["title", "hero"]],
        ["right", "Region", {"variant": "center"}, ["other"]],
        ["title", "SingleLineTitle", {"title": "春节"}],
        ["hero", "EmphasizedData", {"value": {"path": DAY}, "unit": "天"}],
        ["other", "EmphasisText", {"mainText": "已就绪"}],
        [DAY, 30],
    ]
    source = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    result = DesignCompactProcessor().process(source, DslProcessingContext(
        size="2x4", card_spec=card, task_spec=task,
        protocol_profile=A2UIProtocolRegistry("a2ui-form-rom6.0-v1").get_profile(),
        colorless_layout=True,
    ))
    assert not result.errors, result.issues
    assert "距离" in result.standard_dsl and "已就绪" in result.standard_dsl


def test_long_countdown_prefix_is_checked_before_layout_freezes(monkeypatch):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    card, task = context()
    result = DesignCompactProcessor().process(
        compact(title="非常重要的最终项目完整交付纪念日"),
        DslProcessingContext(
            size="2x2", card_spec=card, task_spec=task,
            protocol_profile=A2UIProtocolRegistry("a2ui-form-rom6.0-v1").get_profile(),
            colorless_layout=True,
        ),
    )
    assert result.errors
    assert any("距离" in issue.message for issue in result.errors)


def test_title_and_action_semantic_roots_are_not_colored_by_binding():
    card, task = context("GetPhoneBatteryInfo", "/data/phoneBattery", {
        "batterySOC": {"type": "integer"},
    })
    source = standard()
    records = decode(source)
    for node in components(records):
        if node.get("id") == "label":
            node["content"] = {"path": PHONE}
    compact_source = '\n'.join([
        '["label","SingleLineTitle",{"title":{"path":"/data/phoneBattery/batterySOC"}}]',
        '["action","PillButton",{"label":"打开"}]',
    ])
    roots = presentation_excluded_roots(compact_source)
    colored = apply_presentation_colors(
        json.dumps(records[0]), card, task, excluded_roots=roots,
    )
    nodes = nodes_by_id(colored)
    title = nodes.get("label")
    readout = nodes.get("value")
    assert title and readout
    assert title.get("styles", {}).get("fontColor") == "#FF111111"
    assert conditional_colors(readout.get("styles", {}).get("fontColor"))


def test_upstream_datadisplay_countdown_is_normalized_before_layout(monkeypatch):
    monkeypatch.setattr("services.generation_pipeline.report_ops_metrics", Mock())
    card, task = context()
    source = "\n".join(json.dumps(row, ensure_ascii=False) for row in [
        ["root", "Layout", {"layout": "S-center"}, ["display"]],
        ["display", "DataDisplay", {"label": "春节", "value": {"path": DAY},
                                    "supportingText": "天"}],
        [DAY, 30],
    ])
    result = DesignCompactProcessor().process(source, DslProcessingContext(
        size="2x2", card_spec=card, task_spec=task,
        protocol_profile=A2UIProtocolRegistry("a2ui-form-rom6.0-v1").get_profile(),
        colorless_layout=True,
    ))
    assert not result.errors, result.issues
    assert "距离" in result.standard_dsl
    assert "S-title-content" in result.source_dsl
    normalized = normalize_presentation_source(result.source_dsl, card, task)
    assert normalize_presentation_source(normalized, card, task) == normalized
    units = []
    for node in components(decode(result.standard_dsl)):
        if node.get("content") == "天":
            units.append(node)
    assert len(units) == 1


def test_edit_explicit_uniform_color_releases_only_generated_state_colors():
    from services.dynamic_presentation import has_generated_presentation, prepare_presentation_edit

    card, task = context()
    source = apply_presentation_colors(standard(DAY, unit="天"), card, task)
    assert has_generated_presentation(source, card, task)
    result = prepare_presentation_edit(source, card, task, "所有文字统一使用蓝色")
    assert nodes_by_id(result).get("value", {}).get("styles", {}).get("fontColor") == "#FF0000FF"
    before = nodes_by_id(source).get("value", {}).get("content")
    assert nodes_by_id(result).get("value", {}).get("content") == before
    assert prepare_presentation_edit(source, card, task, "把圆角调大") == source
    assert prepare_presentation_edit(source, card, task, "电量低于10%时文字红色") == source
