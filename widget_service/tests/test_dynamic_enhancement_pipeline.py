"""创建链路的主题 → 增强 → 保存顺序及路线隔离。"""

import asyncio
from types import SimpleNamespace

import pytest

from config.config import get_settings
from core.errors import GenerationStatus
from custom.a2ui_model_client import A2UIModelClient
from services.compact_dsl_a2ui_converter import convert_compact_dsl_to_a2ui
from services.multi_step_generation.core.bridge import JsxA2UIBridge
from services.post_layout_theme import components, decode, encode
from services.source_artifact_repository import SourceArtifactRepository, calculate_artifact_digest
from services.template_generation import TemplateSourceGenerator
from services.validator import ArtifactValidator
from services.widget_generation_service import WidgetGenerationService
from test_asset_mapping_generation import design_source, plan_output, request
from test_asset_mapping_generation import storage as storage
from test_single_step_generation import _request as single_step_request
from test_single_step_generation import single_step_run as single_step_run


@pytest.mark.asyncio
@pytest.mark.parametrize(("backend", "kind"), [
    ("model", "compact_dsl"), ("template", "compact_dsl"), ("jsx", "compact_dsl"),
    ("model", "a2ui_form"), ("jsx", "a2ui_form"),
])
async def test_only_fusion_reaches_enhancement_after_theme_before_save(
    storage, monkeypatch, backend, kind,
):
    timeline = []
    token = design_source()
    standard = convert_compact_dsl_to_a2ui(token, size="2x4")
    settings = get_settings()
    monkeypatch.setattr(settings, "enable_dynamic_expression_enhancement", True)
    monkeypatch.setattr(settings, "enable_artifact_validation", False)

    async def generate(_client, _prompt, profile=None, *, phase="initial", **_kwargs):
        timeline.append(phase)
        if phase == "plan":
            return plan_output()
        return token if kind == "compact_dsl" else standard

    async def template(_generator, *_args, **_kwargs):
        return token

    async def jsx(_bridge, *_args):
        return SimpleNamespace(a2ui_messages=decode(standard), component_name="Test",
                               turns=1, elapsed_seconds=0.0)

    async def theme(artifact, *_args, **_kwargs):
        timeline.append("theme")
        return artifact

    async def enhance(artifact, *_args, **kwargs):
        assert timeline[-1] == "theme"
        assert kwargs.get("source_layout")
        assert kwargs.get("accepted_plan")
        timeline.append("enhancement")
        result = artifact.model_copy(deep=True)
        records = decode(result.genui)
        text = next(node for node in components(records) if node.get("component") == "Text")
        text["content"] = "增强结果"
        result.genui = encode(records)
        return result

    monkeypatch.setattr(A2UIModelClient, "generate", generate)
    monkeypatch.setattr(TemplateSourceGenerator, "__call__", template)
    monkeypatch.setattr(JsxA2UIBridge, "generate", jsx)
    monkeypatch.setattr(ArtifactValidator, "validate", lambda *_args: [])
    monkeypatch.setattr("services.post_layout_theme.theme_artifact", theme)
    monkeypatch.setattr("services.dynamic_expression_enhancement.enhance_artifact", enhance)
    monkeypatch.setattr(WidgetGenerationService, "_enable_card_template",
                        staticmethod(lambda: backend == "template"))
    monkeypatch.setattr(WidgetGenerationService, "_enable_jsx_generation",
                        staticmethod(lambda: backend == "jsx"))
    response = await getattr(WidgetGenerationService(), f"generate_widget_card_{kind}")(request())
    assert response.status == GenerationStatus.SUCCESS
    loaded = await asyncio.to_thread(SourceArtifactRepository().load, response.artifactUrl)
    assert loaded.artifact_digest == calculate_artifact_digest(loaded.artifact)
    fusion = backend == "model" and kind == "compact_dsl"
    assert ("enhancement" in timeline) is fusion
    assert ("增强结果" in loaded.artifact.genui) is fusion
    if fusion:
        assert loaded.design_token is not None
        assert "增强结果" not in loaded.design_token
    saved = (storage / response.artifactUrl.rsplit("/", 1)[-1]).read_text(encoding="utf-8")
    saved_genui = saved.split("```genui\n", 1)[1].split("\n```", 1)[0]
    assert decode(loaded.artifact.genui) == decode(saved_genui)


@pytest.mark.asyncio
async def test_single_step_fallback_never_enters_enhancement(single_step_run, monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_dynamic_expression_enhancement", True)

    async def forbidden(*_args, **_kwargs):
        pytest.fail("单步回退不能进入动态增强")

    monkeypatch.setattr("services.dynamic_expression_enhancement.enhance_artifact", forbidden)
    result = await WidgetGenerationService().generate_widget_card_compact_dsl(single_step_request())
    assert result.status == GenerationStatus.SUCCESS
    assert len(single_step_run.saved) == 1
