"""动态增强的语法、全部分支、原子回退及最终产物回归。"""

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config.config import Settings, get_settings
from models.artifact import ArtifactMeta, WidgetArtifact
from services.artifact_store import ArtifactStore
from services.asset_url_mapper import AssetUrlMapper
from services.card_validation import ValidationOptions, validate_card
from services.card_validation.enhancement_expression import parse_expression, result_variants
from services.card_validation.enhancement_rules import (
    available_fields,
    available_icons,
    validate_changes,
)
from services.dynamic_expression_enhancement import enhance_artifact
from services.post_layout_theme import components, decode, encode
from services.source_artifact_repository import SourceArtifactRepository
from services.validator import ArtifactValidator
from test_asset_mapping_generation import storage as storage
from test_edit_artifact_validation import _standard_dsl

PATH = "/data/weather/temperature"
SRC = "resources/base/media/drop_1.svg"
OTHER = "resources/base/media/alarm_fill_1.svg"
EXPR = "{{ ${" + PATH + "} < 10 ? '注意保暖' : '适宜出行' }}"
COLOR = "{{ ${" + PATH + "} < 10 ? '#FF123456' : '#FF345678' }}"


def change(identity="advice", prop="content", expression=EXPR):
    return {"componentId": identity, "property": prop, "expression": expression}


@pytest.fixture
def artifact():
    nodes = [
        {"id": "root", "component": "Column", "children": ["advice", "number", "icon"],
         "styles": {"width": "matchParent", "height": "matchParent",
                    "backgroundColor": "#FFFFFFFF"}},
        {"id": "advice", "component": "Text", "content": "出行建议",
         "styles": {"fontSize": 14, "fontColor": "#FF111111", "maxLines": 1}},
        {"id": "number", "component": "Text", "content": "{{ ${" + PATH + "} + '℃' }}",
         "styles": {"fontSize": 24, "fontColor": "#FF111111", "maxLines": 1}},
        {"id": "icon", "component": "Image", "src": SRC,
         "styles": {"width": 24, "height": 24}},
    ]
    records = [
        {"version": "v0.9", "createSurface": {"surfaceId": "card", "catalogId": "test"}},
        {"version": "v0.9", "updateComponents": {
            "surfaceId": "card", "root": "root", "components": nodes}},
        {"version": "v0.9", "updateDataModel": {
            "surfaceId": "card", "path": "/", "value": {"data": {
                "weather": {"temperature": 20}}}}},
    ]
    return WidgetArtifact(
        genui=encode(records),
        cardSpec={"title": "天气", "dataBindings": [
            {"capabilityId": "weather", "writeResultTo": "/data/weather"}]},
        taskSpec={"userQuery": "天气与出行建议", "dataModelSchema": {"data": {"weather": {
            "temperature": {"type": "number", "description": "温度", "sampleValue": 20}}}},
            "assetCandidates": [{"id": "drop", "src": SRC}, {"id": "alarm", "src": OTHER}]},
        effectiveCapabilities={"data": ["weather"], "asset": ["drop", "alarm"]},
        meta=ArtifactMeta(protocolProfileId="test", capabilityRegistryVersion="test", createdAt=0),
    )


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_dynamic_expression_enhancement", True)


def validate(artifact, payload):
    indexed = {node.get("id"): node for node in components(decode(artifact.genui))}
    return validate_changes(
        payload, indexed, available_fields(artifact), available_icons(artifact, {}),
    )


def test_switch_defaults_and_environment(monkeypatch):
    monkeypatch.delenv("WIDGET_SERVICE_ENABLE_DYNAMIC_EXPRESSION_ENHANCEMENT", raising=False)
    assert Settings(_env_file=None).enable_dynamic_expression_enhancement is False
    monkeypatch.setenv("WIDGET_SERVICE_ENABLE_DYNAMIC_EXPRESSION_ENHANCEMENT", "true")
    assert Settings(_env_file=None).enable_dynamic_expression_enhancement is True


