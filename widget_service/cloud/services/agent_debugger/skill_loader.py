"""受限地读取在线卡片 Skill 的渐进加载器。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from config.config import Settings

_SKILL_NAME = "harmony-card-generation-online"
_RESOURCE_PATHS = {
    "instructions": "SKILL.md",
    "runtime-guide": "references/runtime-guide.md",
    "examples": "references/examples.md",
    "tool:getWidgetCapabilityOverview": (
        "references/tools/com.omega_w_0823.hmservice__getWidgetCapabilityOverview.json"
    ),
    "tool:getDataCapabilitySchemas": (
        "references/tools/com.omega_w_0823.hmservice__getDataCapabilitySchemas.json"
    ),
    "tool:RequestDataPermission": (
        "references/tools/com.omega_w_0823.hmservice__RequestDataPermission.json"
    ),
    "tool:generateWidgetCardCompactDsl": (
        "references/tools/com.omega_w_0823.hmservice__generateWidgetCardCompactDsl.json"
    ),
}
ALLOWED_RESOURCE_IDS = tuple(_RESOURCE_PATHS)
_FRONTMATTER_PATTERN = re.compile(
    r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


class SkillLoadError(ValueError):
    """Skill 或其受控资源无法安全读取。"""


@dataclass(frozen=True)
class LoadedSkillResource:
    """一个已校验并锁定摘要的 Skill 资源。"""

    resource_id: str
    content: str
    digest: str


class SkillLoader:
    """仅允许读取单个在线卡片 Skill 的白名单资源。"""

    def __init__(self, settings: Settings, *, max_bytes: int = 512 * 1024) -> None:
        self._settings = settings
        self._max_bytes = max_bytes

    @property
    def skill_root(self) -> Path:
        return self._settings.resolved_agent_skill_root.resolve()

    def catalog(self) -> dict[str, str]:
        """从 frontmatter 取出可放进初始上下文的最小元数据。"""
        resource = self.load(_SKILL_NAME, "instructions")
        match = _FRONTMATTER_PATTERN.search(resource.content)
        if match is None:
            raise SkillLoadError("SKILL.md is missing YAML frontmatter")
        frontmatter = match.group("body")
        name = self._find_frontmatter_value(frontmatter, "name")
        description = self._find_frontmatter_value(frontmatter, "description")
        if name != _SKILL_NAME or not description:
            raise SkillLoadError("SKILL.md has invalid required frontmatter")
        return {
            "name": name,
            "description": description,
            "path": f"skill://{name}/SKILL.md",
            "digest": resource.digest,
        }

    def load(self, skill_name: str, resource_id: str) -> LoadedSkillResource:
        """读取一个白名单资源，拒绝一切自由路径。"""
        if skill_name != _SKILL_NAME:
            raise SkillLoadError("unknown skill")
        relative_path = _RESOURCE_PATHS.get(resource_id)
        if relative_path is None:
            raise SkillLoadError("unknown skill resource")
        skill_directory = (self.skill_root / _SKILL_NAME).resolve()
        resource_path = (skill_directory / relative_path).resolve()
        try:
            resource_path.relative_to(skill_directory)
        except ValueError as exc:
            raise SkillLoadError("skill resource escapes the configured root") from exc
        if not resource_path.is_file():
            raise SkillLoadError(f"skill resource is unavailable: {resource_id}")
        content_bytes = resource_path.read_bytes()
        if len(content_bytes) > self._max_bytes:
            raise SkillLoadError("skill resource exceeds the size limit")
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillLoadError("skill resource is not UTF-8") from exc
        return LoadedSkillResource(
            resource_id=resource_id,
            content=content,
            digest=hashlib.sha256(content_bytes).hexdigest(),
        )

    @staticmethod
    def _find_frontmatter_value(frontmatter: str, key: str) -> str:
        pattern = re.compile(
            rf"^{re.escape(key)}:\s*[\"']?(.*?)[\"']?\s*\r?$",
            re.MULTILINE,
        )
        match = pattern.search(frontmatter)
        return match.group(1).strip() if match else ""
