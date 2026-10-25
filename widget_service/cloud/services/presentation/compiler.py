"""固定组件树上的状态编译；所有属性复用一个有序状态选择。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .expressions import compile_expression, present, quote, wrapped
from .models import PresentationArchive, PresentationError, PresentationPlan, PropertyTarget
from .planning import available_fields, scenario_choices, validate_plan

_RANK = {"critical": 4, "warning": 3, "milestone": 2, "success": 2, "normal": 1}


def digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def text_expression(rule: Any, slot: str, fields: dict[str, str]) -> str:
    default = rule.default.texts.get(slot)
    unknown = rule.unknown.texts.get(slot)
    if default is None or unknown is None:
        raise PresentationError("PRESENTATION_SLOT", "文字槽没有覆盖默认及未知状态")
    refs = list(rule_references(rule, fields))
    result = compile_expression(default, fields).source
    for state in reversed(rule.states):
        condition = compile_expression(state.when, fields)
        value = state.texts.get(slot)
        if value is None:
            raise PresentationError("PRESENTATION_SLOT", "文字槽分支缺失")
        compiled = compile_expression(value, fields)
        for path in (*condition.references, *compiled.references):
            if path not in refs:
                refs.append(path)
        result = f"({condition.source} ? {compiled.source} : {result})"
    for path in compile_expression(default, fields).references:
        if path not in refs:
            refs.append(path)
    fallback = compile_expression(unknown, fields)
    if fallback.references:
        raise PresentationError("PRESENTATION_UNKNOWN", "未知文案必须是不依赖缺失数据的常量")
    return wrapped(f"({present(tuple(refs), fields)} ? {result} : {fallback.source})")


def rule_references(rule: Any, fields: dict[str, str]) -> tuple[str, ...]:
    refs: list[str] = []
    for state in rule.states:
        expressions = [state.when, *state.texts.values()]
        for expression in expressions:
            for path in compile_expression(expression, fields).references:
                if path not in refs:
                    refs.append(path)
    for expression in rule.default.texts.values():
        for path in compile_expression(expression, fields).references:
            if path not in refs:
                refs.append(path)
    return tuple(refs)


@dataclass(frozen=True)
class ThemeCandidate:
    condition: str
    theme: str
    order: tuple[int, int, int, int]
    rule_index: int
    state_index: int
    local_targets: tuple[str, ...]


def _theme_candidates(plan: PresentationPlan, fields: dict[str, str]) -> list[ThemeCandidate]:
    result: list[ThemeCandidate] = []
    for rule_index, rule in enumerate(plan.rules):
        previous: list[str] = []
        for state_index, state in enumerate(rule.states):
            condition = compile_expression(state.when, fields)
            guarded = f"({present(rule_references(rule, fields), fields)} && {condition.source})"
            effective = guarded
            if previous:
                effective = "(" + " && ".join([*previous, guarded]) + ")"
            previous.append(f"(!{guarded})")
            if state.theme is not None:
                result.append(
                    ThemeCandidate(
                        effective,
                        state.theme,
                        (
                            -_RANK.get(state.priority, 1),
                            int(rule.origin != "user"),
                            rule_index,
                            state_index,
                        ),
                        rule_index,
                        state_index,
                        tuple(rule.local_targets),
                    )
                )
    result.sort(key=lambda item: item.order)
    return result


def _documents(
    plan: PresentationPlan,
    source: str,
    task: dict,
    overrides: dict | None = None,
) -> dict[str, str]:
    from services.post_layout_theme import apply_theme, user_color_overrides
    from services.post_layout_theme_registry import registered_themes

    themes = {}
    for theme in registered_themes("standard"):
        themes[theme.get("id")] = theme
    requested: set[str] = set()
    for rule in plan.rules:
        for outcome in [rule.default, rule.unknown, *rule.states]:
            if outcome.theme is not None:
                requested.add(outcome.theme)
    result = {}
    for theme_id in sorted(requested):
        theme = themes.get(theme_id)
        if theme is None or theme.get("kind") != "solid":
            raise PresentationError("PRESENTATION_THEME", "主题不属于受控纯色配置")
        colors = overrides
        if colors is None:
            colors = user_color_overrides(task.get("userQuery", ""))
        result[theme_id] = apply_theme(source, theme, task, colors)
    return result


def _nodes(source: str) -> dict[str, dict]:
    from services.post_layout_theme import components, decode

    result = {}
    for node in components(decode(source)):
        result[node.get("id")] = node
    return result


def _read(node: dict, property_name: str) -> Any:
    current: Any = node
    for part in property_name.split("."):
        if not isinstance(current, dict) or part not in current:
            raise PresentationError("PRESENTATION_TARGET", "属性映射已失效")
        current = current.get(part)
    return current


def _write(node: dict, property_name: str, value: Any) -> None:
    if not isinstance(node, dict):
        raise PresentationError("PRESENTATION_TARGET", "属性所属节点不存在")
    parts = property_name.split(".")
    parent = node
    for part in parts[:-1]:
        child = parent.get(part)
        if not isinstance(child, dict):
            raise PresentationError("PRESENTATION_TARGET", "不能创建未登记的属性对象")
        parent = child
    if parts[-1] not in parent:
        raise PresentationError("PRESENTATION_TARGET", "不能创建未登记的属性")
    parent[parts[-1]] = value


def _base_theme(plan: PresentationPlan) -> str | None:
    found: str | None = None
    for rule in plan.rules:
        theme = rule.default.theme
        if theme is None:
            continue
        if found is not None and theme != found:
            raise PresentationError("PRESENTATION_DEFAULT", "整卡规则必须共享基础主题")
        if rule.unknown.theme != theme:
            raise PresentationError("PRESENTATION_UNKNOWN", "未知数据必须恢复基础主题")
        found = theme
    return found


def _scenario_theme(plan: PresentationPlan, choices: tuple[int, ...], base: str) -> str:
    selected = []
    for index, (rule, choice) in enumerate(zip(plan.rules, choices, strict=True)):
        if choice >= 0 and rule.scope == "card":
            state = rule.states[choice]
            if state.theme is not None:
                selected.append(
                    (
                        (-_RANK.get(state.priority, 1), int(rule.origin != "user"), index),
                        state.theme,
                    )
                )
    selected.sort()
    return selected[0][1] if selected else base


def compile_artifact(
    artifact: Any,
    plan: PresentationPlan,
    validator: Any,
    profile: dict,
    *,
    text_targets: list[PropertyTarget] | None = None,
    origins: dict[str, str] | None = None,
    overrides: dict | None = None,
) -> Any:
    from services.post_layout_theme import (
        apply_theme,
        components,
        decode,
        encode,
        user_color_overrides,
    )
    from services.post_layout_theme_registry import registered_themes

    plan = validate_plan(plan, artifact.taskSpec, artifact.cardSpec)
    source = artifact.genui
    fields = available_fields(artifact.taskSpec, artifact.cardSpec)
    base = _base_theme(plan)
    targets: list[PropertyTarget] = []
    for target in text_targets or []:
        targets.append(target.model_copy(deep=True))
    colors = (
        overrides
        if overrides is not None
        else user_color_overrides(
            artifact.taskSpec.get("userQuery", ""),
        )
    )
    documents = _documents(plan, source, artifact.taskSpec, colors)
    archive_base = base
    if base is None:
        documents["__base__"] = source
        base = "__base__"
        if "@theme:" in source:
            from services.theme_selection_policy import default_palette_family

            archive_base = "solid-" + default_palette_family(artifact.taskSpec)
            theme = next(
                item for item in registered_themes("standard") if item.get("id") == archive_base
            )
            documents[base] = apply_theme(
                source,
                theme,
                artifact.taskSpec,
                colors,
                presentation_targets=targets,
            )
    else:
        theme = None
        for item in registered_themes("standard"):
            if item.get("id") == base:
                theme = item
        if theme is None:
            raise PresentationError("PRESENTATION_THEME", "基础主题缺失")
        # apply_theme在角色被消解前提供精确的属性来源，不按色值或ID猜测。
        apply_theme(source, theme, artifact.taskSpec, {}, presentation_targets=targets)
    for target in targets:
        target.source_id = (origins or {}).get(target.component_id, target.source_id)
    local_ids = {target.source_id for target in targets if target.role != "background"}
    for rule in plan.rules:
        if rule.scope == "local" and rule.default.theme is not None:
            if "root" in rule.local_targets or not set(rule.local_targets) <= local_ids:
                raise PresentationError("PRESENTATION_TARGET", "局部目标没有已登记颜色属性")
    by_theme = {}
    for name, document in documents.items():
        by_theme[name] = _nodes(document)
    theme_candidates = _theme_candidates(plan, fields)
    records = decode(documents.get(base))
    nodes = {}
    for node in components(records):
        nodes[node.get("id")] = node
    for target in targets:
        node = nodes.get(target.component_id)
        if node is None:
            raise PresentationError("PRESENTATION_TARGET", "展示目标不存在")
        if target.role.startswith("text:"):
            continue
        original = _read(by_theme.get(base).get(target.component_id), target.property)
        result = quote(original)
        applied = False
        for candidate in reversed(theme_candidates):
            if candidate.local_targets:
                if target.role == "background" or target.source_id not in candidate.local_targets:
                    continue
            applied = True
            branch = _read(by_theme.get(candidate.theme).get(target.component_id), target.property)
            result = f"({candidate.condition} ? {quote(branch)} : {result})"
        if applied:
            _write(node, target.property, wrapped(result))
    # 先把各共享状态物化，再执行完整校验；不以空字符串掩盖求值失败。
    validated: set[str] = set()
    for choices in scenario_choices(plan):
        selected = _scenario_theme(plan, choices, base)
        branch = artifact.model_copy(deep=True)
        branch_records = decode(documents.get(selected))
        branch_nodes = {}
        for node in components(branch_records):
            branch_nodes[node.get("id")] = node
        for target in targets:
            if not target.role.startswith("text:"):
                for candidate in theme_candidates:
                    if choices[candidate.rule_index] != candidate.state_index:
                        continue
                    local = candidate.local_targets
                    if local and (target.role == "background" or target.source_id not in local):
                        continue
                    branch_value = _read(
                        by_theme.get(candidate.theme).get(target.component_id),
                        target.property,
                    )
                    _write(branch_nodes.get(target.component_id), target.property, branch_value)
                    break
            if not target.role.startswith("text:"):
                continue
            _, rule_id, slot = target.role.split(":")
            for index, rule in enumerate(plan.rules):
                if rule.id != rule_id:
                    continue
                choice = choices[index]
                outcome = rule.unknown if choice == -2 else rule.default
                if choice >= 0:
                    outcome = rule.states[choice]
                expression = outcome.texts.get(slot)
                if expression is None:
                    raise PresentationError("PRESENTATION_SLOT", "状态文案缺失")
                compiled = compile_expression(expression, fields)
                value = expression.value if expression.op == "literal" else wrapped(compiled.source)
                _write(branch_nodes.get(target.component_id), target.property, value)
        branch.genui = encode(branch_records)
        fingerprint = digest(branch.genui)
        if fingerprint in validated:
            continue
        errors = validator.validate(branch, profile)
        if errors:
            raise PresentationError("PRESENTATION_BRANCH", str(errors))
        validated.add(fingerprint)
    compiled_artifact = artifact.model_copy(deep=True)
    compiled_artifact.genui = encode(records)
    compiled_artifact.presentationPlan = PresentationArchive(
        plan=plan,
        layout=source,
        targets=targets,
        compiled_digest=digest(compiled_artifact.genui),
        base_theme=archive_base,
        color_overrides=colors,
    )
    from .runtime import verify_archive

    verify_archive(compiled_artifact)
    errors = validator.validate(compiled_artifact, profile)
    if errors:
        raise PresentationError("PRESENTATION_FINAL", str(errors))
    return compiled_artifact