@pytest.mark.parametrize("temperature", [9, 10, 19, 20, 100])
def test_nested_ternary_keeps_all_boundaries(artifact, temperature):
    artifact.taskSpec.get("dataModelSchema").get("data").get("weather").get(
        "temperature")["sampleValue"] = temperature
    expression = "{{ ${" + PATH + "} < 10 ? '#FF123456' : ( ${" + PATH
    expression += "} < 20 ? '#FF345678' : '#FF56789A') }}"
    changes = validate(artifact, [change("number", "styles.fontColor", expression)])
    assert len(changes) == 1
    assert changes[0].variants == ("#FF123456", "#FF345678", "#FF56789A")


@pytest.mark.parametrize("expression", [
    "{{ ${/data/weather/temperature} < 0 ? 'a' }}",
    "{{ ${/data/weather/temperature} < 0 ? 'a' : 'b' ) }}",
    "{{ ${/data/weather/temperature} === 0 ? 'a' : 'b' }}",
    "{{ fn(${/data/weather/temperature}) ? 'a' : 'b' }}",
    '{{ ${/data/weather/temperature} < 0 ? "a" : "b" }}',
    "{{ ${/data/weather/temperature} < 0 ? 'a' : 'b'; evil() }}",
    "{{ ${/data/weather/temperature} }}",
    "{{ true ? 'a' : 'b' }}",
    "{{ ${relative} < 0 ? 'a' : 'b' }}",
    "prefix {{ ${/data/weather/temperature} < 0 ? 'a' : 'b' }}",
])
def test_parser_rejects_invalid_or_unrestricted_syntax(expression):
    with pytest.raises(ValueError):
        parse_expression(expression)


@pytest.mark.parametrize("payload", [
    {}, [change("missing")], [change("advice", "styles.fontSize")],
    [change(), change()], [dict(change(), extra="not allowed")],
    [change(expression="{{ ${/data/secret/value} < 0 ? '保暖' : '出行' }}")],
    [change(expression="{{ ${/data/weather/temperature} ? '保暖' : '出行' }}")],
    [change(expression="{{ ${/data/weather/temperature} < '0' ? '保暖' : '出行' }}")],
    [change("number", "styles.fontColor", "{{ ${" + PATH + "} < 10 ? '#FF123456' : 'red' }}")],
    [change("icon", "src", "{{ ${" + PATH + "} < 10 ? '" + SRC + "' : 'unknown.svg' }}")],
    [change("number", "content", EXPR)],
    [change(expression="{{ ${" + PATH + "} < 10 ? '很长的出行建议文字' : '保暖' }}")],
    [change(expression="{{ ${" + PATH + "} / 0 < 10 ? '保暖' : '出行' }}")],
])
def test_semantic_rejections_are_not_limited_to_current_branch(artifact, payload):
    with pytest.raises(ValueError):
        validate(artifact, payload)


def test_authorized_projection_excludes_arbitrary_first_frame_and_unbound_schema(artifact):
    artifact.taskSpec.get("dataModelSchema")["ui"] = {"secret": {"type": "number"}}
    assert set(available_fields(artifact)) == {PATH}
    artifact.effectiveCapabilities["data"] = []
    assert available_fields(artifact) == {}


def test_schema_field_named_type_and_inline_effective_entries(artifact):
    artifact.effectiveCapabilities["data"] = [{"capabilityId": "weather"}]
    artifact.effectiveCapabilities["asset"] = [{"id": "drop"}]
    schema = artifact.taskSpec.get("dataModelSchema")
    assert isinstance(schema, dict)
    data = schema.get("data")
    assert isinstance(data, dict)
    weather = data.get("weather")
    assert isinstance(weather, dict)
    weather["type"] = {"type": "string", "description": "天气类型"}
    assert set(available_fields(artifact)) == {PATH, "/data/weather/type"}
    assert [icon.get("src") for icon in available_icons(artifact, {})] == [SRC]


