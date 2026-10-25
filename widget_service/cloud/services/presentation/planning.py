"""规则检查及规划说明；业务预设使用相同结构化合同。"""

from __future__ import annotations

import itertools
import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .expressions import compile_expression
from .models import PresentationError, PresentationPlan, PresentationRule

_LOGGER = logging.getLogger(__name__)
_PRESETS = Path(__file__).resolve().parents[2] / "data/presentation_presets.json"


def available_fields(task: dict, card: dict | None = None) -> dict[str, str]:
    result: dict[str, str] = {}
    roots: list[str] = []
    if card is not None:
        for binding in card.get("dataBindings", []):
            root = binding.get("writeResultTo")
            if isinstance(root, str):
                roots.append(root.rstrip("/"))

    def visit(node: Any, path: str) -> None:
        if isinstance(node, list):
            authorized = card is None or any(path.startswith(root + "/") for root in roots)
            if authorized:
                result[path] = "array"
            if node:
                visit(node[0], path + "/0")
            return
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        if isinstance(kind, str):
            authorized = card is None or any(path.startswith(root + "/") for root in roots)
            if authorized:
                result[path] = "number" if kind == "integer" else kind
            if kind not in {"object", "array"}:
                return
        children = node.get("properties", node)
        if isinstance(children, dict):
            for name, child in children.items():
                if isinstance(child, (dict, list)):
                    token = name.replace("~", "~0").replace("/", "~1")
                    visit(child, path + "/" + token)
        if kind == "array":
            visit(node.get("items"), path + "/0")

    visit(task.get("dataModelSchema", {}), "")
    return result


def explicit_request(query: str) -> bool:
    condition = re.search(r"条件|低于|高于|整百|每\s*\d+\s*天|当(?!前).+时|如果", query)
    effect = re.search(r"色|主题|样式|文案|文字|字体|联动|提醒|背景|变红|变蓝|变绿|高亮", query)
    return condition is not None and effect is not None


def _check_rule(rule: PresentationRule, fields: dict[str, str]) -> None:
    from services.post_layout_theme_registry import registered_themes

    themes = {}
    for theme in registered_themes("standard"):
        if theme.get("kind") == "solid":
            themes[theme.get("id")] = theme
    if (rule.origin == "user") != (rule.requirement == "required"):
        raise PresentationError("PRESENTATION_REQUIREMENT", "用户规则必需，模型建议可选")
    slots = set(rule.default.texts)
    if slots != set(rule.unknown.texts):
        raise PresentationError("PRESENTATION_SLOT", "未知状态必须覆盖相同文字槽")
    themes_present = rule.default.theme is not None
    if not themes_present and not slots:
        raise PresentationError("PRESENTATION_EFFECT", "规则必须有可见效果")
    if rule.scope == "local" and themes_present and not rule.local_targets:
        raise PresentationError("PRESENTATION_TARGET", "局部配色须声明语义组件目标")
    if rule.scope == "card" and rule.local_targets:
        raise PresentationError("PRESENTATION_TARGET", "整卡规则不能同时声明局部目标")
    if (rule.unknown.theme is not None) != themes_present:
        raise PresentationError("PRESENTATION_THEME", "所有状态必须有一致的主题落点")
    for outcome in [rule.default, rule.unknown, *rule.states]:
        if set(outcome.texts) != slots:
            raise PresentationError("PRESENTATION_SLOT", "所有状态必须覆盖相同文字槽")
        if (outcome.theme is not None) != themes_present:
            raise PresentationError("PRESENTATION_THEME", "所有状态必须有一致的主题落点")
        if outcome.theme is not None:
            if outcome.theme not in themes:
                raise PresentationError("PRESENTATION_THEME", "首版只允许已登记纯色主题")
        for slot, expression in outcome.texts.items():
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", slot) is None:
                raise PresentationError("PRESENTATION_SLOT", "文字槽名称非法")
            compiled = compile_expression(expression, fields)
            if compiled.result_type not in {"string", "number"}:
                raise PresentationError("PRESENTATION_TYPE", "文案结果只能为文字或数值")
    for state in rule.states:
        compiled = compile_expression(state.when, fields)
        if compiled.result_type != "boolean" or not compiled.references:
            raise PresentationError("PRESENTATION_CONDITION", "条件必须读取合法字段并返回布尔值")


