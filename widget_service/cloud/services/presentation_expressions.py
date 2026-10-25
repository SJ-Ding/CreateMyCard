"""确定性展示表达式的构造和有界识别；不求值任意表达式。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from services.a2ui_expression import ExpressionNode
from services.expression_semantics import binding_paths, expression_literals, expression_shape


@dataclass(frozen=True)
class PresentationPolicy:
    battery_low: int
    countdown_near: int
    light_red: str
    dark_red: str


@lru_cache(maxsize=1)
def presentation_policy() -> PresentationPolicy:
    path = Path(__file__).resolve().parents[1] / "data/dynamic_presentation.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    for key in ("version", "batteryLowExclusive", "countdownNearInclusive"):
        value = raw.get(key)
        if type(value) is not int or value < 0:
            raise ValueError(f"Invalid dynamic presentation setting: {key}")
    if raw.get("version") != 1 or raw.get("batteryLowExclusive") > 100:
        raise ValueError("Unsupported dynamic presentation policy")
    for key in ("lightAlertColor", "darkAlertColor"):
        if not is_color(raw.get(key)):
            raise ValueError(f"Invalid dynamic presentation color: {key}")
    return PresentationPolicy(
        battery_low=raw.get("batteryLowExclusive"),
        countdown_near=raw.get("countdownNearInclusive"),
        light_red=raw.get("lightAlertColor"),
        dark_red=raw.get("darkAlertColor"),
    )


def is_color(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{8}", value) is not None


def quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return "'" + escaped + "'"


def reference(path: str) -> str:
    return "${" + path + "}"


def wrapped(body: str) -> str:
    # 复用正式受限语法解析器，不执行表达式。
    from services.a2ui_expression import normalize_tersel_expression

    return normalize_tersel_expression(body).value


def present(path: str) -> str:
    # 字符串连接仅检查空态；0 不会与空字符串混淆，也不转换数据源的实际类型。
    return "('' + " + reference(path) + ") != ''"


def countdown_number(path: str, unit: str = "") -> str:
    ref = reference(path)
    body = f"({ref} >= 0 ? {ref} : -{ref})"
    if unit:
        body += " + " + quote(unit)
    return wrapped(f"{present(path)} ? {body} : ''")


def countdown_prefix(path: str, target: str, *, target_path: bool = False) -> str:
    """事件身份与状态并列，不把任意事件名称强行拼成一个句子。"""
    ref = reference(path)
    name = quote(target)
    if target_path:
        target_ref = reference(target)
        name = f"({target_ref} ? {target_ref} : '目标日')"
    state = f"({ref} == 0 ? ' · 今天' : ({ref} > 0 ? ' · 还剩' : ' · 已过'))"
    return wrapped(f"{present(path)} ? ({name} + {state}) : ''")


def countdown_readout(path: str) -> str:
    """文本读数自带完整状态；事件名称可留在独立静态标签中。"""
    ref = reference(path)
    number = f"({ref} >= 0 ? {ref} : -{ref})"
    state = f"({ref} > 0 ? '还剩' : '已过')"
    return wrapped(
        f"{present(path)} ? ({ref} == 0 ? '今天' : ({state} + {number} + '天')) : ''"
    )


def _legacy_countdown_prefix(path: str, target: str, *, target_path: bool = False) -> str:
    """仅识别已安装卡片及历史源，不再在融合生成中产出。"""
    ref = reference(path)
    name = quote(target)
    if target_path:
        target_ref = reference(target)
        name = f"({target_ref} ? {target_ref} : '目标日')"
    body = f"('距离' + {name} + ({ref} >= 0 ? '还剩' : '已经'))"
    return wrapped(f"{present(path)} ? {body} : ''")


@dataclass(frozen=True)
class CountdownDisplay:
    path: str
    kind: str
    unit: str = ""
    target: str = ""
    target_path: bool = False
    status: bool = False


def _target_candidates(value: Any) -> tuple[str, ...]:
    targets = []
    for literal in expression_literals(value):
        for candidate in (literal, literal.removeprefix("距离")):
            if candidate and candidate not in targets:
                targets.append(candidate)
    return tuple(targets)


def recognize_countdown(value: Any) -> CountdownDisplay | None:
    """按同一受限语法树识别程序形式，不依赖空白、括号或相邻常量连接。"""
    return _recognize_countdown(value) if isinstance(value, str) else None


@lru_cache(maxsize=512)
def _recognize_countdown(value: str) -> CountdownDisplay | None:
    node = expression_shape(value)
    if node is None or node.kind != "conditional":
        return None
    guard = node.children[0]
    if guard.kind != "binary" or guard.value != "!=":
        return None
    left, right = guard.children
    if left.kind != "binary" or left.value != "+":
        return None
    empty, binding = left.children
    valid_empty = empty == right == ExpressionNode("literal", "")
    if not valid_empty or binding.kind != "binding":
        return None
    path = binding.value
    if node == expression_shape(countdown_readout(path)):
        return CountdownDisplay(path, "number", unit="天", status=True)
    for unit in ("", "天"):
        if node == expression_shape(countdown_number(path, unit)):
            return CountdownDisplay(path, "number", unit=unit)
    for target in _target_candidates(value):
        for builder in (countdown_prefix, _legacy_countdown_prefix):
            if node == expression_shape(builder(path, target)):
                return CountdownDisplay(path, "prefix", target=target)
    for target in binding_paths(node):
        if target == path:
            continue
        for builder in (countdown_prefix, _legacy_countdown_prefix):
            if node == expression_shape(builder(path, target, target_path=True)):
                return CountdownDisplay(path, "prefix", target=target, target_path=True)
    return None


def conditional_colors(value: Any) -> tuple[str, ...]:
    """读取语法树顶层三目的常量颜色分支，不执行条件。"""
    node = expression_shape(value)
    if node is None or node.kind != "conditional":
        return ()
    positive, negative = node.children[1:]
    if positive.kind != "literal" or negative.kind != "literal":
        return ()
    if not is_color(positive.value) or not is_color(negative.value):
        return ()
    return positive.value, negative.value


def _concat_parts(node: ExpressionNode) -> tuple[ExpressionNode, ...]:
    if node.kind != "binary" or node.value != "+":
        return (node,)
    return _concat_parts(node.children[0]) + _concat_parts(node.children[1])


def countdown_input_readout(value: Any, path: str) -> CountdownDisplay | None:
    """只接受单一天数及字面量名称／单位，不把任意算术转换成原始天数。"""
    existing = recognize_countdown(value)
    if existing is not None:
        return existing if existing.path == path and existing.kind == "number" else None
    if value == {"path": path}:
        return CountdownDisplay(path, "number")
    node = expression_shape(value)
    if node is None:
        return None
    ref = reference(path)
    numeric = wrapped(f"{ref} >= 0 ? {ref} : -{ref}")
    if node == expression_shape(numeric):
        return CountdownDisplay(path, "number")
    before = []
    after = []
    found = False
    for part in _concat_parts(node):
        if part.kind == "binding" and part.value == path and not found:
            found = True
        elif part.kind == "literal":
            (after if found else before).append(part.value)
        else:
            return None
    unit = "".join(after)
    if not found or unit not in {"", "天"}:
        return None
    return CountdownDisplay(path, "number", unit=unit, target="".join(before).strip())


def countdown_input_target(value: Any, path: str) -> str | None:
    """兼容旧状态文案的结构等价形式，不解析任意中文表达或模型条件。"""
    known = recognize_countdown(value)
    if known is not None and known.kind == "prefix" and not known.target_path:
        return known.target if known.path == path else None
    node = expression_shape(value)
    if node is None or node.kind != "conditional":
        return None
    inner = node.children[1]
    if inner.kind == "binary" and inner.value == "+":
        left, right = inner.children
        if right == ExpressionNode("literal", "天"):
            node = ExpressionNode(node.kind, node.value, (node.children[0], left, node.children[2]))
    for target in _target_candidates(value):
        if node == expression_shape(_legacy_countdown_prefix(path, target)):
            return target
    guard = expression_shape(wrapped(f"{present(path)} ? 'value' : ''"))
    state = expression_shape(wrapped(f"{reference(path)} >= 0 ? '还剩' : '已经'"))
    if guard is None or node.children[0] != guard.children[0]:
        return None
    if node.children[2] != ExpressionNode("literal", ""):
        return None
    parts = _concat_parts(node.children[1])
    if parts and parts[-1] == ExpressionNode("literal", "天"):
        parts = parts[:-1]
    if len(parts) == 2 and parts[0].kind == "literal" and parts[1] == state:
        return parts[0].value.removeprefix("距离")
    return None
