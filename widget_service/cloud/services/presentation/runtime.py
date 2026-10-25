"""业务编排边界：必需规则失败阻断，可选规则整组回退。"""

from __future__ import annotations

from typing import Any

from services.generation_trace_recorder import trace_span

from .compiler import _write, compile_artifact
from .expressions import compile_expression, wrapped
from .models import PresentationError, PropertyTarget
from .planning import available_fields, validate_plan


def compile_with_policy(
    artifact: Any,
    raw: dict,
    validator: Any,
    profile: dict,
    *,
    targets: list[PropertyTarget] | None = None,
    origins: dict[str, str] | None = None,
    overrides: dict | None = None,
) -> Any:
    plan = validate_plan(raw, artifact.taskSpec, artifact.cardSpec)
    with trace_span("presentation.compile", stage="presentation.compile") as span:
        span.json_artifacts["presentation_plan"] = plan.model_dump(mode="json")
        try:
            result = compile_artifact(
                artifact,
                plan,
                validator,
                profile,
                text_targets=targets,
                origins=origins,
                overrides=overrides,
            )
            state_count = 1
            for rule in plan.rules:
                state_count *= len(rule.states) + 2
            span.outcome("success", ruleCount=len(plan.rules), scenarioCount=state_count)
            return result
        except ValueError as exc:
            span.json_artifacts["presentation_diagnostics"] = {"error": str(exc)}
            if any(rule.requirement == "required" for rule in plan.rules):
                span.outcome("failed")
                raise PresentationError("PRESENTATION_REQUIRED_FAILED", str(exc)) from exc
            span.outcome("recovered", reason="optional_group_reverted")
            return fallback_texts(artifact, plan, targets or [])


def fallback_texts(artifact: Any, plan: Any, targets: list[PropertyTarget]) -> Any:
    from services.post_layout_theme import components, decode, encode

    result = artifact.model_copy(deep=True)
    fields = available_fields(artifact.taskSpec, artifact.cardSpec)
    records = decode(result.genui)
    nodes = {}
    for node in components(records):
        nodes[node.get("id")] = node
    for target in targets:
        if not target.role.startswith("text:"):
            continue
        _, rule_id, slot = target.role.split(":")
        for rule in plan.rules:
            if rule.id != rule_id:
                continue
            expression = rule.default.texts.get(slot)
            if expression is None:
                raise PresentationError("PRESENTATION_SLOT", "默认文案缺失")
            compiled = compile_expression(expression, fields)
            value = expression.value if expression.op == "literal" else wrapped(compiled.source)
            node = nodes.get(target.component_id)
            if node is None:
                raise PresentationError("PRESENTATION_TARGET", "文案目标丢失")
            _write(node, target.property, value)
    result.genui = encode(records)
    result.presentationPlan = None
    return result


def verify_archive(artifact: Any) -> None:
    from .compiler import digest

    archive = artifact.presentationPlan
    if archive is not None and archive.compiled_digest != digest(artifact.genui):
        raise PresentationError("PRESENTATION_DIGEST", "展示计划与交付内容不一致")
    if archive is not None:
        validate_plan(archive.plan, artifact.taskSpec, artifact.cardSpec)
        _verify_structure(archive.layout, artifact.genui, archive.targets)
        from .compiler import _nodes, _read, text_expression

        nodes = _nodes(artifact.genui)
        fields = available_fields(artifact.taskSpec, artifact.cardSpec)
        for target in archive.targets:
            if not target.role.startswith("text:"):
                continue
            parts = target.role.split(":")
            if len(parts) != 3:
                raise PresentationError("PRESENTATION_ARCHIVE", "文字槽来源非法")
            rule = next((item for item in archive.plan.rules if item.id == parts[1]), None)
            if rule is None:
                raise PresentationError("PRESENTATION_ARCHIVE", "文字槽规则不存在")
            expected = text_expression(rule, parts[2], fields)
            node = nodes.get(target.component_id)
            if _read(node, target.property) != expected:
                raise PresentationError("PRESENTATION_ARCHIVE", "文字槽与交付表达式不一致")