def validate_plan(
    value: Any,
    task: dict,
    card: dict | None = None,
    *,
    require_requested: bool = False,
) -> PresentationPlan:
    try:
        plan = PresentationPlan.model_validate(value)
    except (ValidationError, RecursionError) as exc:
        raise PresentationError("PRESENTATION_SCHEMA", str(exc)) from exc
    fields = available_fields(task, card)
    accepted: list[PresentationRule] = []
    seen: set[str] = set()
    combinations = 1
    for rule in plan.rules:
        try:
            _check_rule(rule, fields)
            if rule.id in seen:
                raise PresentationError("PRESENTATION_DUPLICATE", "规则标识重复")
            count = combinations * (len(rule.states) + 2)
            if count > 64:
                raise PresentationError("PRESENTATION_COMPLEXITY", "联合状态超过64种")
            _check_compilation_budget(PresentationPlan(rules=[*accepted, rule]), fields)
        except PresentationError as exc:
            if rule.requirement == "required":
                raise
            _LOGGER.warning("Optional presentation rejected: %s", exc)
            continue
        combinations = count
        seen.add(rule.id)
        accepted.append(rule)
    if require_requested and explicit_request(task.get("userQuery", "")):
        if not any(rule.requirement == "required" for rule in accepted):
            raise PresentationError("PRESENTATION_REQUIRED", "明确的联动要求缺少必需展示规则")
    return PresentationPlan(rules=accepted)


def _check_compilation_budget(plan: PresentationPlan, fields: dict[str, str]) -> None:
    from .compiler import _base_theme, _theme_candidates, text_expression
    from .expressions import wrapped

    try:
        _base_theme(plan)
        source = "'#FF000000'"
        for candidate in reversed(_theme_candidates(plan, fields)):
            source = f"({candidate.condition} ? '#FFFFFFFF' : {source})"
        if _theme_candidates(plan, fields):
            wrapped(source)
        for rule in plan.rules:
            for slot in rule.default.texts:
                text_expression(rule, slot, fields)
    except PresentationError:
        raise
    except ValueError as exc:
        raise PresentationError("PRESENTATION_COMPLEXITY", str(exc)) from exc


def scenario_choices(plan: PresentationPlan):
    """条件相关属性共同使用同一分支；不按属性做笛卡尔积。"""
    choices = []
    for rule in plan.rules:
        choices.append(range(-2, len(rule.states)))
    return itertools.product(*choices)


def plan_tool_schema() -> dict:
    schema = PresentationPlan.model_json_schema()
    return schema


def planning_instruction() -> str:
    presets = json.loads(_PRESETS.read_text(encoding="utf-8"))
    return (
        "\n动态展示：presentation为结构化展示计划；没有合理规则时rules=[]。"
        "用户明确条件必须origin=user/requirement=required，主动建议用model/optional。"
        "op=ref的value只能为本轮合法字段；使用条件树，不输出表达式或代码。"
        "需要动态文字时，在合法可见文字属性放置@presentation:规则id:槽名，"
        "必须新增独立状态说明落点，不替换标题、核心数值、单位或必要事实。"
        "按全部文字分支的最长内容留空间，默认与unknown明确覆盖相同槽。"
        "整卡主题只选已登记solid主题，default与unknown使用基础主题。"
        "局部配色用scope=local并列出local_targets语义组件ID，生成DSL时逐字沿用这些ID；"
        "不允许以root作为局部目标。"
        "以下仅为声明式预设，实际字段必须替换为本轮对应的真实路径：\n"
        + json.dumps(presets, ensure_ascii=False)
    )
