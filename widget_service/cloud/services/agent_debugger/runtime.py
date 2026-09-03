"""多轮 Skill 编排、内部工具和浏览器工具桥。"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from config.config import Settings
from services.agent_debugger.model_client import (
    AgentModelClient,
    AgentModelError,
    AgentToolCall,
)
from services.agent_debugger.skill_loader import (
    ALLOWED_RESOURCE_IDS,
    SkillLoader,
    SkillLoadError,
)

EmitEvent = Callable[[dict[str, Any]], Awaitable[None]]

_SKILL_NAME = "harmony-card-generation-online"
_CLIENT_TOOL_NAMES = frozenset(
    {
        "getWidgetCapabilityOverview",
        "getDataCapabilitySchemas",
        "generateWidgetCardCompactDsl",
    }
)
_INTERNAL_TOOL_NAMES = frozenset({"load_skill", "RequestDataPermission"})
_ARTIFACT_URL_PATTERN = re.compile(r"https?://[^\\s`]+")
_EDIT_PATTERN = re.compile(r"改|修改|调整|背景|颜色|尺寸|布局|文案|优化")


@dataclass
class PendingTool:
    """等待浏览器回传的单个工具调用。"""

    turn_id: str
    call_id: str
    name: str
    future: asyncio.Future[dict[str, Any]]


@dataclass
class ArtifactNode:
    """可用于连续编辑的最近成功生成节点。"""

    artifact_url: str
    arguments: dict[str, Any]
    data_capability_ids: list[str]


@dataclass
class ReplayPoint:
    """外部工具调用前的可恢复分支快照。"""

    call_id: str
    function_name: str
    arguments: dict[str, Any]
    messages: list[dict[str, Any]]
    artifact: ArtifactNode | None
    parent_call_id: str | None
    messages_after: list[dict[str, Any]] | None = None
    artifact_after: ArtifactNode | None = None


@dataclass
class Conversation:
    """单个调试会话的内存状态。"""

    conversation_id: str
    messages: list[dict[str, Any]]
    loaded_resources: dict[str, dict[str, str]] = field(default_factory=dict)
    artifact: ArtifactNode | None = None
    pending: PendingTool | None = None
    active_task: asyncio.Task[None] | None = None
    tool_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    replay_points: dict[str, ReplayPoint] = field(default_factory=dict)
    current_call_id: str | None = None
    sequence: int = 0
    last_access: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConversationStore:
    """带 TTL 与容量控制的单进程会话存储。"""

    def __init__(self, settings: Settings, skill_loader: SkillLoader) -> None:
        self._settings = settings
        self._skill_loader = skill_loader
        self._conversations: dict[str, Conversation] = {}
        self._lock = asyncio.Lock()

    async def open(self, conversation_id: str | None) -> Conversation:
        async with self._lock:
            await self._cleanup_locked()
            if conversation_id:
                existing = self._conversations.get(conversation_id)
                if existing is not None:
                    existing.last_access = time.monotonic()
                    return existing
            catalog = self._skill_loader.catalog()
            new_conversation = Conversation(
                conversation_id=str(uuid.uuid4()),
                messages=[
                    {
                        "role": "system",
                        "content": _build_system_prompt(catalog),
                    }
                ],
            )
            self._conversations[new_conversation.conversation_id] = new_conversation
            await self._trim_locked()
            return new_conversation

    async def cleanup(self) -> None:
        async with self._lock:
            await self._cleanup_locked()

    async def _cleanup_locked(self) -> None:
        deadline = time.monotonic() - self._settings.agent_session_ttl_seconds
        expired_ids = [
            conversation_id
            for conversation_id, conversation in self._conversations.items()
            if conversation.last_access < deadline
            and (conversation.active_task is None or conversation.active_task.done())
        ]
        for conversation_id in expired_ids:
            self._conversations.pop(conversation_id, None)

    async def _trim_locked(self) -> None:
        overflow = len(self._conversations) - self._settings.agent_max_sessions
        if overflow <= 0:
            return
        inactive = [
            conversation
            for conversation in self._conversations.values()
            if conversation.active_task is None or conversation.active_task.done()
        ]
        inactive.sort(key=lambda conversation: conversation.last_access)
        for conversation in inactive[:overflow]:
            self._conversations.pop(conversation.conversation_id, None)


class AgentRuntime:
    """将 DeepSeek function calls 串行映射为内部或浏览器侧工具。"""

    def __init__(
        self,
        settings: Settings,
        *,
        model_client: AgentModelClient | None = None,
        skill_loader: SkillLoader | None = None,
    ) -> None:
        self._settings = settings
        self._skill_loader = skill_loader or SkillLoader(settings)
        self._model_client = model_client or AgentModelClient(settings)
        self._store = ConversationStore(settings, self._skill_loader)

    async def open_conversation(self, conversation_id: str | None) -> Conversation:
        return await self._store.open(conversation_id)

    async def start_turn(self, conversation: Conversation, text: str, emit: EmitEvent) -> str:
        if not text.strip():
            raise ValueError("turn text must not be empty")
        if len(text) > self._settings.agent_user_message_max_chars:
            raise ValueError("turn text exceeds the size limit")
        async with conversation.lock:
            if conversation.active_task is not None and not conversation.active_task.done():
                raise ValueError("another turn is already active")
            turn_id = str(uuid.uuid4())
            conversation.last_access = time.monotonic()
            conversation.messages.append({"role": "user", "content": text})
            task = asyncio.create_task(self._run_turn(conversation, turn_id, text, emit))
            conversation.active_task = task
        return turn_id

    async def resolve_tool_result(
        self,
        conversation: Conversation,
        turn_id: str,
        call_id: str,
        result: dict[str, Any],
    ) -> bool:
        encoded_result = json.dumps(result, ensure_ascii=False)
        if len(encoded_result.encode("utf-8")) > self._settings.agent_tool_result_max_bytes:
            raise ValueError("tool result exceeds the size limit")
        async with conversation.lock:
            cached = conversation.tool_results.get(call_id)
            if cached is not None:
                return True
            pending = conversation.pending
            if pending is None or pending.turn_id != turn_id or pending.call_id != call_id:
                raise ValueError("tool result does not match a pending call")
            conversation.tool_results[call_id] = result
            if not pending.future.done():
                pending.future.set_result(result)
            return False

    async def start_replay(
        self,
        conversation: Conversation,
        source_call_id: str,
        function_name: str,
        arguments: dict[str, Any],
        emit: EmitEvent,
    ) -> str:
        """从历史工具节点的调用前快照创建新分支。"""
        async with conversation.lock:
            if conversation.active_task is not None and not conversation.active_task.done():
                raise ValueError("another turn is already active")
            replay_point = conversation.replay_points.get(source_call_id)
            if replay_point is None:
                raise ValueError("replay source call is unavailable")
            if replay_point.function_name != function_name:
                raise ValueError("replay function does not match the source call")
            turn_id = str(uuid.uuid4())
            conversation.last_access = time.monotonic()
            task = asyncio.create_task(
                self._run_replay(conversation, turn_id, replay_point, arguments, emit)
            )
            conversation.active_task = task
        return turn_id

    async def checkout_call(self, conversation: Conversation, call_id: str) -> None:
        """把当前会话切换到指定外部工具节点执行完成后的上下文。"""
        async with conversation.lock:
            if conversation.active_task is not None and not conversation.active_task.done():
                raise ValueError("cannot switch context while a turn is active")
            replay_point = conversation.replay_points.get(call_id)
            if replay_point is None or replay_point.messages_after is None:
                raise ValueError("tool call context is unavailable")
            conversation.messages = copy.deepcopy(replay_point.messages_after)
            conversation.artifact = copy.deepcopy(replay_point.artifact_after)
            conversation.current_call_id = call_id
            conversation.last_access = time.monotonic()

    async def cancel_turn(self, conversation: Conversation) -> None:
        async with conversation.lock:
            task = conversation.active_task
            if task is not None and not task.done():
                task.cancel()
            pending = conversation.pending
            if pending is not None and not pending.future.done():
                pending.future.cancel()

    async def close(self) -> None:
        await self._store.cleanup()

    async def _run_turn(
        self,
        conversation: Conversation,
        turn_id: str,
        user_text: str,
        emit: EmitEvent,
        *,
        start_message_sent: bool = False,
    ) -> None:
        await self._emit(
            conversation,
            emit,
            {"type": "turn.status", "turnId": turn_id, "status": "thinking"},
        )
        tool_calls = 0
        try:
            for _step in range(self._settings.agent_max_model_steps):
                reply = await self._model_client.complete(
                    conversation.messages,
                    self._tool_definitions(),
                )
                conversation.messages.append(reply.as_message())
                if reply.content and reply.content.strip():
                    visible_text = _sanitize_assistant_text(reply.content)
                    if visible_text:
                        await self._emit(
                            conversation,
                            emit,
                            {
                                "type": "assistant.message",
                                "turnId": turn_id,
                                "content": visible_text,
                            },
                        )
                if not reply.tool_calls:
                    self._refresh_current_replay_point(conversation)
                    await self._emit(
                        conversation,
                        emit,
                        {"type": "turn.completed", "turnId": turn_id, "status": "completed"},
                    )
                    return
                if len(reply.tool_calls) != 1:
                    await self._append_tool_error(
                        conversation,
                        reply.tool_calls,
                        "parallel tool calls are not supported; call exactly one tool",
                    )
                    continue
                tool_call = reply.tool_calls[0]
                if tool_call.name in _CLIENT_TOOL_NAMES and not start_message_sent:
                    start_message_sent = True
                    is_edit = bool(conversation.artifact) and bool(_EDIT_PATTERN.search(user_text))
                    start_text = (
                        "好的，我现在按你的要求修改卡片。"
                        if is_edit
                        else "好的，我现在为你创建卡片。"
                    )
                    await self._emit(
                        conversation,
                        emit,
                        {
                            "type": "assistant.message",
                            "turnId": turn_id,
                            "content": start_text,
                        },
                    )
                tool_calls += 1
                if tool_calls > self._settings.agent_max_tool_calls:
                    raise AgentModelError("agent tool call limit exceeded")
                await self._execute_tool_call(conversation, turn_id, tool_call, emit)
            raise AgentModelError("agent model step limit exceeded")
        except asyncio.CancelledError:
            await self._emit(
                conversation,
                emit,
                {"type": "turn.completed", "turnId": turn_id, "status": "cancelled"},
            )
            raise
        except (AgentModelError, SkillLoadError, ValueError) as exc:
            await self._emit(
                conversation,
                emit,
                {
                    "type": "error",
                    "turnId": turn_id,
                    "code": "AGENT_RUNTIME_ERROR",
                    "message": str(exc),
                },
            )
            await self._emit(
                conversation,
                emit,
                {"type": "turn.completed", "turnId": turn_id, "status": "failed"},
            )
        finally:
            async with conversation.lock:
                conversation.pending = None
                conversation.last_access = time.monotonic()

    async def _run_replay(
        self,
        conversation: Conversation,
        turn_id: str,
        replay_point: ReplayPoint,
        arguments: dict[str, Any],
        emit: EmitEvent,
    ) -> None:
        new_call_id = str(uuid.uuid4())
        conversation.messages = copy.deepcopy(replay_point.messages)
        conversation.artifact = copy.deepcopy(replay_point.artifact)
        conversation.current_call_id = replay_point.parent_call_id
        assistant_message = conversation.messages[-1] if conversation.messages else None
        if not isinstance(assistant_message, dict):
            await self._emit_replay_failure(conversation, turn_id, emit)
            return
        tool_calls = assistant_message.get("tool_calls")
        if not isinstance(tool_calls, list) or len(tool_calls) != 1:
            await self._emit_replay_failure(conversation, turn_id, emit)
            return
        raw_call = tool_calls[0]
        if not isinstance(raw_call, dict):
            await self._emit_replay_failure(conversation, turn_id, emit)
            return
        raw_call["id"] = new_call_id
        function = raw_call.get("function")
        if not isinstance(function, dict):
            await self._emit_replay_failure(conversation, turn_id, emit)
            return
        function["arguments"] = json.dumps(arguments, ensure_ascii=False)
        tool_call = AgentToolCall(
            call_id=new_call_id,
            name=replay_point.function_name,
            arguments=function["arguments"],
        )
        try:
            await self._execute_tool_call(
                conversation,
                turn_id,
                tool_call,
                emit,
                replayed_from_call_id=replay_point.call_id,
            )
            await self._run_turn(
                conversation,
                turn_id,
                "",
                emit,
                start_message_sent=True,
            )
        except asyncio.CancelledError:
            await self._emit(
                conversation,
                emit,
                {"type": "turn.completed", "turnId": turn_id, "status": "cancelled"},
            )
            raise
        except (AgentModelError, SkillLoadError, ValueError) as exc:
            await self._emit(
                conversation,
                emit,
                {
                    "type": "error",
                    "turnId": turn_id,
                    "code": "AGENT_REPLAY_ERROR",
                    "message": str(exc),
                },
            )
            await self._emit(
                conversation,
                emit,
                {"type": "turn.completed", "turnId": turn_id, "status": "failed"},
            )

    async def _emit_replay_failure(
        self,
        conversation: Conversation,
        turn_id: str,
        emit: EmitEvent,
    ) -> None:
        await self._emit(
            conversation,
            emit,
            {
                "type": "error",
                "turnId": turn_id,
                "code": "AGENT_REPLAY_ERROR",
                "message": "replay point has an invalid assistant tool call",
            },
        )
        await self._emit(
            conversation,
            emit,
            {"type": "turn.completed", "turnId": turn_id, "status": "failed"},
        )

    async def _execute_tool_call(
        self,
        conversation: Conversation,
        turn_id: str,
        tool_call: AgentToolCall,
        emit: EmitEvent,
        *,
        replayed_from_call_id: str | None = None,
    ) -> None:
        arguments = self._parse_arguments(tool_call)
        if tool_call.name == "load_skill":
            await self._execute_load_skill(conversation, turn_id, tool_call, arguments, emit)
            return
        if not self._runtime_guide_loaded(conversation):
            await self._append_tool_output(
                conversation,
                tool_call.call_id,
                {
                    "error": (
                        "load_skill(instructions) and load_skill(runtime-guide) "
                        "must complete before card tools"
                    )
                },
            )
            return
        if tool_call.name == "RequestDataPermission":
            await self._execute_permission(conversation, turn_id, tool_call, arguments, emit)
            return
        if tool_call.name not in _CLIENT_TOOL_NAMES:
            await self._append_tool_output(
                conversation,
                tool_call.call_id,
                {"error": "unknown tool"},
            )
            return
        self._validate_client_arguments(conversation, tool_call.name, arguments)
        parent_call_id = conversation.current_call_id
        conversation.replay_points[tool_call.call_id] = ReplayPoint(
            call_id=tool_call.call_id,
            function_name=tool_call.name,
            arguments=copy.deepcopy(arguments),
            messages=copy.deepcopy(conversation.messages),
            artifact=copy.deepcopy(conversation.artifact),
            parent_call_id=parent_call_id,
        )
        pending_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        pending = PendingTool(
            turn_id=turn_id,
            call_id=tool_call.call_id,
            name=tool_call.name,
            future=pending_future,
        )
        async with conversation.lock:
            conversation.pending = pending
        await self._emit(
            conversation,
            emit,
            {
                "type": "tool.call",
                "turnId": turn_id,
                "callId": tool_call.call_id,
                "executor": "client",
                "skillName": _SKILL_NAME,
                "functionName": tool_call.name,
                "arguments": arguments,
                "parentCallId": parent_call_id,
                "replayedFromCallId": replayed_from_call_id,
            },
        )

        await self._emit(
            conversation,
            emit,
            {"type": "turn.status", "turnId": turn_id, "status": "waiting_tool"},
        )
        try:
            result = await asyncio.wait_for(
                pending_future,
                timeout=self._settings.agent_tool_result_timeout_seconds,
            )
        finally:
            async with conversation.lock:
                if conversation.pending is pending:
                    conversation.pending = None
        self._update_artifact(conversation, tool_call.name, arguments, result)
        conversation.current_call_id = tool_call.call_id
        await self._append_tool_output(conversation, tool_call.call_id, result)
        replay_point = conversation.replay_points.get(tool_call.call_id)
        if replay_point is not None:
            replay_point.messages_after = copy.deepcopy(conversation.messages)
            replay_point.artifact_after = copy.deepcopy(conversation.artifact)
        await self._emit(
            conversation,
            emit,
            {
                "type": "tool.trace",
                "turnId": turn_id,
                "callId": tool_call.call_id,
                "executor": "client",
                "functionName": tool_call.name,
                "arguments": arguments,
                "result": result,
                "parentCallId": parent_call_id,
                "replayedFromCallId": replayed_from_call_id,
            },
        )

    @staticmethod
    def _refresh_current_replay_point(conversation: Conversation) -> None:
        call_id = conversation.current_call_id
        if call_id is None:
            return
        replay_point = conversation.replay_points.get(call_id)
        if replay_point is None or replay_point.messages_after is None:
            return
        replay_point.messages_after = copy.deepcopy(conversation.messages)
        replay_point.artifact_after = copy.deepcopy(conversation.artifact)

    @staticmethod
    def _runtime_guide_loaded(conversation: Conversation) -> bool:
        return (
            "instructions" in conversation.loaded_resources
            and "runtime-guide" in conversation.loaded_resources
        )

    async def _execute_load_skill(
        self,
        conversation: Conversation,
        turn_id: str,
        tool_call: AgentToolCall,
        arguments: dict[str, Any],
        emit: EmitEvent,
    ) -> None:
        skill_name = arguments.get("skillName")
        resource_id = arguments.get("resourceId")
        if not isinstance(skill_name, str) or not isinstance(resource_id, str):
            raise ValueError("load_skill requires skillName and resourceId")
        cached = conversation.loaded_resources.get(resource_id)
        if cached is None:
            try:
                loaded = self._skill_loader.load(skill_name, resource_id)
            except SkillLoadError as exc:
                await self._append_tool_output(
                    conversation,
                    tool_call.call_id,
                    {
                        "error": str(exc),
                        "allowedSkillName": _SKILL_NAME,
                        "allowedResourceIds": list(ALLOWED_RESOURCE_IDS),
                        "instruction": "Retry load_skill with one exact logical resourceId.",
                    },
                )
                return
            cached = {"content": loaded.content, "digest": loaded.digest}
            conversation.loaded_resources[resource_id] = cached
        output = {
            "resourceId": resource_id,
            "digest": cached["digest"],
            "content": cached["content"],
        }
        await self._append_tool_output(conversation, tool_call.call_id, output)
        await self._emit(
            conversation,
            emit,
            {
                "type": "tool.trace",
                "turnId": turn_id,
                "callId": tool_call.call_id,
                "executor": "server",
                "functionName": "load_skill",
                "arguments": arguments,
                "result": {
                    "resourceId": resource_id,
                    "digest": cached["digest"],
                    "loaded": True,
                },
            },
        )

    async def _execute_permission(
        self,
        conversation: Conversation,
        turn_id: str,
        tool_call: AgentToolCall,
        arguments: dict[str, Any],
        emit: EmitEvent,
    ) -> None:
        capability_ids = arguments.get("dataCapabilityIds")
        if (
            not isinstance(capability_ids, list)
            or not capability_ids
            or not all(
                isinstance(capability_id, str) and capability_id for capability_id in capability_ids
            )
        ):
            raise ValueError("RequestDataPermission requires non-empty dataCapabilityIds")
        output = {"code": 0, "result": {"stateOfPermission": True, "nonAuthStatus": []}}
        await self._append_tool_output(conversation, tool_call.call_id, output)
        await self._emit(
            conversation,
            emit,
            {
                "type": "tool.trace",
                "turnId": turn_id,
                "callId": tool_call.call_id,
                "executor": "server",
                "functionName": "RequestDataPermission",
                "arguments": arguments,
                "result": output,
                "simulated": True,
            },
        )

    @staticmethod
    def _parse_arguments(tool_call: AgentToolCall) -> dict[str, Any]:
        try:
            arguments = json.loads(tool_call.arguments)
        except json.JSONDecodeError as exc:
            raise ValueError(f"tool {tool_call.name} has invalid JSON arguments") from exc
        if not isinstance(arguments, dict):
            raise ValueError(f"tool {tool_call.name} arguments must be an object")
        return arguments

    @staticmethod
    def _validate_client_arguments(
        conversation: Conversation,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        if tool_name == "getWidgetCapabilityOverview":
            if arguments:
                raise ValueError("getWidgetCapabilityOverview does not accept arguments")
            return
        if tool_name == "getDataCapabilitySchemas":
            ids = arguments.get("dataCapabilityIds")
            has_only_strings = isinstance(ids, list) and all(
                isinstance(item, str) for item in ids
            )
            if not ids or not has_only_strings:
                raise ValueError("getDataCapabilitySchemas requires non-empty dataCapabilityIds")
            return
        user_query = arguments.get("userQuery")
        if not isinstance(user_query, str) or not user_query.strip():
            raise ValueError("generateWidgetCardCompactDsl requires userQuery")
        source_url = arguments.get("sourceArtifactUrl")
        if source_url is not None:
            artifact = conversation.artifact
            if artifact is None or source_url != artifact.artifact_url:
                raise ValueError("sourceArtifactUrl must match the latest valid artifact")

    @staticmethod
    def _update_artifact(
        conversation: Conversation,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if tool_name != "generateWidgetCardCompactDsl":
            return
        response = result.get("response")
        if not isinstance(response, dict):
            return
        data = response.get("data")
        if not isinstance(data, dict):
            return
        status = data.get("status")
        artifact_url = data.get("artifactUrl")
        old_url = arguments.get("sourceArtifactUrl")
        if status not in {"success", "degraded"}:
            return
        if not isinstance(artifact_url, str) or not artifact_url.strip() or artifact_url == old_url:
            return
        bindings = arguments.get("candidateDataBindings")
        capability_ids: list[str] = []
        if isinstance(bindings, list):
            for binding in bindings:
                if isinstance(binding, dict):
                    capability_id = binding.get("capabilityId")
                    if isinstance(capability_id, str):
                        capability_ids.append(capability_id)
        conversation.artifact = ArtifactNode(
            artifact_url=artifact_url,
            arguments=dict(arguments),
            data_capability_ids=capability_ids,
        )

    @staticmethod
    async def _append_tool_output(
        conversation: Conversation,
        call_id: str,
        payload: dict[str, Any],
    ) -> None:
        conversation.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(payload, ensure_ascii=False),
            }
        )

    @staticmethod
    async def _append_tool_error(
        conversation: Conversation,
        calls: list[AgentToolCall],
        message: str,
    ) -> None:
        for call in calls:
            await AgentRuntime._append_tool_output(
                conversation,
                call.call_id,
                {"error": message},
            )

    @staticmethod
    async def _emit(conversation: Conversation, emit: EmitEvent, event: dict[str, Any]) -> None:
        conversation.sequence += 1
        event.setdefault("protocolVersion", "1.0")
        event.setdefault("conversationId", conversation.conversation_id)
        event.setdefault("seq", conversation.sequence)
        await emit(event)

    @staticmethod
    def _tool_definitions() -> list[dict[str, Any]]:
        return [
            _function(
                "load_skill",
                "按逻辑 resourceId 渐进加载在线卡片 Skill；不要传文件名或路径。",
                {
                    "skillName": _string(),
                    "resourceId": {
                        "type": "string",
                        "enum": list(ALLOWED_RESOURCE_IDS),
                    },
                },
                ["skillName", "resourceId"],
            ),
            _function(
                "getWidgetCapabilityOverview",
                "获取当前可用的数据、事件和素材能力概述。",
                {},
                [],
            ),
            _function(
                "getDataCapabilitySchemas",
                "加载已选数据能力的完整输入输出 schema。",
                {"dataCapabilityIds": _capability_ids_schema()},
                ["dataCapabilityIds"],
            ),
            _function(
                "RequestDataPermission",
                "检查最终数据能力集合的授权状态。",
                {"dataCapabilityIds": _capability_ids_schema()},
                ["dataCapabilityIds"],
            ),
            _function(
                "generateWidgetCardCompactDsl",
                "按已验证候选生成或编辑桌面卡片。",
                _generate_properties(),
                ["userQuery"],
            ),
        ]


def _build_system_prompt(catalog: dict[str, str]) -> str:
    return (
        "你是本机卡片链路调试器的编排智能体。仅在用户有桌面卡片意图时工作。"
        "初始可用 Skill 为："
        f"name={catalog['name']}；description={catalog['description']}；path={catalog['path']}。"
        "命中后必须先按顺序调用 load_skill(instructions) 与 load_skill(runtime-guide)，"
        "再严格遵循加载内容。一次只能调用一个工具。"
        "三个云工具由浏览器执行，绝不能编造 URL、包络、能力或工具结果。"
        "不要向用户输出 artifactUrl、能力 ID、schema、内部错误或工具名称。"
    )


def _sanitize_assistant_text(content: str) -> str:
    sanitized = _ARTIFACT_URL_PATTERN.sub("[已隐藏]", content)
    return sanitized.replace("artifactUrl", "产物地址")


def _string() -> dict[str, str]:
    return {"type": "string"}


def _capability_ids_schema() -> dict[str, Any]:
    return {"type": "array", "items": _string(), "minItems": 1}


def _function(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _generate_properties() -> dict[str, Any]:
    return {
        "userQuery": _string(),
        "sourceArtifactUrl": _string(),
        "size": {"type": "string", "enum": ["2x2", "2x4"]},
        "title": _string(),
        "description": _string(),
        "candidateDataBindings": {"type": "array", "items": {"type": "object"}},
        "candidateEventCandidates": {"type": "array", "items": {"type": "object"}},
        "candidateAssetIds": {"type": "array", "items": _string()},
        "options": {"type": "object"},
    }