def _verify_structure(layout: str, compiled: str, targets: list[PropertyTarget]) -> None:
    """存档不能通过替换布局更改动作、数据或未登记属性。"""
    import json

    from services.post_layout_theme import components, decode
    from services.theme_style_roles import COLOR_FIELDS, is_role

    documents = [decode(layout), decode(compiled)]
    seen: set[tuple[str, str]] = set()
    for target in targets:
        key = (target.component_id, target.property)
        if key in seen:
            raise PresentationError("PRESENTATION_ARCHIVE", "存档存在重复属性目标")
        seen.add(key)
        style_target = target.property.startswith("styles.") and is_role(target.role)
        style_target = style_target and target.property.removeprefix("styles.") in COLOR_FIELDS
        root_target = target.role == "background" and target.component_id == "root"
        root_target = root_target and target.property == "styles.backgroundColor"
        text_target = target.role.startswith("text:") and target.property == "content"
        if not (style_target or root_target or text_target):
            raise PresentationError("PRESENTATION_ARCHIVE", "存档包含未授权属性目标")
        for records in documents:
            node = next(
                (item for item in components(records) if item.get("id") == target.component_id),
                None,
            )
            if node is None:
                raise PresentationError("PRESENTATION_ARCHIVE", "存档组件来源不一致")
            if text_target and node.get("component") != "Text":
                raise PresentationError("PRESENTATION_ARCHIVE", "文案目标不是文字组件")
            parent = node
            parts = target.property.split(".")
            for part in parts[:-1]:
                child = parent.get(part)
                if not isinstance(child, dict):
                    raise PresentationError("PRESENTATION_ARCHIVE", "存档属性容器不一致")
                parent = child
            parent.pop(parts[-1], None)
    before = json.dumps(documents[0], sort_keys=True, ensure_ascii=False)
    after = json.dumps(documents[1], sort_keys=True, ensure_ascii=False)
    if before != after:
        raise PresentationError("PRESENTATION_ARCHIVE", "存档改变了结构、动作、数据或未登记属性")


def editing_base(artifact: Any) -> Any:
    """只解除已记录的程序属性，编辑模型不能反向识别表达式。"""
    verify_archive(artifact)
    archive = artifact.presentationPlan
    if archive is None:
        return artifact.model_copy(deep=True)
    # 使用实际交付树，避免丢失此前编辑；仅恢复有明确来源的属性。
    result = artifact.model_copy(deep=True)
    # 编译器存档的layout是本次实际编辑树，不是历史首次创建源。
    plan = archive.plan
    from .compiler import _base_theme, _documents

    base = _base_theme(plan)
    if base is not None:
        result.genui = _documents(
            plan,
            archive.layout,
            result.taskSpec,
            archive.color_overrides,
        ).get(base)
    elif archive.base_theme is not None:
        from services.post_layout_theme import apply_theme
        from services.post_layout_theme_registry import registered_themes

        theme = next(
            item for item in registered_themes("standard") if item.get("id") == archive.base_theme
        )
        result.genui = apply_theme(archive.layout, theme, result.taskSpec, archive.color_overrides)
    else:
        result.genui = archive.layout
    result = fallback_texts(result, plan, archive.targets)
    return result


def recompile_edit(original: Any, candidate: Any, plan: Any, validator: Any, profile: dict) -> Any:
    from services.post_layout_theme import components, decode, encode

    archive = original.presentationPlan
    if archive is None:
        raise PresentationError("PRESENTATION_EDIT", "历史卡片不反向推断展示规则")
    plan = validate_plan(plan, candidate.taskSpec, candidate.cardSpec)
    if not plan.rules:
        candidate.presentationPlan = None
        errors = validator.validate(candidate, profile)
        if errors:
            raise PresentationError("PRESENTATION_EDIT", str(errors))
        return candidate
    safe_base = candidate.model_copy(deep=True)
    records = decode(candidate.genui)
    nodes = {}
    for node in components(records):
        nodes[node.get("id")] = node
    fields = available_fields(candidate.taskSpec, candidate.cardSpec)
    text_targets: list[PropertyTarget] = []
    for target in archive.targets:
        node = nodes.get(target.component_id)
        if node is None:
            raise PresentationError("PRESENTATION_TARGET", "编辑未保留已登记的展示落点")
        if target.role.startswith("@theme:"):
            _write(node, target.property, target.role)
        elif target.role.startswith("text:"):
            _, rule_id, slot = target.role.split(":")
            for rule in plan.rules:
                if rule.id == rule_id:
                    from .compiler import text_expression

                    _write(node, target.property, text_expression(rule, slot, fields))
                    text_targets.append(target)
    candidate.genui = encode(records)
    from services.post_layout_theme import user_color_overrides

    colors = dict(archive.color_overrides)
    colors.update(user_color_overrides(candidate.taskSpec.get("userQuery", "")))
    origins = {}
    for target in archive.targets:
        origins[target.component_id] = target.source_id
    result = compile_with_policy(
        candidate,
        plan.model_dump(mode="json"),
        validator,
        profile,
        targets=text_targets,
        overrides=colors,
        origins=origins,
    )
    if result.presentationPlan is None:
        return safe_base
    return result
