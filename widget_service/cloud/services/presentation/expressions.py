"""结构化条件的类型检查与现有 Form 表达式编译。"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import Decimal

from services.a2ui_expression import (
    ExpressionNode,
    normalize_wrapped_expression,
    parse_expression_tree,
)

from .models import Expression, PresentationError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TypedExpression:
    source: str
    result_type: str
    references: tuple[str, ...]


def quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return "'" + escaped + "'"


def compile_expression(
    expression: Expression,
    fields: dict[str, str],
    depth: int = 0,
) -> TypedExpression:
    if depth > 20:
        raise PresentationError("PRESENTATION_COMPLEXITY", "条件树超过20层")
    op = expression.op
    value = expression.value
    refs: list[str] = []
    if op in {"ref", "literal"}:
        if expression.args or value is None:
            raise PresentationError("PRESENTATION_EXPRESSION", "叶节点需要值且不能包含参数")
        result_type = ""
        source = ""
        if op == "ref":
            if not isinstance(value, str) or value not in fields:
                raise PresentationError("PRESENTATION_FIELD", "条件引用未授权字段")
            result_type = fields.get(value)
            source = "${" + value + "}"
            refs.append(value)
        elif isinstance(value, bool):
            source, result_type = ("true" if value else "false"), "boolean"
        elif isinstance(value, str):
            if value.startswith("@presentation:"):
                raise PresentationError("PRESENTATION_SLOT", "状态文案不能包含另一个槽引用")
            source, result_type = quote(value), "string"
        else:
            try:
                finite = math.isfinite(value)
            except OverflowError as exc:
                raise PresentationError("PRESENTATION_TYPE", "数值超出端侧有限数值范围") from exc
            if not finite:
                raise PresentationError("PRESENTATION_TYPE", "只允许有限数值")
            source = format(Decimal(str(value)), "f")
            result_type = "number"
        return TypedExpression(source, result_type, tuple(refs))
    if value is not None:
        raise PresentationError("PRESENTATION_EXPRESSION", "运算节点不能同时包含值")
    arity = 1 if op in {"!", "size"} else 2
    if len(expression.args) != arity:
        raise PresentationError("PRESENTATION_EXPRESSION", "运算参数数量不匹配")
    children = []
    for argument in expression.args:
        child = compile_expression(argument, fields, depth + 1)
        children.append(child)
        for path in child.references:
            if path not in refs:
                refs.append(path)
    left = children[0]
    result_type = "number"
    if arity == 1:
        expected = {"boolean"} if op == "!" else {"string", "array", "object"}
        if left.result_type not in expected:
            raise PresentationError("PRESENTATION_TYPE", "一元运算类型不匹配")
        result_type = "boolean" if op == "!" else "number"
        source = f"(!{left.source})" if op == "!" else f"size({left.source})"
    else:
        right = children[1]
        types = (left.result_type, right.result_type)
        if op in {"&&", "||"}:
            if types != ("boolean", "boolean"):
                raise PresentationError("PRESENTATION_TYPE", "逻辑运算要求布尔值")
            result_type = "boolean"
        elif op in {"==", "!=", "<", ">", "<=", ">="}:
            if types[0] != types[1] or types[0] in {"array", "object"}:
                raise PresentationError("PRESENTATION_TYPE", "比较运算类型不匹配")
            if op not in {"==", "!="} and types[0] != "number":
                raise PresentationError("PRESENTATION_TYPE", "顺序比较仅允许数值")
            result_type = "boolean"
        elif op == "+" and "string" in types:
            if any(kind in {"array", "object"} for kind in types):
                raise PresentationError("PRESENTATION_TYPE", "不能隐式拼接复合值")
            result_type = "string"
        elif types != ("number", "number"):
            raise PresentationError("PRESENTATION_TYPE", "算术运算要求数值")
        if op in {"/", "%"}:
            denominator = expression.args[1]
            if denominator.op != "literal" or isinstance(denominator.value, bool):
                raise PresentationError("PRESENTATION_DIVISOR", "除数必须为非零数值常量")
            if not isinstance(denominator.value, (int, float)) or denominator.value == 0:
                raise PresentationError("PRESENTATION_DIVISOR", "除数必须为非零数值常量")
            if op == "%" and not isinstance(denominator.value, int):
                raise PresentationError("PRESENTATION_DIVISOR", "取模除数必须为整数")
        source = f"({left.source} {op} {right.source})"
    return TypedExpression(source, result_type, tuple(refs))


def wrapped(source: str) -> str:
    value = "{{ " + source + " }}"
    normalize_wrapped_expression(value)
    return value


def present(paths: tuple[str, ...], fields: dict[str, str] | None = None) -> str:
    checks = []
    for path in paths:
        if (fields or {}).get(path) in {"array", "object"}:
            checks.append("(${" + path + "} != '')")
        else:
            checks.append("(('' + ${" + path + "}) != '')")
    return " && ".join(checks) or "true"


def result_nodes(node: ExpressionNode) -> tuple[ExpressionNode, ...]:
    """仅返回实际展示分支，条件依赖不得冒充可见事实。"""
    if node.kind != "conditional":
        return (node,)
    return result_nodes(node.children[1]) + result_nodes(node.children[2])


def display_references(value: str) -> tuple[str, ...] | None:
    from services.expression_semantics import expression_display_paths

    return expression_display_paths(value)


def render_node(node: ExpressionNode) -> str:
    result = node.value
    if node.kind == "binding":
        result = "${" + node.value + "}"
    elif node.kind == "literal":
        result = quote(node.value)
    elif node.kind == "unary":
        result = f"({node.value}{render_node(node.children[0])})"
    elif node.kind == "binary":
        result = f"({render_node(node.children[0])} {node.value} {render_node(node.children[1])})"
    elif node.kind == "call":
        result = f"{node.value}({render_node(node.children[0])})"
    return result


def display_variants(value: str) -> tuple[str, ...]:
    """有界展开可见结果，保留历史解析器不认识的语法交原校验处理。"""
    import itertools

    if not value.strip().startswith("{{"):
        return (value,)

    def expand(node: ExpressionNode) -> list[ExpressionNode]:
        if node.kind == "conditional":
            result = expand(node.children[1]) + expand(node.children[2])
        elif node.children:
            variants = []
            for child in node.children:
                variants.append(expand(child))
            result = []
            for children in itertools.product(*variants):
                result.append(ExpressionNode(node.kind, node.value, children))
                if len(result) > 64:
                    raise PresentationError("PRESENTATION_COMPLEXITY", "展示分支超过64种")
        else:
            result = [node]
        if len(result) > 64:
            raise PresentationError("PRESENTATION_COMPLEXITY", "展示分支超过64种")
        return result

    try:
        root = parse_expression_tree(value)
    except (ValueError, RecursionError) as exc:
        _LOGGER.debug("Use legacy display analysis: %s: %s", type(exc).__name__, exc)
        return (value,)
    return tuple("{{ " + render_node(node) + " }}" for node in expand(root))