def test_original_value_and_unit_must_be_visible_in_every_branch(artifact):
    expression = "{{ ${" + PATH + "} < 10 ? (${" + PATH + "} + '℃') : (${" + PATH + "} + '℃') }}"
    assert validate(artifact, [change("number", "content", expression)])
    with pytest.raises(ValueError, match="原数据展示"):
        validate(artifact, [change("number", "content", expression.replace("'℃'", "''", 1))])


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], [change()], [change("number", "styles.fontColor", COLOR)]])
async def test_success_keeps_original_immutable_and_checks_every_variant(artifact, payload):
    before = artifact.model_dump()
    client = SimpleNamespace(generate=AsyncMock(return_value=json.dumps(payload)))
    validator = SimpleNamespace(validate=Mock(return_value=[]))
    output = await enhance_artifact(artifact, client, validator, {})
    assert artifact.model_dump() == before
    assert client.generate.await_count == 1
    context = json.loads(client.generate.call_args.args[0][1].get("content"))
    assert context.get("finalDsl") == artifact.genui
    assert set(context.get("dataFields")) == {PATH}
    if payload:
        assert output.genui != artifact.genui
        assert validator.validate.call_count == 4  # 原卡 + 两个结果 + 最终表达式
        result = output.model_dump()
        result["genui"] = before.get("genui")
        assert result == before
    else:
        assert output is artifact


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "json", "branch", "final", "baseline"])
async def test_failure_rolls_back_whole_plan(artifact, failure):
    client = SimpleNamespace(generate=AsyncMock(return_value=json.dumps([
        change(), change("number", "styles.fontColor", COLOR)])))
    validator = SimpleNamespace(validate=Mock(return_value=[]))
    if failure == "transport":
        client.generate.side_effect = RuntimeError("test timeout")
    elif failure == "json":
        client.generate.return_value = "not-json"
    elif failure == "baseline":
        validator.validate.return_value = ["invalid baseline"]
    elif failure == "branch":
        validator.validate.side_effect = [[], [], ["unreadable non-current color"]]
    else:
        validator.validate.side_effect = [[], [], [], [], [], ["invalid expression"]]
    before = copy.deepcopy(artifact.model_dump())
    assert await enhance_artifact(artifact, client, validator, {}) is artifact
    assert artifact.model_dump() == before


