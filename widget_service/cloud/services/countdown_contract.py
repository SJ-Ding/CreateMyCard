"""关联倒计时的可信身份；不推断日期、地点或任意自然语言事实。"""

from __future__ import annotations

import re


def clean_countdown_name(value: str) -> str:
    text = value.strip()
    previous = re.fullmatch(r"(?:距离|距)(.+?)(?:还剩|已经|已过)", text)
    if previous is not None:
        text = previous.group(1)
    text = text.removesuffix("倒计时").strip()
    return "" if text in {"", "倒数日", "剩余天数", "天", "目标日"} else text


def countdown_identity(card: dict, plan: dict | None = None, *, path: str | None = None) -> str:
    """整卡身份仅适用于唯一倒计时；显式目标称谓优先于泛化元标题。"""
    if isinstance(plan, dict) and path is not None:
        for fact in plan.get("info_required") or []:
            if fact.get("dataId") == path:
                name = fact.get("displayName")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    bindings = []
    for binding in card.get("dataBindings") or []:
        if binding.get("capabilityId") == "GetCountdownDays":
            bindings.append(binding)
    if len(bindings) != 1:
        return ""
    title = card.get("title")
    return clean_countdown_name(title) if isinstance(title, str) else ""
