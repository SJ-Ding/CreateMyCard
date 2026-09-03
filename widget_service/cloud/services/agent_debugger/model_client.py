"""DeepSeek HTTP function-calling client used only by the debugger agent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from config.config import Settings


class AgentModelError(RuntimeError):
    """智能体模型调用失败。"""


@dataclass(frozen=True)
class AgentToolCall:
    """模型输出的单个函数调用。"""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class AgentModelReply:
    """保留工具循环所需的 DeepSeek assistant 消息。"""

    content: str | None
    reasoning_content: str | None
    tool_calls: list[AgentToolCall]

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.reasoning_content:
            message["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class AgentModelClient:
    """直接使用已有 DeepSeek HTTP 配置，避免改变 DSL 文本客户端。"""

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http_client = http_client

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AgentModelReply:
        if not self._settings.deepseek_platform_http_api_key.strip():
            raise AgentModelError("DeepSeek HTTP API key is not configured")
        if not self._settings.deepseek_platform_http_url.strip():
            raise AgentModelError("DeepSeek HTTP URL is not configured")
        body = {
            "model": self._settings.agent_llm_model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": False,
            "temperature": self._settings.agent_llm_temperature,
            "top_p": self._settings.agent_llm_top_p,
            "max_tokens": self._settings.agent_llm_max_tokens,
            "thinking": {
                "type": "enabled" if self._settings.agent_llm_enable_thinking else "disabled"
            },
        }
        headers = {
            "Authorization": f"Bearer {self._settings.deepseek_platform_http_api_key}",
            "Content-Type": "application/json",
        }
        try:
            if self._http_client is not None:
                response = await self._http_client.post(
                    self._settings.deepseek_platform_http_url,
                    json=body,
                    headers=headers,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self._settings.agent_llm_request_timeout_seconds,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        self._settings.deepseek_platform_http_url,
                        json=body,
                        headers=headers,
                    )
        except httpx.TimeoutException as exc:
            raise AgentModelError("DeepSeek HTTP request timed out") from exc
        except httpx.RequestError as exc:
            raise AgentModelError("DeepSeek HTTP request failed") from exc
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise AgentModelError("DeepSeek HTTP returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AgentModelError("DeepSeek HTTP returned an invalid response")
        if response.is_error:
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else response.reason_phrase
            raise AgentModelError(f"DeepSeek HTTP error: {message}")
        return self._parse_reply(payload)

    @staticmethod
    def _parse_reply(payload: dict[str, Any]) -> AgentModelReply:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AgentModelError("DeepSeek HTTP response has no choice")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise AgentModelError("DeepSeek HTTP response has no message")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise AgentModelError("DeepSeek HTTP response content is invalid")
        raw_calls = message.get("tool_calls") or []
        if not isinstance(raw_calls, list):
            raise AgentModelError("DeepSeek HTTP tool calls are invalid")
        tool_calls: list[AgentToolCall] = []
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                raise AgentModelError("DeepSeek HTTP tool call is invalid")
            function = raw_call.get("function")
            call_id = raw_call.get("id")
            if not isinstance(function, dict) or not isinstance(call_id, str):
                raise AgentModelError("DeepSeek HTTP tool call is invalid")
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, str):
                raise AgentModelError("DeepSeek HTTP tool call is invalid")
            tool_calls.append(AgentToolCall(call_id=call_id, name=name, arguments=arguments))
        reasoning_content = message.get("reasoning_content")
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            reasoning_content = None
        if content is None and not tool_calls:
            raise AgentModelError("DeepSeek HTTP returned neither content nor tool calls")
        return AgentModelReply(
            content=content,
            reasoning_content=reasoning_content,
            tool_calls=tool_calls,
        )
