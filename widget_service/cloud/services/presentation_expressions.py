"""确定性展示表达式的构造和有界识别；不求值任意表达式。"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


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
    from services.template_generation.engine.a2ui_expression import normalize_tersel_expression

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


def recognize_countdown(value: Any) -> CountdownDisplay | None:
    """只识别本模块产出的规范形式，重复规范化和宽度检查共用。"""
    result: CountdownDisplay | None = None
    if not isinstance(value, str):
        return result
    guard = re.match(r"^\{\{ \('' \+ \$\{(/[^{}]+)\}\) != '' \? ", value)
    if guard is None:
        return result
    path = guard.group(1)
    for unit in ("", "天"):
        if value == countdown_number(path, unit):
            result = CountdownDisplay(path, "number", unit=unit)
            return result
    literal = r"'(?:\\.|[^'\\])*'"
    target_match = re.search(r"\('距离' \+ (" + literal + r") \+ \(", value)
    if target_match is not None:
        target = ast.literal_eval(target_match.group(1))
        if value == countdown_prefix(path, target):
            result = CountdownDisplay(path, "prefix", target=target)
    dynamic = re.search(r"\('距离' \+ \(\$\{(/[^{}]+)\} \?", value)
    if dynamic is not None:
        target = dynamic.group(1)
        if value == countdown_prefix(path, target, target_path=True):
            result = CountdownDisplay(path, "prefix", target=target, target_path=True)
    return result


def conditional_colors(value: Any) -> tuple[str, ...]:
    """有界读取最终三目颜色的两支常量，不执行条件或放宽其它表达式。"""
    if not isinstance(value, str):
        return ()
    match = re.fullmatch(
        r"\{\{ [\s\S]+ \? '(#[0-9A-Fa-f]{8})' : '(#[0-9A-Fa-f]{8})' \}\}", value
    )
    if match is None:
        return ()
    from services.template_generation.engine.a2ui_expression import normalize_wrapped_expression

    try:
        normalize_wrapped_expression(value)
    except ValueError:
        return ()  # 非法表达式仍由正式语法校验器报告。
    return (match.group(1), match.group(2))
