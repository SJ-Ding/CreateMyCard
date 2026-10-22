"""主题完成后增强的语义约束；不改变 Compact 校验入口或布局。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .base import (
    HEX_RE,
    escape_pointer,
    estimate_text_width,
    expression_like,
    numeric,
)
from .enhancement_expression import (
    ExpressionNode,
    literal_string,
    parse_expression,
    result_variants,
    walk_nodes,
)


@dataclass(frozen=True)
class EnhancementChange:
    component_id: str
    property_name: str
    expression: str
    variants: tuple[str, ...]


def protected_text_ids(
    components: dict[str, dict], title: str | None, source: str | None, plan: dict | None,
) -> frozenset[str]:
    """从已接受计划与原组件角色保护标题、标签、动作及核心静态事实。"""
    texts = {title}
    for fact in (plan or {}).get("info_required", []):
        text = fact.get("text")
        if fact.get("priority") == "must" and isinstance(text, str):
            texts.add(text)
    protected_prefixes: list[str] = []
    if source:
        for line in source.splitlines():
            row = json.loads(line)
            if not isinstance(row, list) or len(row) < 3:
                continue
            if row[1] in {"SingleLineTitle", "DoubleLineTitle", "Badge", "CardButton"}:
                protected_prefixes.append(row[0])
    identities: set[str] = set()
    for node in components.values():
        identity = node.get("id")
        if not isinstance(identity, str) or node.get("component") != "Text":
            continue
        content = node.get("content")
        if isinstance(content, str) and content in texts:
            identities.add(identity)
        if identity.endswith("_label"):
            identities.add(identity)
        for prefix in protected_prefixes:
            if identity == prefix or identity.startswith(prefix + "_"):
                identities.add(identity)
                break
    return frozenset(identities)


def _effective_ids(items: list[Any]) -> set[str]:
    identities: set[str] = set()
    for item in items:
        identity = item
        if isinstance(item, dict):
            identity = item.get("id") or item.get("capabilityId")
        if isinstance(identity, str):
            identities.add(identity)
    return identities


def available_fields(artifact: Any) -> dict[str, dict]:
    """只暴露已投影且属于有效数据绑定的叶子，不把任意首帧字段当作动态能力。"""
    effective_ids = _effective_ids(artifact.effectiveCapabilities.get("data", []))
    roots = []
    for binding in artifact.cardSpec.get("dataBindings", []):
        if binding.get("capabilityId") not in effective_ids:
            continue
        root = binding.get("writeResultTo")
        if isinstance(root, str) and root.startswith("/"):
            roots.append(root.rstrip("/"))
    fields: dict[str, dict] = {}

    def visit(value: Any, path: str) -> None:
        if not isinstance(value, dict):
            return
        declared_type = value.get("type")
        if isinstance(declared_type, str) and declared_type in {
            "string", "integer", "number", "boolean",
        }:
            if any(path.startswith(root + "/") or path == root for root in roots):
                fields[path] = value
            return
        for key, child in value.items():
            visit(child, path + "/" + escape_pointer(key))

    visit(artifact.taskSpec.get("dataModelSchema", {}), "")
    return fields


def available_icons(artifact: Any, mapping: dict[str, str]) -> list[dict]:
    effective_ids = _effective_ids(artifact.effectiveCapabilities.get("asset", []))
    icons = []
    for asset in artifact.taskSpec.get("assetCandidates", []):
        src = asset.get("src")
        if asset.get("id") not in effective_ids or not isinstance(src, str):
            continue
        # 当前图标库为 SVG；主题图片及其它未分类素材不扩展为图标权限。
        if not src.startswith("resources/base/media/") or not src.endswith(".svg"):
            continue
        icons.append({**asset, "src": mapping.get(src, src)})
    return icons


def _expression_type(node: ExpressionNode, fields: dict[str, dict]) -> str:
    result = node.kind
    if node.kind == "reference":
        path = node.value.removeprefix("${").removesuffix("}")
        metadata = fields.get(path)
        if metadata is None:
            raise ValueError(f"动态字段未授权：{path}")
        result = metadata.get("type")
        if result == "integer":
            result = "number"
    elif node.kind == "unary":
        child_type = _expression_type(node.children[0], fields)
        result = "boolean" if node.value == "!" else "number"
        if child_type != result:
            raise ValueError("一元运算类型不匹配")
    elif node.kind == "binary":
        left, right = node.children
        left_type = _expression_type(left, fields)
        right_type = _expression_type(right, fields)
        if node.value in {"&&", "||"}:
            if left_type != "boolean" or right_type != "boolean":
                raise ValueError("逻辑运算要求布尔值")
            result = "boolean"
        elif node.value in {"==", "!=", "<", ">", "<=", ">="}:
            if left_type != right_type:
                raise ValueError("比较运算类型不匹配")
            result = "boolean"
        elif node.value == "+" and "string" in {left_type, right_type}:
            result = "string"
        else:
            if left_type != "number" or right_type != "number":
                raise ValueError("算术运算要求数值")
            # 不接受动态分母：无法证明所有运行值都不会除零。
            if node.value in {"/", "%"}:
                if right.kind != "number" or float(right.value) == 0:
                    raise ValueError("分母必须是非零数值常量")
            result = "number"
    elif node.kind == "conditional":
        condition, yes, no = node.children
        if _expression_type(condition, fields) != "boolean":
            raise ValueError("三目条件必须是布尔值")
        result = _expression_type(yes, fields)
        if _expression_type(no, fields) != result:
            raise ValueError("三目结果分支类型必须一致")
    return result


def _display_atoms(node: ExpressionNode) -> list[str]:
    """只允许原值直接展示或字符串拼接，不能拿算术变形伪装保留核心数值。"""
    atoms: list[str] = []
    if node.kind == "binary" and node.value == "+":
        for child in node.children:
            atoms.extend(_display_atoms(child))
    elif node.kind in {"string", "reference", "number"}:
        atoms.append(node.render())
    else:
        raise ValueError("文本结果只允许字符串、原始数据和拼接")
    return atoms


def _text_variant(original: Any, variant: ExpressionNode, component: dict) -> None:
    atoms = _display_atoms(variant)
    old_atoms: list[str] = []
    old_static = original
    if isinstance(original, dict):
        path = original.get("path")
        if not isinstance(path, str):
            raise ValueError("原始文本绑定不可识别")
        old_atoms = ["${" + path + "}"]
        old_static = None
    elif expression_like(original):
        baseline = parse_expression(original, require_conditional=False)
        old_atoms = _display_atoms(baseline)
        old_static = None
    if old_atoms:
        # 原展示必须作为有序连续片段存在，保留数值、单位及格式，不计条件引用。
        count = len(old_atoms)
        if not any(atoms[index:index + count] == old_atoms for index in range(len(atoms))):
            raise ValueError("结果分支没有完整保留原数据展示及单位")
    text = ""
    for node in walk_nodes(variant):
        if node.kind == "string":
            text += literal_string(node)
    if isinstance(old_static, str):
        if any(node.kind == "reference" for node in walk_nodes(variant)):
            raise ValueError("静态建议结果不能新增无法确定宽度的动态文案")
        # 静态数值、日期等事实不能替换为建议文案。
        if re.search(r"\d", old_static) and old_static not in text:
            raise ValueError("不得移除静态数值事实")
        if "\n" in text and "\n" not in old_static:
            raise ValueError("不得新增文本换行")
        font_size = numeric(component.get("styles", {}).get("fontSize"))
        if font_size is None:
            raise ValueError("无法确认原文本宽度预算")
        if estimate_text_width(text, font_size) > estimate_text_width(old_static, font_size):
            raise ValueError("新文案超过已验证原文本的宽度包络")
    elif len(atoms) != len(old_atoms):
        raise ValueError("无法确认新增动态文本的宽度预算")


def validate_changes(
    payload: Any, components: dict[str, dict], fields: dict[str, dict], icons: list[dict],
    *, protected_text_ids: frozenset[str] = frozenset(),
) -> list[EnhancementChange]:
    if not isinstance(payload, list) or len(payload) > 16:
        raise ValueError("增强方案必须是最多 16 项的 JSON 列表")
    allowed_icons = {icon.get("src") for icon in icons}
    seen: set[tuple[str, str]] = set()
    changes = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {"componentId", "property", "expression"}:
            raise ValueError("增强项字段不符合合同")
        identity = item.get("componentId")
        property_name = item.get("property")
        expression = item.get("expression")
        if not all(isinstance(value, str) for value in (identity, property_name, expression)):
            raise ValueError("增强项字段必须是字符串")
        component = components.get(identity)
        if component is None or identity.startswith("__post_theme_"):
            raise ValueError("增强目标不存在或属于主题层")
        target = (identity, property_name)
        if target in seen:
            raise ValueError("重复增强目标")
        seen.add(target)
        allowed = {"Text": {"content", "styles.fontColor"}, "Image": {"src"}}
        if property_name not in allowed.get(component.get("component"), set()):
            raise ValueError("不允许修改该组件属性")
        if property_name == "content" and identity in protected_text_ids:
            raise ValueError("标题、标签、动作及核心静态事实不允许替换")
        parent = component.get("styles", {}) if property_name.startswith("styles.") else component
        key = property_name.removeprefix("styles.")
        if key not in parent:
            raise ValueError("增强只能修改已有属性")
        if property_name == "src" and parent.get(key) not in allowed_icons:
            raise ValueError("原始素材不是授权图标")
        root = parse_expression(expression)
        if _expression_type(root, fields) != "string":
            raise ValueError("增强结果必须为字符串")
        values = []
        for variant in result_variants(root):
            value = "{{ " + variant.render() + " }}"
            if property_name == "styles.fontColor":
                value = literal_string(variant)
                if HEX_RE.fullmatch(value) is None:
                    raise ValueError("分支字体颜色非法")
            elif property_name == "src":
                value = literal_string(variant)
                if value not in allowed_icons:
                    raise ValueError("分支图标素材未授权")
            else:
                _text_variant(parent.get(key), variant, component)
                if variant.kind == "string":
                    value = literal_string(variant)
            values.append(value)
        changes.append(EnhancementChange(identity, property_name, expression, tuple(values)))
    return changes
