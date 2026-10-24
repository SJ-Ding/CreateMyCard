"""Fusion 主题定稿后的可选属性增强；失败原子回退，不参与布局修复。"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

from app.logger import logger
from config.config import get_settings
from services.card_validation.enhancement_rules import (
    EnhancementChange,
    available_fields,
    available_icons,
    enhancement_targets,
    property_parent,
    protected_text_ids,
    validate_changes,
)
from services.generation_trace_recorder import trace_span
from services.post_layout_theme import components, decode, encode

_INSTRUCTIONS = (
    Path(__file__).resolve().parents[1] / "data" / "protocol_profiles"
    / "dynamic-expression-enhancement" / "instructions.md"
)


def _apply(source: str, changes: list[EnhancementChange], values: tuple[str, ...]) -> str:
    records = decode(source)
    indexed = {node.get("id"): node for node in components(records)}
    for change, value in zip(changes, values, strict=True):
        node = indexed.get(change.component_id)
        if node is None:
            raise ValueError("增强目标已不存在")
        parent = property_parent(node, change.property_name)
        key = change.property_name.split(".")[-1]
        parent[key] = value
    return encode(records)


def _validate_candidate(artifact: Any, validator: Any, profile: dict) -> None:
    errors = validator.validate(artifact, profile)
    if errors:
        raise ValueError("增强产物校验失败：" + str(errors))


async def enhance_artifact(
    artifact: Any, client: Any, validator: Any, profile: dict,
    *, asset_mapping: dict[str, str] | None = None,
    source_layout: str | None = None, accepted_plan: dict | None = None,
) -> Any:
    """调用方负责 Fusion 路线门禁；关闭开关时不读取说明、不调用模型。"""
    if not get_settings().enable_dynamic_expression_enhancement:
        return artifact
    with trace_span("dynamicEnhancement", stage="dynamicEnhancement") as span:
        span.text_artifacts["enhancement_before"] = artifact.genui
        span.artifact_roles["enhancement_before"] = "input"
        try:
            # 原卡必须可交付；完整产物常规校验关闭时同样执行。
            _validate_candidate(artifact, validator, profile)
            fields = available_fields(artifact)
            icons = available_icons(artifact, asset_mapping or {})
            if not fields:
                span.outcome("success", applied=False, reason="no_authorized_fields")
                return artifact
            indexed = {node.get("id"): node for node in components(decode(artifact.genui))}
            protected = protected_text_ids(
                indexed, artifact.cardSpec.get("title"), source_layout, accepted_plan,
            )
            payload = {
                "query": artifact.taskSpec.get("userQuery"),
                "finalDsl": artifact.genui,
                "dataFields": fields,
                "iconAssets": icons,
                "protectedTextIds": sorted(protected),
                "editableTargets": enhancement_targets(indexed, protected, icons),
                "acceptedInformationPlan": accepted_plan,
                "sourceLayout": source_layout,
            }
            span.json_artifacts["enhancement_context"] = payload
            span.artifact_roles["enhancement_context"] = "input"
            messages = [
                {"role": "system", "content": _INSTRUCTIONS.read_text(encoding="utf-8")},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            raw = await client.generate(
                messages, {"id": "dynamic-expression-enhancement", "format": "raw-json"},
                phase="dynamicEnhancement",
            )
            span.text_artifacts["enhancement_plan"] = raw
            span.artifact_roles["enhancement_plan"] = "diagnostic"
            changes = validate_changes(
                json.loads(raw), indexed, fields, icons, protected_text_ids=protected,
            )
            if not changes:
                span.outcome("success", applied=False, reason="empty_plan")
                return artifact
            combinations = 1
            for change in changes:
                combinations *= len(change.variants)
            if combinations > 64:
                raise ValueError("增强方案结果组合超过 64 个，无法有界验证")
            # 检查所有返回值组合，含非首帧颜色的对比度、图标格式及文本约束。
            for values in itertools.product(*(change.variants for change in changes)):
                branch = artifact.model_copy(deep=True)
                branch.genui = _apply(artifact.genui, changes, values)
                _validate_candidate(branch, validator, profile)
            enhanced = artifact.model_copy(deep=True)
            expressions = tuple(change.expression for change in changes)
            enhanced.genui = _apply(artifact.genui, changes, expressions)
            _validate_candidate(enhanced, validator, profile)
            span.text_artifacts["enhancement_after"] = enhanced.genui
            span.artifact_roles["enhancement_after"] = "output"
            span.outcome("success", applied=True, changeCount=len(changes))
            return enhanced
        except Exception as exc:
            # 可选阶段异常只回退本阶段，不影响已完成的创建和主题结果。
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("Dynamic expression enhancement reverted: {}", reason)
            span.json_artifacts["enhancement_diagnostics"] = {"reason": reason}
            span.artifact_roles["enhancement_diagnostics"] = "diagnostic"
            span.text_artifacts["enhancement_after"] = artifact.genui
            span.artifact_roles["enhancement_after"] = "output"
            span.outcome("recovered", applied=False, reason=reason)
            return artifact
