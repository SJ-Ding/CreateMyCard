"""融合产物的确定性动态展示：布局前规范文案，主题后编译颜色。"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from services.countdown_contract import clean_countdown_name, countdown_identity
from services.presentation_expressions import (
    conditional_colors,
    countdown_input_readout,
    countdown_input_target,
    countdown_number,
    countdown_prefix,
    countdown_readout,
    is_color,
    present,
    presentation_policy,
    quote,
    recognize_countdown,
    reference,
    wrapped,
)

_LOGGER = logging.getLogger(__name__)
_NUMERIC = {"integer", "number"}
_FIELDS = {
    "GetPhoneBatteryInfo": (("batterySOC", "batterySOCText"),),
    "GetEarphoneInfo": (
        ("batteryLevel", "batteryLevel"),
        ("leftBatteryLevel", "leftBatteryLevel"),
        ("rightBatteryLevel", "rightBatteryLevel"),
    ),
    "GetCountdownDays": (("countdownDays", "countdownDays"),),
}
_READOUTS = {
    "Text": "content",
    "EmphasizedData": "value",
    "DataDisplay": "value",
    "EmphasisText": "mainText",
    "InfoBlock": "primaryText",
}


def uniform_text_request(query: str) -> bool:
    """条件中的颜色不是统一配色；明确的全局要求仍优先于默认状态色。"""
    global_text = re.search(
        r"(?:所有|全部|统一|整张卡片|整卡).{0,12}(?:文字|字体|字色)", query,
    )
    if global_text is not None:
        return True
    return re.search(
        r"小于|低于|大于|高于|超过|以内|之前|之后|如果|当.*时|不足|少于|临近|到期|过期", query,
    ) is None


@dataclass(frozen=True)
class PresentationRule:
    path: str
    display_paths: tuple[str, ...]
    kind: str
    connected_path: str | None = None


def _schema(task: dict, path: str) -> dict:
    from services.card_validation.compact_validation.schema import _schema_node_at_path

    node = _schema_node_at_path(task.get("dataModelSchema", {}), path)
    return node if isinstance(node, dict) else {}


def presentation_rules(card: dict, task: dict) -> tuple[PresentationRule, ...]:
    rules = []
    for binding in card.get("dataBindings") or []:
        capability = binding.get("capabilityId")
        root = binding.get("writeResultTo")
        if capability not in _FIELDS or not isinstance(root, str):
            continue
        for numeric, display in _FIELDS.get(capability, ()):
            path = root.rstrip("/") + "/" + numeric
            display_path = root.rstrip("/") + "/" + display
            if _schema(task, path).get("type") not in _NUMERIC:
                _LOGGER.info(
                    "dynamic_presentation_skipped reason=numeric_dependency_missing path=%s", path,
                )
                continue
            kind = "countdown" if capability == "GetCountdownDays" else "battery"
            connected = None
            if capability == "GetEarphoneInfo":
                connected = root.rstrip("/") + "/isConnected"
                if _schema(task, connected).get("type") != "boolean":
                    _LOGGER.info(
                        "dynamic_presentation_skipped reason=connection_dependency_missing"
                    )
                    continue
            paths = [path]
            if display_path != path and _schema(task, display_path).get("type") == "string":
                paths.append(display_path)
            rules.append(PresentationRule(path, tuple(paths), kind, connected))
    return tuple(rules)


def add_presentation_dependencies(task: dict, card: dict, capabilities: list) -> dict:
    """只补同一已授权绑定的正式字段，不改变请求投影或要求模型展示条件依赖。"""
    from core.json_pointer import parse_json_pointer
    from services.task_spec_builder import TaskSpecBuilder

    result = copy.deepcopy(task)
    by_id = {}
    for capability in capabilities:
        value = capability
        if hasattr(capability, "model_dump"):
            value = capability.model_dump(mode="json")
        by_id[value.get("id")] = value
    for binding in card.get("dataBindings") or []:
        capability_id = binding.get("capabilityId")
        capability = by_id.get(capability_id)
        root = binding.get("writeResultTo")
        if capability_id not in _FIELDS or not isinstance(capability, dict):
            continue
        if not isinstance(root, str):
            continue
        root_parts = parse_json_pointer(root)
        if root_parts is None:
            continue
        fields = capability.get("outputSchema", {}).get("properties", {})
        dependencies = []
        for numeric, display in _FIELDS.get(capability_id, ()):
            if _schema(task, root + "/" + display) or _schema(task, root + "/" + numeric):
                dependencies.append(numeric)
        if dependencies and capability_id == "GetEarphoneInfo":
            dependencies.append("isConnected")
        for field in dependencies:
            path = root + "/" + field
            leaf = fields.get(field)
            if _schema(result, path) or not isinstance(leaf, dict):
                continue
            expected = {"boolean"} if field == "isConnected" else _NUMERIC
            if leaf.get("type") not in expected:
                continue
            metadata = {}
            for key in ("type", "description", "sampleValue"):
                if key in leaf:
                    metadata[key] = copy.deepcopy(leaf.get(key))
            schema = result.get("dataModelSchema")
            if not isinstance(schema, dict):
                raise ValueError("Dynamic presentation requires dataModelSchema")
            TaskSpecBuilder()._set_by_parts(schema, (*root_parts, field), metadata)
    return result


def presentation_instruction(card: dict, task: dict) -> str:
    lines = []
    for rule in presentation_rules(card, task):
        if rule.kind == "countdown":
            lines.append(
                f"倒计时{rule.path}保留具体事件身份与用户明确要求的信息。名称可放同分区标题、"
                "DataDisplay.label或同对象InfoBlock.secondaryText，不借用其它对象标题。"
                "不要自行拼动态句子；程序生成还剩/已过/今天及非负天数。"
                f"本轮唯一目标备用名称：{countdown_identity(card) or '无'}。"
                "明确分组枚举逐项保留，不能以列表容量为由只取前三项。"
                f"数字将由程序规范为{countdown_number(rule.path)}，"
                "文本读数可使用EmphasisText或InfoBlock，由程序生成完整状态读数；"
                "独立数字使用EmphasizedData与单位天，同对象说明由程序生成。"
                "布局必须给全部状态分支留足空间。"
            )
        else:
            lines.append(
                f"{rule.path}及连接状态仅作为条件依赖，未要求时不要增加可见事实。"
                "每个对象的电量使用独立读数槽，不与其它对象或其它指标拼成一个文字值。"
            )
    return "\n# 确定性动态展示合同\n" + "\n".join(lines) if lines else ""


def _references(value: Any) -> tuple[str, ...]:
    from services.card_validation.base import expression_references

    paths = []
    if isinstance(value, dict) and set(value) == {"path"}:
        path = value.get("path")
        if isinstance(path, str):
            paths.append(path)
    elif isinstance(value, str):
        for path in expression_references(value):
            if path not in paths:
                paths.append(path)
    return tuple(paths)


def _target(value: Any, day_path: str) -> tuple[str, bool] | None:
    result: tuple[str, bool] | None = None
    existing = recognize_countdown(value)
    if existing is not None and existing.kind == "prefix":
        if existing.path == day_path:
            result = existing.target or "目标日", existing.target_path
    input_target = countdown_input_target(value, day_path) if existing is None else None
    if input_target is not None:
        result = clean_countdown_name(input_target) or "目标日", False
    paths = _references(value)
    if len(paths) == 1 and day_path not in paths:
        path = paths[0]
        simple = value == {"path": path} or value == wrapped(reference(path))
        if simple:
            result = path, True
    if isinstance(value, str) and not paths:
        text = clean_countdown_name(value)
        if not text or text in {"倒计时", "倒数日", "剩余天数", "天"}:
            result = "目标日", False
        elif re.search(r"还剩|已经|剩余|\{\{", text) is None:
            result = text, False
    return result


def _group_ids(component_id: str, components: dict, parents: dict) -> set[str]:
    current = component_id
    seen = set()
    while current not in seen:
        seen.add(current)
        row = components.get(current)
        if row is None:
            break
        if row.component_type == "Region" or current not in parents:
            break
        current = parents.get(current)
    pending = [current]
    result = set()
    while pending:
        key = pending.pop()
        if key in result:
            continue
        result.add(key)
        row = components.get(key)
        if row is not None:
            pending.extend(row.children)
    return result


def _normalize_countdown_display_layout(
    rows: list, rules: list[PresentationRule], fallback_names: dict[str, str],
) -> None:
    """将独立数字核心换为已有标题数字布局，让文案和单位都有正式预算。"""
    from services.compact_high_level_components import ComponentRow

    components = {}
    for row in rows:
        if isinstance(row, ComponentRow):
            components[row.component_id] = row
    root = components.get("root")
    if root is None or root.props.get("layout") != "S-center":
        return
    if len(root.children) != 1:
        return
    display = components.get(root.children[0])
    if display is None or display.component_type != "DataDisplay":
        return
    supporting = display.props.get("supportingText")
    if supporting not in (None, "", "天", "状态", "倒计时"):
        return
    for rule in rules:
        if _references(display.props.get("value")) != (rule.path,):
            continue
        name = _target(display.props.get("label", ""), rule.path)
        fallback_name = fallback_names.get(rule.path)
        if name == ("目标日", False) and fallback_name:
            name = fallback_name, False
        if name is None or name[1]:
            return
        title_id = display.component_id + "_presentation_title"
        if title_id in components:
            raise ValueError("Countdown presentation title ID already exists")
        title_props = {"title": name[0]}
        if display.props.get("icon"):
            title_props["icon"] = display.props.get("icon")
        title = ComponentRow(title_id, "SingleLineTitle", title_props, ())
        value = ComponentRow(display.component_id, "EmphasizedData", {
            "value": display.props.get("value"), "unit": "天",
        }, ())
        root.props["layout"] = "S-title-content"
        replacement = ComponentRow(root.component_id, root.component_type, root.props,
                                   (title_id, display.component_id))
        rows[rows.index(root)] = replacement
        display_index = rows.index(display)
        rows[display_index] = value
        rows.insert(display_index, title)
        return


def normalize_presentation_source(
    source: str, card: dict, task: dict, plan: dict | None = None,
) -> str:
    """在语义布局展开前改写明确的读数和同分区标题；歧义交给原校验修复链路。"""
    from services.card_validation.base import read_pointer
    from services.card_validation.compact_validation.semantic.display import (
        require_countdown_presentation,
    )
    from services.compact_dsl_a2ui_converter import build_compact_data_model, parse_compact_dsl_rows
    from services.compact_high_level_components import ComponentRow, DataRow

    all_rules = presentation_rules(card, task)
    rules = []
    for rule in all_rules:
        if rule.kind == "countdown":
            rules.append(rule)
    if not all_rules:
        return source
    rows = copy.deepcopy(list(parse_compact_dsl_rows(source, expand_layouts=False)))
    fallback_names = {}
    for rule in rules:
        fallback_names[rule.path] = countdown_identity(card, plan, path=rule.path)
    _normalize_countdown_display_layout(rows, rules, fallback_names)
    data_rows = []
    for row in rows:
        if isinstance(row, DataRow):
            data_rows.append(row)
    data = build_compact_data_model(data_rows)
    seeded: set[str] = set()
    for rule in all_rules:
        for path in (rule.path, rule.connected_path):
            if path is None or path in seeded:
                continue
            exists, _value = read_pointer(data, path)
            if exists:
                continue
            metadata = _schema(task, path)
            if "sampleValue" in metadata:
                rows.append(DataRow(path, copy.deepcopy(metadata.get("sampleValue"))))
                seeded.add(path)
    components = {}
    parents = {}
    title_owners = {}
    for row in rows:
        if isinstance(row, ComponentRow):
            components[row.component_id] = row
            for child in row.children:
                parents[child] = row.component_id
    for rule in rules:
        fallback_name = fallback_names.get(rule.path)
        readouts = []
        for row in components.values():
            prop = _READOUTS.get(row.component_type)
            value = row.props.get(prop)
            known = recognize_countdown(value)
            if known is not None and known.kind == "prefix":
                continue
            paths = _references(value)
            if paths == (rule.path,):
                require_countdown_presentation(
                    row.component_id,
                    mixed_readout=countdown_input_readout(value, rule.path) is None,
                )
                readouts.append((row, prop))
            elif rule.path in paths:
                require_countdown_presentation(row.component_id, mixed_readout=True)
        if not readouts:
            require_countdown_presentation("root", readout_count=0, title_count=0)
        for row, prop in readouts:
            group = _group_ids(row.component_id, components, parents)
            group_readouts = []
            for candidate, _prop in readouts:
                if candidate.component_id in group:
                    group_readouts.append(candidate)
            require_countdown_presentation(row.component_id, readout_count=len(group_readouts))
            titles = []
            if row.component_type == "InfoBlock":
                target = _target(row.props.get("secondaryText"), rule.path)
                if target is None and fallback_name:
                    target = fallback_name, False
                input_value = countdown_input_readout(row.props.get(prop), rule.path)
                if input_value is not None and input_value.target:
                    inline_target = _target(input_value.target, rule.path)
                    named_local = target is not None and target != ("目标日", False)
                    require_countdown_presentation(
                        row.component_id,
                        mixed_readout=named_local and target != inline_target,
                    )
                    target = inline_target
                if target is not None:
                    titles.append((row.props, "secondaryText", target))
            for key in group:
                candidate = components.get(key)
                if candidate is None:
                    continue
                if candidate.component_type not in {"SingleLineTitle", "DoubleLineTitle"}:
                    continue
                if row.component_type == "InfoBlock" and titles:
                    continue
                target = _target(candidate.props.get("title"), rule.path)
                if target is not None and target[1]:
                    if _schema(task, target[0]).get("type") != "string":
                        target = None
                if target is not None:
                    titles.append((candidate.props, "title", target))
            if not titles and row.component_type == "DataDisplay":
                target = _target(row.props.get("label"), rule.path)
                supporting = row.props.get("supportingText")
                existing = recognize_countdown(supporting)
                replaceable = supporting in (None, "", "天", "状态", "倒计时")
                replaceable = replaceable or existing is not None
                if target is not None and replaceable:
                    titles.append((row.props, "label", target))
            if not titles and row.component_type == "EmphasisText":
                target = _target(row.props.get("secondaryText"), rule.path)
                if target is None and fallback_name:
                    target = fallback_name, False
                if target is not None:
                    titles.append((row.props, "secondaryText", target))
            require_countdown_presentation(row.component_id, title_count=len(titles))
            title_props, title_prop, target = titles[0]
            planned_name = countdown_identity({}, plan, path=rule.path)
            if planned_name and not target[1]:
                target = planned_name, False
            if fallback_name and not target[1]:
                if target[0] == "目标日" or target[0] in fallback_name:
                    target = fallback_name, False
            input_value = countdown_input_readout(row.props.get(prop), rule.path)
            if input_value is not None and input_value.target:
                inline_target = _target(input_value.target, rule.path)
                require_countdown_presentation(
                    row.component_id, mixed_readout=inline_target != target,
                )
            owner = title_owners.get(id(title_props))
            require_countdown_presentation(
                row.component_id, mixed_readout=owner is not None and owner != rule.path,
            )
            title_owners[id(title_props)] = rule.path
            name, dynamic = target
            text_readout = row.component_type in {"InfoBlock", "EmphasisText", "Text"}
            if text_readout:
                title_props[title_prop] = {"path": name} if dynamic else name
                row.props[prop] = countdown_readout(rule.path)
                # 辅助日期等字面事实保留；只消除可证明的旧状态包装，不删除其内容。
                secondary = row.props.get("secondaryText")
                if title_prop != "secondaryText" or title_props is not row.props:
                    secondary_name = countdown_input_target(secondary, rule.path)
                    if secondary_name is not None:
                        row.props["secondaryText"] = secondary_name
                continue
            title_props[title_prop] = countdown_prefix(rule.path, name, target_path=dynamic)
            value = row.props.get(prop)
            unit = ""
            if isinstance(value, str) and "天" in value:
                unit = "天"
            if row.component_type == "EmphasizedData":
                row.props["unit"] = "天"
                unit = ""
            if row.component_type == "DataDisplay":
                supporting = row.props.get("supportingText")
                if supporting in (None, "", "天", "状态", "倒计时"):
                    row.props["supportingText"] = "天"
                    unit = ""
                else:
                    unit = "天"
            row.props[prop] = countdown_number(rule.path, unit)
    output = []
    for row in rows:
        if isinstance(row, ComponentRow):
            item = [row.component_id, row.component_type, row.props]
            if row.children:
                item.append(list(row.children))
        else:
            item = [row.path, row.value]
        output.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(output)


def _condition(rule: PresentationRule) -> str:
    ref = reference(rule.path)
    policy = presentation_policy()
    comparison = f"{ref} < {policy.battery_low}"
    if rule.kind == "countdown":
        comparison = f"{ref} <= {policy.countdown_near}"
    condition = f"{present(rule.path)} && {ref} >= 0 && {comparison}"
    if rule.connected_path:
        condition = reference(rule.connected_path) + " && " + condition
    return condition


def presentation_excluded_roots(compact_source: str | None) -> tuple[str, ...]:
    """按编译源的语义角色定位标题和动作根，不通过生成 ID 或业务文案猜测。"""
    if not compact_source:
        return ()
    from services.compact_dsl_a2ui_converter import parse_compact_dsl_rows
    from services.compact_high_level_components import ComponentRow

    excluded = []
    roles = {"SingleLineTitle", "DoubleLineTitle", "PillButton", "CircleButton", "CardButton"}
    for row in parse_compact_dsl_rows(compact_source, expand_layouts=False):
        if isinstance(row, ComponentRow) and row.component_type in roles:
            excluded.append(row.component_id)
    return tuple(excluded)


def apply_presentation_colors(
    source: str, card: dict, task: dict, *, uniform: bool = False,
    excluded_roots: tuple[str, ...] = (),
) -> str:
    """主题已完成后执行，只改读数及紧邻的独立单位；不改布局和业务数据。"""
    if uniform:
        return source
    rules = presentation_rules(card, task)
    if not rules:
        return source
    records = [json.loads(line) for line in source.splitlines() if line.strip()]
    nodes = []
    for record in records:
        nodes.extend(record.get("updateComponents", {}).get("components", []))
    by_id = {}
    parents = {}
    for node in nodes:
        by_id[node.get("id")] = node
        for child in node.get("children", []):
            parents[child] = node
    excluded = set()
    pending = list(excluded_roots)
    while pending:
        identifier = pending.pop()
        if identifier in excluded:
            continue
        excluded.add(identifier)
        parent = by_id.get(identifier, {})
        pending.extend(parent.get("children", []))
    for node in nodes:
        if node.get("component") != "Text" or node.get("id") in excluded:
            continue
        content = node.get("content")
        known = recognize_countdown(content)
        if known is not None and known.kind == "prefix":
            continue
        paths = _references(content)
        matched = []
        for rule in rules:
            has_readout = any(path in rule.display_paths for path in paths)
            extra_paths = []
            for path in paths:
                if path not in rule.display_paths:
                    extra_paths.append(path)
            boolean_controls = all(
                _schema(task, path).get("type") == "boolean" for path in extra_paths
            )
            if has_readout and boolean_controls:
                matched.append(rule)
        if len(matched) != 1:
            continue
        rule = matched[0]
        targets = [node]
        parent = parents.get(node.get("id"), {})
        siblings = parent.get("children", [])
        is_unit_group = parent.get("component") in {"Row", "Column"}
        if node.get("id") in siblings and is_unit_group:
            index = siblings.index(node.get("id"))
            unit = "天" if rule.kind == "countdown" else "%"
            adjacent = index + 1
            sibling = by_id.get(siblings[adjacent]) if adjacent < len(siblings) else None
            if isinstance(sibling, dict) and sibling.get("component") == "Text":
                if sibling.get("content") == unit:
                    targets.append(sibling)
        for target in targets:
            styles = target.get("styles", {})
            base = styles.get("fontColor")
            branches = conditional_colors(base)
            if branches:
                base = branches[-1]
            if not is_color(base):
                raise ValueError("Dynamic presentation requires a resolved normal font color")
            policy = presentation_policy()
            rgb = int(base.removeprefix("#")[2:], 16)
            brightness = ((rgb >> 16) * 0.299 + ((rgb >> 8) & 255) * 0.587 + (rgb & 255) * 0.114)
            red = policy.dark_red if brightness > 160 else policy.light_red
            styles["fontColor"] = wrapped(f"{_condition(rule)} ? {quote(red)} : {quote(base)}")
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in records)


def has_generated_presentation(source: str, card: dict, task: dict) -> bool:
    """仅识别程序的精确输出，供独立编辑保留已授权的条件依赖。"""
    rules = presentation_rules(card, task)
    for line in source.splitlines():
        record = json.loads(line)
        for node in record.get("updateComponents", {}).get("components", []):
            if recognize_countdown(node.get("content")) is not None:
                return True
            color = node.get("styles", {}).get("fontColor")
            branches = conditional_colors(color)
            if not branches:
                continue
            for rule in rules:
                expected = wrapped(
                    f"{_condition(rule)} ? {quote(branches[0])} : {quote(branches[1])}",
                )
                if color == expected:
                    return True
    return False


def prepare_presentation_edit(source: str, card: dict, task: dict, query: str) -> str:
    """显式统一配色只解除程序状态色，业务表达式继续受编辑保护。"""
    from services.post_layout_theme import user_color_overrides

    override = user_color_overrides(query).get("text")
    if not override or not uniform_text_request(query):
        return source
    if not has_generated_presentation(source, card, task):
        return source
    rules = presentation_rules(card, task)
    records = []
    changed = False
    for line in source.splitlines():
        record = json.loads(line)
        records.append(record)
        for node in record.get("updateComponents", {}).get("components", []):
            styles = node.get("styles", {})
            color = styles.get("fontColor")
            branches = conditional_colors(color)
            if not branches:
                continue
            for rule in rules:
                expected = wrapped(
                    f"{_condition(rule)} ? {quote(branches[0])} : {quote(branches[1])}",
                )
                if color == expected:
                    styles["fontColor"] = override.get("color")
                    changed = True
                    break
    if not changed:
        return source
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
