"""复用正式受限解析器检查表达式结构和引用角色；不执行任何表达式。"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

from services.a2ui_expression import (
    ExpressionNode,
    parse_expression_tree,
)

_BOOLEAN_OPERATORS = frozenset({">", ">=", "<", "<=", "==", "!=", "&&", "||", "!"})


@lru_cache(maxsize=512)
def _tree(value: str) -> ExpressionNode | None:
    result: ExpressionNode | None = None
    try:
        result = parse_expression_tree(value)
    except (ValueError, RecursionError):
        # 正式语法校验器负责报告错误；语义分析不能把非法表达式当作可识别结果。
        result = None
    return result


def expression_tree(value: Any) -> ExpressionNode | None:
    result: ExpressionNode | None = None
    if isinstance(value, str) and value.strip().startswith("{{"):
        result = _tree(value)
    return result


def _constant_shape(node: ExpressionNode) -> ExpressionNode:
    children = tuple(_constant_shape(child) for child in node.children)
    if node.kind == "binary" and node.value == "+":
        left, right = children
        if left.kind == right.kind == "literal":
            return ExpressionNode("literal", left.value + right.value)
    return ExpressionNode(node.kind, node.value, children)


@lru_cache(maxsize=512)
def _shape(value: str) -> ExpressionNode | None:
    node = _tree(value)
    return _constant_shape(node) if node is not None else None


def expression_shape(value: Any) -> ExpressionNode | None:
    result: ExpressionNode | None = None
    if isinstance(value, str) and value.strip().startswith("{{"):
        result = _shape(value)
    return result


def binding_paths(node: ExpressionNode) -> tuple[str, ...]:
    result = []
    pending = [node]
    while pending:
        current = pending.pop()
        if current.kind == "binding" and current.value not in result:
            result.append(current.value)
        pending.extend(reversed(current.children))
    return tuple(result)


def _display_counts(node: ExpressionNode) -> dict[str, int]:
    if node.kind == "binding":
        return {node.value: 1}
    if node.kind == "conditional":
        positive = _display_counts(node.children[1])
        negative = _display_counts(node.children[2])
        for path, count in negative.items():
            positive[path] = max(positive.get(path, 0), count)
        return positive
    boolean_operator = node.value in _BOOLEAN_OPERATORS
    if node.kind in {"binary", "unary"} and boolean_operator:
        return {}
    result: dict[str, int] = {}
    for child in node.children:
        for path, count in _display_counts(child).items():
            result[path] = result.get(path, 0) + count
    return result


def expression_display_paths(value: Any) -> tuple[str, ...] | None:
    node = expression_tree(value)
    if node is None:
        return None
    paths = []
    for path, count in _display_counts(node).items():
        paths.extend([path] * count)
    return tuple(paths)


def expression_literals(value: Any) -> tuple[str, ...]:
    node = expression_shape(value)
    if node is None:
        return ()
    result = []
    pending = [node]
    while pending:
        current = pending.pop()
        if current.kind == "call":
            continue
        if current.kind in {"binary", "unary"} and current.value in _BOOLEAN_OPERATORS:
            continue
        if current.kind == "literal":
            result.append(current.value)
        children = current.children[1:] if current.kind == "conditional" else current.children
        pending.extend(reversed(children))
    return tuple(result)


def expression_literal_count(value: str, matches: Callable[[str], bool]) -> int | None:
    node = expression_tree(value)
    if node is None:
        return None

    def count(current: ExpressionNode) -> int:
        if current.kind == "call":
            return 0
        if current.kind == "literal":
            return int(matches(current.value))
        if current.kind == "conditional":
            return max(count(current.children[1]), count(current.children[2]))
        if current.kind in {"binary", "unary"} and current.value in _BOOLEAN_OPERATORS:
            return 0
        return sum(count(child) for child in current.children)

    return count(node)