@pytest.mark.asyncio
async def test_disabled_has_no_model_or_validation_calls(artifact, monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_dynamic_expression_enhancement", False)
    client = SimpleNamespace(generate=AsyncMock())
    validator = SimpleNamespace(validate=Mock())
    assert await enhance_artifact(artifact, client, validator, {}) is artifact
    client.generate.assert_not_called()
    validator.validate.assert_not_called()


def test_mapper_roundtrip_changes_all_icon_branches_only(artifact):
    expression = "{{ ${" + PATH + "} < 10 ? '" + SRC + "' : '" + OTHER + "' }}"
    records = decode(artifact.genui)
    nodes = components(records)
    icon = next(node for node in nodes if node.get("id") == "icon")
    icon["src"] = expression
    text = next(node for node in nodes if node.get("id") == "advice")
    text["content"] = expression
    source = encode(records)
    mapper = AssetUrlMapper({SRC: "https://assets.test/drop.svg"}, {SRC, OTHER})
    delivered = mapper.rewrite_standard(source)
    delivered_nodes = components(decode(delivered))
    icon = next(node for node in delivered_nodes if node.get("id") == "icon")
    assert "https://assets.test/drop.svg" in icon.get("src")
    assert OTHER in icon.get("src")
    text = next(node for node in delivered_nodes if node.get("id") == "advice")
    assert text.get("content") == expression
    restored = mapper.rewrite_standard(delivered, restore=True)
    icon = next(node for node in components(decode(restored)) if node.get("id") == "icon")
    assert SRC in icon.get("src")
    assert "https://assets.test" not in icon.get("src")


def test_branch_limit_is_bounded():
    body = "'a'"
    for _ in range(6):
        body = "(" + body + " + (${" + PATH + "} < 10 ? 'a' : 'b'))"
    root = parse_expression("{{ " + body + " }}")
    with pytest.raises(ValueError, match="分支过多"):
        result_variants(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("prop", ["styles.backgroundColor", "styles.shadow.color"])
async def test_existing_conditional_effects_and_prompt_targets(artifact, prop):
    records = decode(artifact.genui)
    text = next(node for node in components(records) if node.get("id") == "advice")
    styles = text.get("styles")
    assert isinstance(styles, dict)
    styles["backgroundColor"] = "#00000000"
    styles["shadow"] = {"radius": 4, "color": "#00000000"}
    artifact.genui = encode(records)
    client = SimpleNamespace(generate=AsyncMock(return_value=json.dumps([
        change("advice", prop, COLOR)])))
    validator = SimpleNamespace(validate=Mock(return_value=[]))
    result = await enhance_artifact(artifact, client, validator, {})
    assert result is not artifact
    payload = json.loads(client.generate.call_args.args[0][1].get("content"))
    targets = payload.get("editableTargets")
    assert isinstance(targets, list)
    assert any(target.get("property") == prop for target in targets)
    result_text = next(node for node in components(decode(result.genui))
                       if node.get("id") == "advice")
    result_styles = result_text.get("styles")
    assert isinstance(result_styles, dict)
    if prop == "styles.shadow.color":
        assert result_styles.get("shadow") == {"radius": 4, "color": COLOR}
    else:
        assert result_styles.get("backgroundColor") == COLOR


def test_missing_effect_structure_cannot_be_created(artifact):
    with pytest.raises(ValueError, match="属性对象"):
        validate(artifact, [change("advice", "styles.shadow.color", COLOR)])


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute", ["content", "styles.fontColor", "styles.shadow.color", "src"])
async def test_real_artifact_validator_accepts_enhanced_dsl(artifact, attribute, storage):
    actual_path = "/data/weather/current/temperatureC"
    artifact.genui = artifact.genui.replace(PATH, actual_path)
    records = decode(artifact.genui)
    if attribute == "styles.shadow.color":
        number = next(node for node in components(records) if node.get("id") == "number")
        styles = number.get("styles")
        assert isinstance(styles, dict)
        styles["shadow"] = {"radius": 4, "color": "#00000000"}
    for row in records:
        surface = row.get("createSurface")
        if isinstance(surface, dict):
            surface["catalogId"] = "ohos.a2ui.extended.catalog.form"
        data = row.get("updateDataModel")
        if isinstance(data, dict):
            data["value"] = {"data": {"weather": {"current": {"temperatureC": 20}}}}
    artifact.genui = encode(records)
    artifact.cardSpec = {"title": "天气", "description": "天气卡片", "suggestSize": "2x4",
                         "dataBindings": [{"capabilityId": "ViewWeather",
                         "arguments": {"prefectureName": "上海市", "forecastDays": 1},
                         "writeResultTo": "/data/weather"}]}
    artifact.taskSpec["dataModelSchema"] = {"data": {"weather": {"current": {
        "temperatureC": {"type": "number", "description": "温度", "sampleValue": 20}}}}}
    artifact.taskSpec["assetCandidates"] = [
        {"id": "asset.drop_1", "src": SRC}, {"id": "asset.alarm_fill_1", "src": OTHER}]
    artifact.effectiveCapabilities = {
        "data": ["ViewWeather"], "asset": ["asset.drop_1", "asset.alarm_fill_1"]}
    artifact.meta.capabilityRegistryVersion = "app-11.7.7.300_rom-7.0"
    validator = ArtifactValidator({})
    profile = {"id": "a2ui-form-rom6.0-v1"}
    assert validator.validate(artifact, profile) == []
    expression = EXPR
    identity = "advice"
    if attribute.startswith("styles."):
        expression = COLOR
        identity = "number"
    elif attribute == "src":
        expression = "{{ ${" + PATH + "} < 10 ? '" + SRC + "' : '" + OTHER + "' }}"
        identity = "icon"
    payload = [change(identity, attribute, expression.replace(PATH, actual_path))]
    client = SimpleNamespace(generate=AsyncMock(return_value=json.dumps(payload)))
    output = await enhance_artifact(artifact, client, validator, profile)
    assert output is not artifact
    assert validator.validate(output, profile) == []
    # 用正式存储格式往返，验证编辑来源仍读取最终 DSL，而非增强前的 Compact 源。
    saved = await ArtifactStore().save(output)
    loaded = await asyncio.to_thread(SourceArtifactRepository().load, saved.artifactUrl)
    assert decode(loaded.artifact.genui) == decode(output.genui)
    url = "https://assets.test/drop.svg"
    mapper = AssetUrlMapper({SRC: url}, {SRC, OTHER})
    delivered = loaded.artifact.model_copy(deep=True)
    delivered.genui = mapper.rewrite_standard(delivered.genui)
    source = replace(loaded, artifact=delivered)
    restored = SourceArtifactRepository.restore_asset_paths(
        source, {SRC: url}, restore_genui=True,
    )
    nodes = {node.get("id"): node for node in components(decode(restored.artifact.genui))}
    parent = nodes.get(identity)
    assert parent is not None
    for part in attribute.split(".")[:-1]:
        parent = parent.get(part)
        assert isinstance(parent, dict)
    original_expression = payload[0].get("expression")
    actual_expression = parent.get(attribute.split(".")[-1])
    assert parse_expression(actual_expression).render() == parse_expression(
        original_expression,
    ).render()


@pytest.mark.asyncio
async def test_title_and_accepted_static_facts_are_protected(artifact):
    plan = {"info_required": [{"priority": "must", "text": "出行建议"}]}
    client = SimpleNamespace(generate=AsyncMock(return_value=json.dumps([change()])))
    validator = SimpleNamespace(validate=Mock(return_value=[]))
    assert await enhance_artifact(artifact, client, validator, {}, accepted_plan=plan) is artifact
    assert validator.validate.call_count == 1


@pytest.mark.asyncio
async def test_no_authorized_fields_skips_model(artifact):
    artifact.effectiveCapabilities["data"] = []
    client = SimpleNamespace(generate=AsyncMock())
    validator = SimpleNamespace(validate=Mock(return_value=[]))
    assert await enhance_artifact(artifact, client, validator, {}) is artifact
    client.generate.assert_not_called()


@pytest.mark.parametrize("suffix", ["'保暖'", "(${/data/weather/temperature} + '℃')"])
def test_condition_reference_is_not_a_numeric_display(suffix):
    expression = "{{ ${/data/weather/current/temperatureC} < 10 ? '保暖' : " + suffix + " }}"
    # 下面直接使用真实注册表的温度字段，第二分支真正展示数值时仍要求单位。
    expression = expression.replace(
        "/data/weather/temperature", "/data/weather/current/temperatureC",
    )

    binding = {"capabilityId": "ViewWeather", "arguments": {
        "prefectureName": "上海市", "forecastDays": 1}, "writeResultTo": "/data/weather"}
    capability_dir = get_settings().data_root / "capabilities" / "app-11.7.7.300_rom-7.0"
    report = validate_card(
        dsl_text=_standard_dsl(expression),
        cardspec={"title": "天气", "description": "天气", "suggestSize": "2x4",
                  "dataBindings": [binding]},
        effective_capabilities={"data": ["ViewWeather"]},
        options=ValidationOptions(capabilities_dir=capability_dir),
    )
    assert not any(item.code == "DISPLAY_UNIT_MISSING" for item in report.diagnostics)
    if "℃" in expression:
        missing_unit = expression.replace("'℃'", "''")
        report = validate_card(
            dsl_text=_standard_dsl(missing_unit),
            cardspec={"title": "天气", "description": "天气", "suggestSize": "2x4",
                      "dataBindings": [binding]},
            effective_capabilities={"data": ["ViewWeather"]},
            options=ValidationOptions(capabilities_dir=capability_dir),
        )
        assert any(item.code == "DISPLAY_UNIT_MISSING" for item in report.diagnostics)
