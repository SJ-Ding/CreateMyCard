from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from models.generation import ModelRequestContext

if TYPE_CHECKING:
    from config.config import Settings

from .config import DebugSettings
from .logging_utils import DebugLogger
from .skill_loader import SkillLoader
from .tool_dispatcher import ToolDispatcher
from .tool_registry import ToolRegistry

EventSink = Callable[[str, dict[str, Any], str], Awaitable[None]]


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class AgentRunError(RuntimeError):
    """模型循环无法产生合法结果。"""


class DebugAgentSession:
    """单个浏览器连接对应的主 Agent 会话和完整工具历史。"""

    def __init__(
        self,
        debug_settings: DebugSettings,
        *,
        production_settings: Settings | None = None,
        event_sink: EventSink | None = None,
        model_client_factory: Callable[..., Any] | None = None,
        upstream_client: Any | None = None,
    ) -> None:
        self.debug_settings = debug_settings
        self.debug_logger = DebugLogger(
            skill=debug_settings.skill_name,
            trace=debug_settings.log_trace,
            limit=debug_settings.log_value_limit,
        )
        if production_settings is None:
            from config.config import get_settings

            production_settings = get_settings()
        self.production_settings = production_settings
        self.session_id = uuid.uuid4().hex
        self.context = None
        self.current_query = ""
        self.running = False
        self.cancel_event = asyncio.Event()
        self._event_sink = event_sink
        self._model_client_factory = model_client_factory or self._default_model_client
        self._upstream_client = upstream_client
        self.loader: SkillLoader
        self.registry: ToolRegistry
        self.dispatcher: ToolDispatcher
        self.history: list[dict[str, Any]] = []
        self.last_artifact_url = ""
        self.artifacts: list[dict[str, Any]] = []
        self._initialize()

    def _initialize(self) -> None:
        skills_root = self.debug_settings.skill_root or (
            self.production_settings.repo_root / "skills"
        )
        self.loader = SkillLoader(
            skills_root.parent
            if skills_root.name == self.debug_settings.skill_name
            else skills_root,
            self.debug_settings.skill_name,
            self.debug_settings.skill_resource_max_bytes,
            self.debug_settings.skill_directory,
        )
        self.registry = ToolRegistry(
            self.loader.skill_root, self.loader.catalog, self.debug_settings.bundle_name
        )
        if self._upstream_client is None:
            from .upstream_ws_client import UpstreamWebSocketClient

            self._upstream_client = UpstreamWebSocketClient(
                self.debug_settings.upstream_base_url,
                self.debug_settings.bundle_name,
                self.debug_settings.request_timeout_seconds,
            )
        self.dispatcher = ToolDispatcher(
            self.debug_settings,
            self.loader,
            self.registry,
            self._upstream_client,
        )
        self.history = [{"role": "system", "content": self._system_prompt()}]
        self.debug_logger.event(
            "skill_initialized",
            component="agent",
            skill_version=self.debug_settings.skill_version,
            skill_path=str(self.loader.skill_root),
            prompt_path=str(self.debug_settings.system_prompt_path or "default"),
            tool_count=len(self.registry.tools),
        )

    def configure(self, context: Any) -> None:
        if self.running or len(self.history) != 1:
            raise ValueError("会话开始后不能修改配置，请新建会话")
        self.context = context

    def reset(self) -> None:
        if self.running:
            raise ValueError("运行中不能重置会话")
        preserved_context = self.context
        self.session_id = uuid.uuid4().hex
        self.current_query = ""
        self.last_artifact_url = ""
        self.artifacts.clear()
        self.cancel_event = asyncio.Event()
        self._initialize()
        self.context = preserved_context

    async def run(self, query: str) -> None:
        if self.running:
            raise ValueError("当前会话已有运行中的任务")
        if self.context is None:
            from .schemas import DeviceDebugContext

            self.context = DeviceDebugContext(
                uid=self.debug_settings.default_uid,
                odid=self.debug_settings.default_device_id,
                deviceId=self.debug_settings.default_device_id,
                phoneType=self.debug_settings.default_phone_type,
                appVersion=self.debug_settings.default_app_version,
                romVersion=self.debug_settings.default_rom_version,
                locale=self.debug_settings.default_locale,
                countryCode=self.debug_settings.default_country_code,
            )
        self.running = True
        self.cancel_event.clear()
        self.current_query = query
        run_id = uuid.uuid4().hex
        started = time.perf_counter()
        await self.emit("run_started", {"query": query}, run_id)
        self.debug_logger.event("run_started", component="agent", run_id=run_id, status="started")
        self.history.append({"role": "user", "content": query})
        try:
            await self._loop(run_id)
            await self.emit(
                "run_completed",
                {"durationMs": round((time.perf_counter() - started) * 1000, 2)},
                run_id,
            )
            self.debug_logger.event(
                "run_completed", component="agent", run_id=run_id, status="success"
            )
        except asyncio.CancelledError:
            await self.emit("run_cancelled", {}, run_id)
            raise
        except AgentRunError as exc:
            detail = _exception_detail(exc)
            self.debug_logger.event(
                "run_failed",
                component="agent",
                run_id=run_id,
                status="failed",
                error_type=type(exc).__name__,
                error_message=detail,
            )
            await self.emit("run_failed", {"error": detail}, run_id)
        except Exception as exc:  # pragma: no cover - 兜底保证页面得到稳定事件
            detail = _exception_detail(exc)
            self.debug_logger.event(
                "run_failed",
                component="agent",
                run_id=run_id,
                status="failed",
                error_type=type(exc).__name__,
                error_message=detail,
            )
            await self.emit("run_failed", {"error": detail}, run_id)
        finally:
            self.running = False

    async def cancel(self) -> None:
        self.cancel_event.set()

    async def emit(self, event_type: str, data: dict[str, Any], run_id: str = "") -> None:
        if self._event_sink is not None:
            await self._event_sink(event_type, data, run_id)

    async def _loop(self, run_id: str) -> None:
        for step in range(1, self.debug_settings.max_steps + 1):
            if self.cancel_event.is_set():
                raise asyncio.CancelledError
            client = self._model_client_factory(self._model_context(), self.production_settings)
            started = time.perf_counter()
            completion = await client.complete(
                list(self.history),
                tools=self.registry.model_tools,
                tool_choice="auto",
                max_tokens=self.debug_settings.max_tokens,
                enable_thinking = client.thinking_mode != "disable",
            )
            content = str(getattr(completion, "content", "") or "")
            tool_calls = tuple(getattr(completion, "tool_calls", ()) or ())
            self.debug_logger.event(
                "model_completed",
                component="model",
                run_id=run_id,
                step=step,
                status="success",
                history_count=len(self.history),
                model_tool_count=len(self.registry.model_tools),
                content_chars=len(content),
                tool_call_count=len(tool_calls),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            if content:
                await self.emit("assistant_message", {"content": content, "step": step}, run_id)
                if _contains_output_leak(content):
                    await self.emit(
                        "diagnostic",
                        {
                            "kind": "assistant_output_leak",
                            "message": "模型回复疑似暴露产物或内部字段",
                        },
                        run_id,
                    )
            if not tool_calls:
                if not content.strip():
                    raise AgentRunError("模型返回空内容且没有工具调用")
                self.history.append({"role": "assistant", "content": content})
                return
            assistant_message = {
                "role": "assistant",
                "content": content,
                "tool_calls": [self._tool_call_dict(call) for call in tool_calls],
            }
            self.history.append(assistant_message)
            for call in tool_calls:
                if self.cancel_event.is_set():
                    raise asyncio.CancelledError
                await self._execute_tool_call(call, run_id, step)
        raise AgentRunError(f"达到单轮最大模型步骤数 {self.debug_settings.max_steps}")

    async def _execute_tool_call(self, call: Any, run_id: str, step: int) -> None:
        call_id = str(_call_attr(call, "id") or uuid.uuid4().hex)
        function_name = str(_call_attr(call, "name") or "")
        raw_arguments = str(_call_attr(call, "arguments") or "")
        await self.emit(
            "tool_call",
            {
                "name": function_name,
                "callId": call_id,
                "arguments": raw_arguments,
                "functionName": self._tool_function_name(function_name, raw_arguments),
                "step": step,
            },
            run_id,
        )
        result, frames = await self._dispatch_call(function_name, raw_arguments)
        for frame in frames:
            await self.emit("upstream_frame", frame, run_id)
        await self._append_tool_result(call_id, result, run_id, step, call)
        await self._maybe_preview(run_id, function_name, result)

    async def _dispatch_call(
        self, function_name: str, raw_arguments: str
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": {"code": "INVALID_JSON", "message": str(exc)}}, ()
        if function_name == "load_skill":
            return await self.dispatcher.dispatch_load_skill(arguments), ()
        if function_name == "invoke":
            return await self.dispatcher.dispatch_invoke(
                arguments,
                self.context,
                self.session_id,
                self.current_query,
            )
        return {"ok": False, "error": {"code": "UNKNOWN_TOOL", "message": function_name}}, ()

    @staticmethod
    def _tool_function_name(tool_name: str, raw_arguments: str) -> str:
        if tool_name != "invoke":
            return ""
        try:
            parsed = json.loads(raw_arguments)
        except (TypeError, ValueError):
            return ""
        value = parsed.get("functionName") if isinstance(parsed, dict) else None
        return value if isinstance(value, str) else ""

    async def _append_tool_result(
        self,
        call_id: str,
        result: dict[str, Any],
        run_id: str,
        step: int,
        call: Any,
    ) -> None:
        result_text = json.dumps(result, ensure_ascii=False)
        self.history.append({"role": "tool", "tool_call_id": call_id, "content": result_text})
        await self.emit(
            "tool_result",
            {
                "callId": call_id,
                "result": result,
                "step": step,
                "name": str(_call_attr(call, "name") or ""),
                "functionName": _function_name_from_result(call, result),
            },
            run_id,
        )

    @staticmethod
    def _tool_function_name(tool_name: str, raw_arguments: str) -> str:
        if tool_name != "invoke":
            return ""
        try:
            parsed = json.loads(raw_arguments)
        except (TypeError, ValueError):
            return ""
        value = parsed.get("functionName") if isinstance(parsed, dict) else None
        return value if isinstance(value, str) else ""

    async def _maybe_preview(self, run_id: str, function_name: str, result: dict[str, Any]) -> None:
        if function_name != "invoke":
            return
        status = result.get("status")
        raw_artifact_url = result.get("artifactUrl")
        artifact_url = (
            _normalize_artifact_url(raw_artifact_url)
            if isinstance(raw_artifact_url, str)
            else ""
        )
        if (
            status not in {"success", "degraded"}
            or not artifact_url.strip()
        ):
            return
        if not _is_artifact_url(artifact_url):
            await self.emit(
                "diagnostic",
                {"kind": "artifact_url_invalid", "message": "生成结果未返回合法 artifact URL"},
                run_id,
            )
            return
        if artifact_url == self.last_artifact_url:
            await self.emit(
                "diagnostic",
                {"kind": "artifact_source_reused", "artifactUrl": artifact_url},
                run_id,
            )
            return
        self.last_artifact_url = artifact_url
        from .artifact_reader import ArtifactReader

        try:
            preview, error = await ArtifactReader().read(
                run_id, artifact_url, str(result.get("artifactDigest") or "")
            )
        except Exception as exc:  # artifact 面板失败不应改变生成工具结果
            await self.emit(
                "diagnostic",
                {"kind": "artifact_read_failed", "message": f"{type(exc).__name__}: {exc}"},
                run_id,
            )
            return
        if error is not None:
            await self.emit(
                "diagnostic", {"kind": "artifact_read_failed", "message": error}, run_id
            )
            return
        if preview is not None:
            self.artifacts.append(preview.model_dump(mode="json"))
            await self.emit("artifact_preview", preview.model_dump(mode="json"), run_id)
            if preview.digestMatches is False:
                await self.emit(
                    "diagnostic",
                    {"kind": "artifact_digest_mismatch", "artifactUrl": artifact_url},
                    run_id,
                )

    def _model_context(self) -> ModelRequestContext:
        context = self.context
        return ModelRequestContext(
            session_id=self.session_id,
            interaction_id=uuid.uuid4().hex,
            device_id=context.deviceId,
            country_code=context.countryCode,
            app_version=context.appVersion,
            app_name="com.huawei.hmos.vassistant",
        )

    def _default_model_client(
        self, request_context: ModelRequestContext, settings: Settings
    ) -> Any:
        from services.multi_step_generation.core.model_adapter import PlatformChatClient

        provider = settings.openai_master_client
        allowed = {"deepseek_platform", "llmclient"}
        if provider not in allowed:
            raise ValueError(f"Debug 服务不支持当前模型 Provider: {provider}")
        thinking_mode = "high" if settings.deepseek_enable_thinking else "disable"
        return PlatformChatClient(
            settings,
            request_context,
            thinking_mode=thinking_mode,
            request_timeout=self.debug_settings.request_timeout_seconds,
        )

    def _system_prompt(self) -> str:
        prompt_path = self.debug_settings.system_prompt_path
        if prompt_path is not None:
            content = prompt_path.read_text(encoding="utf-8")
        else:
            content = "请根据 Skill catalog 调用 load_skill，再通过 invoke 串行执行已注册函数。"
        return (
            f"{content}\n当前 Skill：{self.loader.catalog.name}\n"
            "模型工具仅允许：load_skill、invoke。"
            "首先使用如下工具调用导入初始化skill"
            r"""{"skillName":"harmony-card-generation-online","resourceId":"instructions"}"""
        )

    @staticmethod
    def _tool_call_dict(call: Any) -> dict[str, Any]:
        call_id = str(_call_attr(call, "id") or uuid.uuid4().hex)
        name = str(_call_attr(call, "name") or "")
        arguments = str(_call_attr(call, "arguments") or "")
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }


def _call_attr(call: Any, name: str) -> Any:
    if isinstance(call, dict):
        return call.get(name)
    return getattr(call, name, None)


def _function_name_from_result(call: Any, _result: dict[str, Any]) -> str:
    if str(_call_attr(call, "name") or "") != "invoke":
        return str(_call_attr(call, "name") or "")
    raw_arguments = _call_attr(call, "arguments")
    try:
        parsed = json.loads(str(raw_arguments or ""))
    except json.JSONDecodeError:
        parsed = {}
    function_name = parsed.get("functionName") if isinstance(parsed, dict) else None
    return str(function_name) if isinstance(function_name, str) else "invoke"


def _contains_output_leak(content: str) -> bool:
    markers = ("artifactUrl", "genWidgetResult", "genuiResult", "http://", "https://")
    return any(marker in content for marker in markers)


def _is_artifact_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not any(char.isspace() for char in value)
    )


def _normalize_artifact_url(value: str) -> str:
    """清理本地联调结果中包裹 URL 的 Markdown 边界标记。"""
    normalized = value.strip().strip("`\"'")
    if normalized.startswith("["):
        normalized = normalized[1:].lstrip()
    if "](" in normalized:
        normalized = normalized.split("](", 1)[0]
    if normalized.endswith("]"):
        normalized = normalized[:-1].rstrip()
    return normalized


def _exception_detail(error: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = error
    while current is not None and len(parts) < 4:
        text = f"{type(current).__name__}: {current}"
        if text not in parts:
            parts.append(text)
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)

