from __future__ import annotations

from typing import Any

from .config import DebugSettings
from .logging_utils import DebugLogger
from .schemas import DeviceDebugContext
from .skill_loader import SkillLoader
from .tool_registry import ToolRegistry, ToolValidationError
from .upstream_ws_client import UpstreamToolError, UpstreamWebSocketClient


class ToolDispatcher:
    """执行模型允许调用的两个原生函数及其四个内部业务工具。"""

    def __init__(
        self,
        settings: DebugSettings,
        loader: SkillLoader,
        registry: ToolRegistry,
        upstream: UpstreamWebSocketClient,
    ) -> None:
        self.settings = settings
        self.loader = loader
        self.registry = registry
        self.upstream = upstream
        self.log = DebugLogger(
            skill=settings.skill_name,
            trace=settings.log_trace,
            limit=settings.log_value_limit,
        )

    async def dispatch_load_skill(self, arguments: object) -> dict[str, Any]:
        resource_id = arguments.get("resourceId") if isinstance(arguments, dict) else ""
        self.log.event("load_skill_started", component="skill", resource_id=resource_id)
        if not isinstance(arguments, dict):
            return self._error("INVALID_ARGUMENTS", "load_skill arguments 必须是对象")
        if set(arguments) - {"skillName", "resourceId"}:
            return self._error("INVALID_ARGUMENTS", "load_skill 包含未声明字段")
        skill_name = arguments.get("skillName")
        requested_resource_id = arguments.get("resourceId")
        if skill_name != self.settings.skill_name or not isinstance(requested_resource_id, str):
            return self._error("INVALID_ARGUMENTS", "skillName 或 resourceId 无效")
        resource_id = self._canonical_resource_id(requested_resource_id)
        if resource_id is None:
            return self._error("UNKNOWN_RESOURCE", "未知 Skill 资源")
        if resource_id != "instructions" and "instructions" not in self.loader.loaded_resource_ids:
            return self._error("SKILL_ORDER", "必须先加载 instructions")
        if resource_id == "runtime-guide" and "instructions" not in self.loader.loaded_resource_ids:
            return self._error("SKILL_ORDER", "必须先加载 instructions")
        try:
            result = self.loader.load_for_model(resource_id)
            self.log.event(
                "load_skill_completed",
                component="skill",
                resource_id=resource_id,
                status="success",
                already_loaded=result.get("alreadyLoaded", False),
                content_chars=len(str(result.get("content", ""))),
            )
            return result
        except (OSError, ValueError) as exc:
            return self._error("SKILL_LOAD_FAILED", str(exc))

    @staticmethod
    def _canonical_resource_id(resource_id: str) -> str | None:
        """将模型可能从 Skill 文档复制的路径别名归一为逻辑资源 ID。"""
        if not isinstance(resource_id, str):
            return None
        stripped = resource_id.strip().strip("`\"'").strip()
        if not stripped:
            return None
        normalized = stripped.replace("\\", "/")
        if normalized.lower().startswith("skill://"):
            normalized = normalized[8:]
        normalized = normalized.lstrip("/")
        if any(part == ".." for part in normalized.split("/")):
            return None
        prefix = normalized.lower().find("/references/")
        if prefix >= 0:
            normalized = normalized[prefix + 1:]
        while normalized.startswith("./"):
            normalized = normalized[2:]
        aliases = {
            "skill.md": "instructions",
            "instructions": "instructions",
            "skill": "instructions",
            "references/runtime-guide.md": "runtime-guide",
            "references/runtime-guide": "runtime-guide",
            "runtime-guide.md": "runtime-guide",
            "runtime-guide": "runtime-guide",
            "runtime_guide.md": "runtime-guide",
            "runtime_guide": "runtime-guide",
            "references/examples.md": "examples",
            "references/examples": "examples",
            "examples.md": "examples",
            "examples": "examples",
            "references/user-replies.md": "user-replies",
            "references/user-replies": "user-replies",
            "user-replies.md": "user-replies",
            "user-replies": "user-replies",
        }
        direct = aliases.get(normalized.lower())
        if direct is not None:
            return direct
        for suffix, logical_id in (
            ("/references/runtime-guide.md", "runtime-guide"),
            ("/references/runtime-guide", "runtime-guide"),
            ("/references/runtime_guide.md", "runtime-guide"),
            ("/references/runtime_guide", "runtime-guide"),
            ("/references/examples.md", "examples"),
            ("/references/examples", "examples"),
            ("/references/user-replies.md", "user-replies"),
            ("/references/user-replies", "user-replies"),
            ("/skill.md", "instructions"),
        ):
            if normalized.lower().endswith(suffix):
                return logical_id
        if normalized.startswith("tool:"):
            tool_name = normalized.removeprefix("tool:").strip()
            return f"tool:{tool_name}" if tool_name else None
        if "/references/tools/" in normalized.lower() and normalized.lower().endswith(".json"):
            snapshot = normalized.rsplit("/", 1)[-1][:-5]
            if "__" in snapshot:
                _, tool_name = snapshot.rsplit("__", 1)
                return f"tool:{tool_name}" if tool_name else None
        return None

    async def dispatch_invoke(
        self,
        arguments: object,
        context: DeviceDebugContext,
        session_id: str,
        user_query: str,
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
        self.log.event(
            "invoke_started",
            component="tool",
            function=arguments.get("functionName") if isinstance(arguments, dict) else "",
            argument_keys=sorted(arguments.get("arguments", {}).keys())
            if isinstance(arguments, dict) and isinstance(arguments.get("arguments"), dict)
            else [],
        )
        if (
            "instructions" not in self.loader.loaded_resource_ids
            or "runtime-guide" not in self.loader.loaded_resource_ids
        ):
            return self._error(
                "SKILL_ORDER", "加载 instructions 和 runtime-guide 后才能 invoke"
            ), ()
        if not isinstance(arguments, dict):
            return self._error("INVALID_ARGUMENTS", "invoke arguments 必须是对象"), ()
        if set(arguments) - {"skillName", "functionName", "arguments"}:
            return self._error("INVALID_ARGUMENTS", "invoke 包含未声明字段"), ()
        skill_name = arguments.get("skillName")
        function_name = arguments.get("functionName")
        business_arguments = arguments.get("arguments")
        if skill_name != self.settings.skill_name or not isinstance(function_name, str):
            return self._error("INVALID_ARGUMENTS", "skillName 或 functionName 无效"), ()
        if not isinstance(business_arguments, dict):
            return self._error("INVALID_ARGUMENTS", "invoke.arguments 必须是对象"), ()
        supplied_bundle = business_arguments.get("bundleName")
        if supplied_bundle != self.settings.bundle_name:
            return self._error("INVALID_ARGUMENTS", "bundleName 必须匹配当前 Skill 工具"), ()
        business_arguments = dict(business_arguments)
        business_arguments.pop("bundleName", None)
        try:
            validated = self.registry.validate_arguments(function_name, business_arguments)
        except ToolValidationError as exc:
            return self._error("INVALID_ARGUMENTS", str(exc)), ()
        registered_tool = self.registry.get(function_name)
        if registered_tool.plugin_type == "Device":
            if function_name == "RequestDataPermission":
                return self._permission_stub(validated), ()
            return {"code": 0, "result": {}, "pluginType": "Device"}, ()
        try:
            result = await self.upstream.invoke(
                function_name,
                validated,
                context,
                session_id,
                user_query,
            )
        except UpstreamToolError as exc:
            self.log.event(
                "invoke_failed",
                component="tool",
                status="failed",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return self._error("UPSTREAM_FAILED", str(exc)), ()
        self.log.event(
            "invoke_completed",
            component="tool",
            function=function_name,
            status=result.status,
            error_code=result.error_code,
            frame_count=len(result.frames),
        )
        if result.status != "success" or result.error_code:
            return {
                "ok": False,
                "status": result.status,
                "error": {
                    "code": result.error_code or result.status,
                    "message": result.error_message or "正式工具返回业务失败状态",
                },
                "errorCode": result.error_code,
                "data": result.data,
            }, result.frames
        return result.data, result.frames

    def _permission_stub(self, arguments: dict[str, Any]) -> dict[str, Any]:
        capability_ids = arguments.get("dataCapabilityIds")
        if not isinstance(capability_ids, list) or not capability_ids:
            return self._error("INVALID_ARGUMENTS", "dataCapabilityIds 必须是非空字符串数组")
        if any(not isinstance(item, str) or not item.strip() for item in capability_ids):
            return self._error("INVALID_ARGUMENTS", "dataCapabilityIds 必须只包含非空字符串")
        return {
            "code": 0,
            "result": {"stateOfPermission": True, "nonAuthStatus": []},
        }

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message}}

