"""增强阶段受限表达式语法；只解析结构，绝不执行模型代码。"""

from __future__ import annotations

import ast
import itertools
import logging
import re
from dataclasses import dataclass

from .base import expression_body, is_single_wrapped_expression

_TOKEN = re.compile(
    r"\s*(?:(\$\{/[^{}\s]*\})|('(?:[^'\\]|\\.)*')|"
    r"(\d+(?:\.\d+)?)|(true\b|false\b)|(==|!=|<=|>=|&&|\|\||[()?:+*/%<>!\-]))"
)
_PRECEDENCE = {"||": 1, "&&": 2, "==": 3, "!=": 3, "<": 4, ">": 4,
               "<=": 4, ">=": 4, "+": 5, "-": 5, "*": 6, "/": 6, "%": 6}
MAX_VARIANTS = 32
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExpressionNode:
    kind: str
    value: str
    children: tuple[ExpressionNode, ...] = ()

    def render(self) -> str:
        result = self.value
        if self.kind == "unary":
            result = f"({self.value}{self.children[0].render()})"
        elif self.kind == "binary":
            left, right = self.children
            result = f"({left.render()} {self.value} {right.render()})"
        elif self.kind == "conditional":
            condition, yes, no = self.children
            result = f"({condition.render()} ? {yes.render()} : {no.render()})"
        return result


class _Parser:
    def __init__(self, body: str) -> None:
        self.tokens: list[ExpressionNode] = []
        offset = 0
        while offset < len(body):
            match = _TOKEN.match(body, offset)
            if match is None:
                raise ValueError("不支持的表达式字符或语法")
            kinds = ("reference", "string", "number", "boolean", "operator")
            for kind, value in zip(kinds, match.groups(), strict=True):
                if value is not None:
                    self.tokens.append(ExpressionNode(kind, value))
                    break
            offset = match.end()
        self.position = 0

    def peek(self) -> str | None:
        value = None
        if self.position < len(self.tokens):
            value = self.tokens[self.position].value
        return value

    def take(self, expected: str) -> None:
        if self.peek() != expected:
            raise ValueError(f"表达式缺少 {expected}")
        self.position += 1

    def parse(self, minimum: int = 0, depth: int = 0) -> ExpressionNode:
        if depth > 20 or self.position >= len(self.tokens):
            raise ValueError("表达式不完整或嵌套过深")
        node = self.tokens[self.position]
        self.position += 1
        if node.value == "(":
            node = self.parse(depth=depth + 1)
            self.take(")")
        elif node.value in {"!", "-", "+"}:
            node = ExpressionNode("unary", node.value, (self.parse(7, depth + 1),))
        elif node.kind == "operator":
            raise ValueError("表达式缺少操作数")
        while self.peek() in _PRECEDENCE:
            operator = self.peek()
            precedence = _PRECEDENCE.get(operator, 0)
            if precedence < minimum:
                break
            self.position += 1
            right = self.parse(precedence + 1, depth + 1)
            node = ExpressionNode("binary", operator, (node, right))
        if minimum == 0 and self.peek() == "?":
            self.take("?")
            yes = self.parse(depth=depth + 1)
            self.take(":")
            no = self.parse(depth=depth + 1)
            node = ExpressionNode("conditional", "?:", (node, yes, no))
        return node


def parse_expression(value: str, *, require_conditional: bool = True) -> ExpressionNode:
    if not is_single_wrapped_expression(value) or len(value) > 2048:
        raise ValueError("必须是长度不超过 2048 的完整表达式")
    parser = _Parser(expression_body(value))
    root = parser.parse()
    if parser.position != len(parser.tokens):
        raise ValueError("表达式含多余标记")
    nodes = list(walk_nodes(root))
    if require_conditional and not any(node.kind == "conditional" for node in nodes):
        raise ValueError("增强表达式必须包含三目运算")
    if not any(node.kind == "reference" for node in nodes):
        raise ValueError("增强表达式必须引用动态数据")
    return root


def walk_nodes(root: ExpressionNode):
    yield root
    for child in root.children:
        yield from walk_nodes(child)


def result_variants(root: ExpressionNode) -> list[ExpressionNode]:
    """展开所有返回分支，条件中的引用不计为展示；不根据首帧剪枝。"""
    result: list[ExpressionNode] = []
    if root.kind == "conditional":
        _, yes, no = root.children
        result = result_variants(yes) + result_variants(no)
    elif root.children:
        variants = [result_variants(child) for child in root.children]
        count = 1
        for choices in variants:
            count *= len(choices)
        if count > MAX_VARIANTS:
            raise ValueError("表达式结果分支过多")
        for combination in itertools.product(*variants):
            result.append(ExpressionNode(root.kind, root.value, combination))
    else:
        result = [root]
    if len(result) > MAX_VARIANTS:
        raise ValueError("表达式结果分支过多")
    return result


def literal_string(node: ExpressionNode) -> str:
    if node.kind != "string":
        raise ValueError("颜色和图标的每个结果分支必须是字符串常量")
    value = ast.literal_eval(node.value)
    if not isinstance(value, str):
        raise ValueError("预期字符串常量")
    return value


def display_result_expressions(value: str) -> list[str]:
    """公共展示校验只检查三目结果；其它历史语法保留原判定。"""
    expressions = [value]
    try:
        root = parse_expression(value)
        expressions = ["{{ " + variant.render() + " }}" for variant in result_variants(root)]
    except (ValueError, RecursionError) as exc:
        _LOGGER.debug("Use legacy display validation: %s", type(exc).__name__)
    return expressions
