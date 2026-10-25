"""从明确标题和枚举分隔符提取原文成员；不推断任意自然语言事实。"""

from __future__ import annotations

import re
from dataclasses import dataclass


def normalized_content(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


@dataclass(frozen=True)
class ExplicitMember:
    section: str
    text: str

    @property
    def parts(self) -> tuple[str, ...]:
        # 时间是结构字段，可与正文拆开渲染；其它内容仍保持原文。
        time = re.match(r"(\d{1,2}:\d{2}(?:[-–]\d{1,2}:\d{2})?)(.+)", self.text)
        return (time.group(1), time.group(2)) if time else (self.text,)

    def has_section(self, value: str) -> bool:
        pattern = r"(?<![a-z0-9])" + re.escape(self.section.casefold()) + r"(?![a-z0-9])"
        return re.search(pattern, value.casefold()) is not None


def explicit_content_members(query: str) -> tuple[ExplicitMember, ...]:
    """支持任意标题的顿号或换行列表，避免绑定特定业务、名称和分组编号。"""
    headings = []
    for match in re.finditer(r"(?:^|[。；;\n])([^\n:：；;。]{1,120})[:：]", query):
        raw = match.group(1).strip()
        tail = re.split(r"[，,]", raw)[-1].strip()
        introduction = re.match(r"^(?:包含|包括|如下|分别为)\s*", tail)
        if tail != raw and introduction is not None:
            raw = tail.removeprefix(introduction.group(0)).strip()
        if not raw or re.fullmatch(r"[\d\s-]+", raw):
            continue
        headings.append((match, raw))
    members = []
    has_enumeration = False
    for index, (match, heading) in enumerate(headings):
        end = headings[index + 1][0].start() if index + 1 < len(headings) else len(query)
        body = query[slice(match.end(), end)].split("。", 1)[0].strip("；; \n")
        items = []
        for line in re.split(r"[、\n]", body):
            text = re.sub(r"^(?:[-*•]|\d+[.)])\s*", "", line.strip()).strip("；; ")
            if text:
                items.append(text)
        if len(items) > 1:
            has_enumeration = True
        for text in items:
            members.append(ExplicitMember(heading, text))
    return tuple(members) if has_enumeration else ()


def missing_content_members(
    query: str, fragments: list[tuple[str, tuple[str, ...]]],
) -> tuple[str, ...]:
    missing = []
    for member in explicit_content_members(query):
        matched = False
        for context, parts in fragments:
            if not member.has_section(context + " " + " ".join(parts)):
                continue
            visible = normalized_content(" ".join(parts))
            if all(normalized_content(part) in visible for part in member.parts):
                matched = True
                break
        if not matched:
            missing.append(f"{member.section}: {member.text}")
    return tuple(missing)
