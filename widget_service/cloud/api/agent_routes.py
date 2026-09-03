"""本机智能体调试 WebSocket 路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config.config import get_settings
from services.agent_debugger.runtime import AgentRuntime, Conversation

router = APIRouter(prefix="/api/v1")

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


@router.websocket("/ws/agent/chat")
async def agent_chat_websocket(websocket: WebSocket) -> None:
    """处理调试器的多轮消息与浏览器工具结果。"""
    settings = get_settings()
    if not settings.enable_agent_debugger:
        await websocket.accept()
        await websocket.send_json(
            {
                "protocolVersion": "1.0",
                "type": "error",
                "code": "AGENT_DEBUGGER_DISABLED",
                "message": "智能体调试接口未启用。",
            }
        )
        await websocket.close(code=1008)
        return
    if not _is_local_connection(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    runtime = _get_runtime(websocket)
    conversation: Conversation | None = None
    try:
        while True:
            payload = await websocket.receive_json()
            if not isinstance(payload, dict):
                await websocket.send_json(_error("PROTOCOL_INVALID", "消息必须是 JSON 对象。"))
                continue
            event_type = payload.get("type")
            if event_type == "conversation.open":
                requested_id = payload.get("conversationId")
                if requested_id is not None and not isinstance(requested_id, str):
                    await websocket.send_json(
                        _error("PROTOCOL_INVALID", "conversationId 必须是字符串。")
                    )
                    continue
                conversation = await runtime.open_conversation(requested_id)
                await websocket.send_json(
                    {
                        "protocolVersion": "1.0",
                        "type": "conversation.ready",
                        "conversationId": conversation.conversation_id,
                        "resumed": requested_id == conversation.conversation_id,
                        "hasArtifact": conversation.artifact is not None,
                    }
                )
                continue
            if conversation is None:
                await websocket.send_json(
                    _error("CONVERSATION_REQUIRED", "请先发送 conversation.open。")
                )
                continue
            if event_type == "conversation.reset":
                await runtime.cancel_turn(conversation)
                conversation = await runtime.open_conversation(None)
                await websocket.send_json(
                    {
                        "protocolVersion": "1.0",
                        "type": "conversation.ready",
                        "conversationId": conversation.conversation_id,
                        "resumed": False,
                        "hasArtifact": False,
                    }
                )
                continue
            if event_type == "turn.start":
                text = payload.get("text")
                if not isinstance(text, str):
                    await websocket.send_json(_error("PROTOCOL_INVALID", "text 必须是字符串。"))
                    continue
                try:
                    turn_id = await runtime.start_turn(conversation, text, websocket.send_json)
                except ValueError as exc:
                    await websocket.send_json(_error("TURN_REJECTED", str(exc)))
                    continue
                await websocket.send_json(
                    {
                        "protocolVersion": "1.0",
                        "type": "turn.status",
                        "conversationId": conversation.conversation_id,
                        "turnId": turn_id,
                        "status": "accepted",
                    }
                )
                continue
            if event_type == "turn.replay":
                await _handle_turn_replay(runtime, conversation, payload, websocket)
                continue
            if event_type == "conversation.checkout":
                await _handle_conversation_checkout(runtime, conversation, payload, websocket)
                continue
            if event_type == "tool.result":
                await _handle_tool_result(runtime, conversation, payload, websocket)
                continue
            if event_type == "turn.cancel":
                await runtime.cancel_turn(conversation)
                continue
            await websocket.send_json(_error("PROTOCOL_INVALID", "不支持的事件类型。"))
    except WebSocketDisconnect:
        if conversation is not None:
            await runtime.cancel_turn(conversation)


async def _handle_tool_result(
    runtime: AgentRuntime,
    conversation: Conversation,
    payload: dict[str, Any],
    websocket: WebSocket,
) -> None:
    turn_id = payload.get("turnId")
    call_id = payload.get("callId")
    result = payload.get("result")
    if not isinstance(turn_id, str) or not isinstance(call_id, str) or not isinstance(result, dict):
        await websocket.send_json(_error("PROTOCOL_INVALID", "tool.result 字段不完整。"))
        return
    try:
        duplicate = await runtime.resolve_tool_result(conversation, turn_id, call_id, result)
    except ValueError as exc:
        await websocket.send_json(_error("TOOL_RESULT_REJECTED", str(exc)))
        return
    await websocket.send_json(
        {
            "protocolVersion": "1.0",
            "type": "tool.result.accepted",
            "conversationId": conversation.conversation_id,
            "turnId": turn_id,
            "callId": call_id,
            "duplicate": duplicate,
        }
    )


async def _handle_turn_replay(
    runtime: AgentRuntime,
    conversation: Conversation,
    payload: dict[str, Any],
    websocket: WebSocket,
) -> None:
    source_call_id = payload.get("sourceCallId")
    function_name = payload.get("functionName")
    arguments = payload.get("arguments")
    fields_valid = isinstance(source_call_id, str) and isinstance(function_name, str)
    if not fields_valid or not isinstance(arguments, dict):
        await websocket.send_json(_error("PROTOCOL_INVALID", "turn.replay 字段不完整。"))
        return
    try:
        turn_id = await runtime.start_replay(
            conversation,
            source_call_id,
            function_name,
            arguments,
            websocket.send_json,
        )
    except ValueError as exc:
        await websocket.send_json(_error("TURN_REPLAY_REJECTED", str(exc)))
        return
    await websocket.send_json(
        {
            "protocolVersion": "1.0",
            "type": "turn.status",
            "conversationId": conversation.conversation_id,
            "turnId": turn_id,
            "status": "accepted",
            "replayedFromCallId": source_call_id,
        }
    )


async def _handle_conversation_checkout(
    runtime: AgentRuntime,
    conversation: Conversation,
    payload: dict[str, Any],
    websocket: WebSocket,
) -> None:
    call_id = payload.get("callId")
    if not isinstance(call_id, str):
        await websocket.send_json(_error("PROTOCOL_INVALID", "conversation.checkout 缺少 callId。"))
        return
    try:
        await runtime.checkout_call(conversation, call_id)
    except ValueError as exc:
        await websocket.send_json(_error("CONTEXT_CHECKOUT_REJECTED", str(exc)))
        return
    await websocket.send_json(
        {
            "protocolVersion": "1.0",
            "type": "conversation.checked_out",
            "conversationId": conversation.conversation_id,
            "callId": call_id,
        }
    )


def _get_runtime(websocket: WebSocket) -> AgentRuntime:
    runtime = getattr(websocket.app.state, "agent_runtime", None)
    if not isinstance(runtime, AgentRuntime):
        raise RuntimeError("agent runtime is unavailable")
    return runtime


def _is_local_connection(websocket: WebSocket) -> bool:
    client = websocket.client
    if client is None or client.host not in _LOCAL_HOSTS:
        return False
    origin = websocket.headers.get("origin")
    return (
        origin is None
        or origin == "null"
        or origin.startswith("http://localhost")
        or origin.startswith("http://127.0.0.1")
    )


def _error(code: str, message: str) -> dict[str, str]:
    return {"protocolVersion": "1.0", "type": "error", "code": code, "message": message}
