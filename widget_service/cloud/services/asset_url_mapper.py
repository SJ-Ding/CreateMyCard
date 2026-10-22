# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""在交付阶段映射素材地址，模型及编辑源始终使用原始资源路径。"""

import json
from copy import deepcopy
from typing import Any

from services.compact_dsl_a2ui_converter import DataRow, parse_compact_dsl_rows

SYSTEM_INTERNAL_ASSET_SOURCES = frozenset({
    "resources/base/media/ic_fallback_light.svg",
})


class AssetUrlMapper:
    def __init__(self, mapping: dict[str, str], allowed_sources: set[str]) -> None:
        self.mapping: dict[str, str] = {}
        self._originals: dict[str, set[str]] = {}
        delivery_sources = set(allowed_sources)
        delivery_sources.update(SYSTEM_INTERNAL_ASSET_SOURCES)
        for src in delivery_sources:
            target = mapping.get(src, src)
            if not isinstance(target, str) or not target:
                raise ValueError(f"invalid asset URL mapping for {src}")
            if target != src:
                self.mapping[src] = target
                self.add_source_alias(target, src)

    def add_source_alias(self, address: str, src: str) -> None:
        """登记当前配置或历史候选中的地址，冲突仅在实际还原时拒绝。"""
        if address != src:
            self._originals.setdefault(address, set()).add(src)

    def _resolve(self, value: Any, restore: bool) -> Any:
        result: Any = value
        if isinstance(value, str) and not value.strip().startswith("{{"):
            if restore:
                sources = self._originals.get(value)
                if sources:
                    if len(sources) != 1:
                        raise ValueError("source asset URL maps to multiple resource paths")
                    result = next(iter(sources))
                elif value.startswith(("https://", "http://")):
                    raise ValueError("source asset URL cannot be restored to a declared path")
            else:
                result = self.mapping.get(value, value)
        return result

    def _rewrite_property(self, props: dict, key: str, restore: bool) -> bool:
        if key not in props:
            return False
        original = props.get(key)
        resolved = self._resolve(original, restore)
        if isinstance(original, str) and original.strip().startswith("{{"):
            resolved = self._rewrite_enhancement_expression(original, restore)
        if resolved == original:
            return False
        props[key] = resolved
        return True

    def _rewrite_enhancement_expression(self, value: str, restore: bool) -> str:
        """仅映射受限三目结果中的素材常量，不改条件引用或其它字符串字段。"""
        from services.card_validation.enhancement_expression import (
            ExpressionNode,
            literal_string,
            parse_expression,
        )

        try:
            root = parse_expression(value)
        except ValueError:
            # 历史表达式继续按原路径保留；只处理本次新增的受限语法。
            return value

        changed = False

        def rewrite(node: ExpressionNode) -> ExpressionNode:
            nonlocal changed
            if node.kind == "conditional":
                condition, yes, no = node.children
                return ExpressionNode(node.kind, node.value, (condition, rewrite(yes), rewrite(no)))
            if node.kind != "string":
                return node
            original_path = literal_string(node)
            mapped = self._resolve(original_path, restore)
            if mapped == original_path:
                return node
            changed = True
            quoted = "'" + mapped.replace("\\", "\\\\").replace("'", "\\'") + "'"
            return ExpressionNode("string", quoted)

        result = rewrite(root)
        return "{{ " + result.render() + " }}" if changed else value

    def rewrite_standard(self, dsl: str, *, restore: bool = False) -> str:
        """只处理标准 Image.src 和 styles.backgroundImage，不改数据、文本、事件。"""
        if not restore and not self.mapping:
            return dsl
        rows = [json.loads(line) for line in dsl.splitlines() if line.strip()]
        changed = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            update = row.get("updateComponents")
            if not isinstance(update, dict):
                continue
            components = update.get("components")
            if not isinstance(components, list):
                continue
            for component in components:
                if not isinstance(component, dict):
                    continue
                if component.get("component") == "Image":
                    changed |= self._rewrite_property(component, "src", restore)
                styles = component.get("styles")
                if isinstance(styles, dict):
                    changed |= self._rewrite_property(styles, "backgroundImage", restore)
        if not changed:
            return dsl
        return "\n".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            for row in rows
        )

    def restore_design_token(self, token: str) -> str:
        """只还原极简协议的素材属性，不做整段字符串替换或改写 DataModel。"""
        if "https://" not in token and "http://" not in token:
            return token
        # 按归档组件结构还原模板素材地址，不展开语义布局源文。
        rows = parse_compact_dsl_rows(
            token, expand_layouts=False, skip_compact_dsl_validation=True,
        )
        restored_rows: list[list[Any]] = []
        changed = False
        for row in rows:
            if isinstance(row, DataRow):
                restored_rows.append([row.path, row.value])
                continue
            props = deepcopy(row.props)
            for key in ("src", "icon", "backgroundImage"):
                changed |= self._rewrite_property(props, key, True)
            changed |= self._restore_nested_icons(row.component_type, props)
            styles = props.get("styles")
            if isinstance(styles, dict):
                changed |= self._rewrite_property(styles, "backgroundImage", True)
            restored_rows.append([row.component_id, row.component_type, props, list(row.children)])
        if not changed:
            return token
        return "\n".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            for row in restored_rows
        )

    def _restore_nested_icons(self, component_type: str, props: dict[str, Any]) -> bool:
        """只还原登记组件的嵌套素材位，保留正文、事件及数据中的同名字段。"""
        changed = False
        if component_type == "InfoBlock":
            visual = props.get("visual")
            if isinstance(visual, dict):
                changed |= self._rewrite_property(visual, "icon", True)
        if component_type == "NumericRatioStack":
            items = props.get("items")
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        changed |= self._rewrite_property(item, "icon", True)
        return changed

    def restore_diagnostic_values(self, value: Any) -> Any:
        """修复提示中的结构化 actual/expected 使用原始路径，避免回灌长 URL。"""
        result: Any = value
        if isinstance(value, str):
            sources = self._originals.get(value)
            if sources:
                result = sorted(sources) if len(sources) > 1 else next(iter(sources))
        elif isinstance(value, list):
            result = [self.restore_diagnostic_values(item) for item in value]
        elif isinstance(value, dict):
            result = {}
            for key, item in value.items():
                result[key] = self.restore_diagnostic_values(item)
        return result
