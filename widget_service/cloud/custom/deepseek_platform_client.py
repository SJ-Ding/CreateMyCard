# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import websockets

from app.logger import logger
from config.config import Settings
from custom.model_transport import ModelTransportError
from models.generation import ModelRequestContext
from utils.base_utils import sts_config

_MODULE = "[DeepSeek Platform]"

SecretLoader = Callable[[str], bytes | str]
TimestampProvider = Callable[[], int]


class DeepSeekPlatformClient:
    """按配置使用 DeepSeek 官方 HTTP 或原 Platform WebSocket 生成文本。"""

    def __init__(
            self,
            settings: Settings,
            *,
            secret_loader: SecretLoader | None = None,
            timestamp_provider: TimestampProvider | None = None,
            http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._secret_loader = secret_loader or sts_config.get_sts_config
        self._timestamp_provider = timestamp_provider or self._current_timestamp_ms
        self._http_client = http_client

    async def generate(
            self,
            messages: list[dict[str, str]],
            request_context: ModelRequestContext,
    ) -> str:
        """通过配置选择的传输方式发送请求并返回完整文本。"""
        self._validate_configuration()
        if self.settings.deepseek_platform_transport == "http":
            return await self._generate_http(messages)
        return await self._generate_websocket(messages, request_context)

    async def _generate_websocket(
            self,
            messages: list[dict[str, str]],
            request_context: ModelRequestContext,
    ) -> str:
        """使用原 Platform WebSocket 协议生成完整模型文本。"""
        headers = self._build_headers(request_context)
        body = self._build_body(messages, request_context)
        partial_texts: list[str] = []
        model_metrics: dict[str, Any] = {}
        start = time.perf_counter()
        first_token_at: float | None = None
        final_text: str | None = None
        try:
            async with websockets.connect(
                    self.settings.deepseek_platform_ws_url,
                    additional_headers=headers,
                    open_timeout=self.settings.model_request_timeout_seconds,
                    proxy=None,
            ) as websocket:
                await websocket.send(json.dumps(body, ensure_ascii=False))
                async for message in websocket:
                    final_text = self._process_message(message, partial_texts, model_metrics)
                    if final_text is not None:
                        if first_token_at is None and partial_texts:
                            first_token_at = time.perf_counter()
                        return final_text
        except ModelTransportError:
            raise
        except Exception as exc:
            logger.error(
                f"{_MODULE} request_failed exception_type={type(exc).__name__} "
                f"exception={exc!r}"
            )
            raise ModelTransportError(
                "DeepSeek Platform request failed",
                partial_output="".join(partial_texts),
            ) from exc
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            first_token_latency_ms = (
                round((first_token_at - start) * 1000, 2)
                if first_token_at is not None else None
            )
            decode_duration_ms = (
                round(duration_ms - first_token_latency_ms, 2)
                if first_token_latency_ms is not None else None
            )
            input_tokens = model_metrics.get("inputTokenNum")
            completion_tokens = model_metrics.get("generateTokenNum")
            model_time_ms = model_metrics.get("modelTime")
            output_length = len(final_text) if final_text else 0
            speed_str = "N/A"
            if completion_tokens and model_time_ms:
                try:
                    model_time_sec = float(model_time_ms) / 1000
                    if model_time_sec > 0:
                        speed_str = f"{completion_tokens / model_time_sec:.2f}"
                except (ValueError, TypeError):
                    pass
            logger.info(
                f"{_MODULE} stream_metrics "
                f"duration_ms={duration_ms}ms "
                f"first_token_latency_ms={first_token_latency_ms}ms "
                f"decode_duration_ms={decode_duration_ms}ms "
                f"input_tokens={input_tokens} "
                f"completion_tokens={completion_tokens} "
                f"tokens_per_sec={speed_str} "
                f"output_length={output_length} "
                f"output_preview=\n{final_text}"
            )
        raise ModelTransportError(
            "DeepSeek Platform connection closed before finalText",
            code="MODEL_STREAM_INCOMPLETE",
            partial_output="".join(partial_texts),
        )

    async def _generate_http(self, messages: list[dict[str, str]]) -> str:
        """调用 DeepSeek 官方非流式 Chat Completions HTTP 接口。"""
        request_body = self._build_http_body(messages)
        headers = {
            "Authorization": (
                f"Bearer {self.settings.deepseek_platform_http_api_key}"
            ),
            "Content-Type": "application/json",
        }
        started_at = time.perf_counter()
        try:
            response = await self._post_http(request_body, headers)
            response_data = self._decode_http_response(response)
            self._raise_for_http_error(response, response_data)
            final_text, usage = self._extract_http_result(response_data)
            self._log_http_metrics(started_at, final_text, usage)
            return final_text
        except ModelTransportError:
            raise
        except httpx.TimeoutException as exc:
            self._log_http_error("request_timeout", exc)
            raise ModelTransportError(
                "DeepSeek official HTTP request timed out",
                code="MODEL_REQUEST_TIMEOUT",
            ) from exc
        except httpx.RequestError as exc:
            self._log_http_error("request_failed", exc)
            raise ModelTransportError(
                "DeepSeek official HTTP request failed",
            ) from exc
        except Exception as exc:
            self._log_http_error("unexpected_error", exc)
            raise ModelTransportError(
                "DeepSeek official HTTP request failed",
            ) from exc

    async def _post_http(
            self,
            request_body: dict[str, Any],
            headers: dict[str, str],
    ) -> httpx.Response:
        request_url = self.settings.deepseek_platform_http_url
        if self._http_client is not None:
            return await self._http_client.post(
                request_url,
                json=request_body,
                headers=headers,
            )
        async with httpx.AsyncClient(
                timeout=self.settings.model_request_timeout_seconds,
                trust_env=False,
        ) as http_client:
            return await http_client.post(
                request_url,
                json=request_body,
                headers=headers,
            )

    def _build_http_body(
            self,
            messages: list[dict[str, str]],
    ) -> dict[str, Any]:
        thinking_type = (
            "enabled" if self.settings.deepseek_enable_thinking else "disabled"
        )
        return {
            "model": self.settings.deepseek_platform_http_model,
            "messages": [dict(message) for message in messages],
            "stream": False,
            "temperature": self.settings.deepseek_temperature,
            "top_p": self.settings.deepseek_top_p,
            "max_tokens": self.settings.deepseek_max_tokens,
            "thinking": {"type": thinking_type},
        }

    @staticmethod
    def _decode_http_response(response: httpx.Response) -> dict[str, Any]:
        try:
            response_data = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ModelTransportError(
                "DeepSeek official HTTP returned invalid JSON",
                code=str(response.status_code),
            ) from exc
        if not isinstance(response_data, dict):
            raise ModelTransportError(
                "DeepSeek official HTTP returned an invalid response shape",
                code=str(response.status_code),
            )
        return response_data

    @staticmethod
    def _raise_for_http_error(
            response: httpx.Response,
            response_data: dict[str, Any],
    ) -> None:
        if not response.is_error:
            return
        error = response_data.get("error")
        error_data = error if isinstance(error, dict) else {}
        error_code = error_data.get("code") or response.status_code
        error_message = error_data.get("message") or response.reason_phrase
        raise ModelTransportError(
            "DeepSeek official HTTP returned error: "
            f"code={error_code}, message={error_message}",
            code=str(error_code),
        )

    @staticmethod
    def _extract_http_result(
            response_data: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        choices = response_data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelTransportError(
                "DeepSeek official HTTP response has no choices",
                code="MODEL_EMPTY_OUTPUT",
            )
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise ModelTransportError(
                "DeepSeek official HTTP returned an invalid choice",
                code="MODEL_EMPTY_OUTPUT",
            )
        message = first_choice.get("message")
        message_data = message if isinstance(message, dict) else {}
        content = message_data.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelTransportError(
                "DeepSeek official HTTP returned empty content",
                code="MODEL_EMPTY_OUTPUT",
            )
        usage = response_data.get("usage")
        usage_data = usage if isinstance(usage, dict) else {}
        return content, usage_data

    @staticmethod
    def _log_http_metrics(
            started_at: float,
            final_text: str,
            usage: dict[str, Any],
    ) -> None:
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
        logger.info(
            f"{_MODULE} http_metrics "
            f"duration_ms={duration_ms}ms "
            f"input_tokens={usage.get('prompt_tokens')} "
            f"completion_tokens={usage.get('completion_tokens')} "
            f"total_tokens={usage.get('total_tokens')} "
            f"output_length={len(final_text)}"
        )

    @staticmethod
    def _log_http_error(event: str, exc: Exception) -> None:
        logger.error(
            f"{_MODULE} http_{event} exception_type={type(exc).__name__} "
            f"exception={exc!r}"
        )

    def _validate_configuration(self) -> None:
        if self.settings.deepseek_platform_transport == "http":
            self._validate_http_configuration()
            return
        if not self.settings.deepseek_platform_access_key.strip():
            raise ModelTransportError("DeepSeek Platform access key is not configured")
        if not self.settings.deepseek_platform_ws_url.strip():
            raise ModelTransportError("DeepSeek Platform WebSocket URL is not configured")

    def _validate_http_configuration(self) -> None:
        if not self.settings.deepseek_platform_http_api_key.strip():
            raise ModelTransportError("DeepSeek official HTTP API key is not configured")
        if not self.settings.deepseek_platform_http_url.strip():
            raise ModelTransportError("DeepSeek official HTTP URL is not configured")
        if not self.settings.deepseek_platform_http_model.strip():
            raise ModelTransportError("DeepSeek official HTTP model is not configured")

    def _build_headers(
            self,
            request_context: ModelRequestContext,
    ) -> dict[str, str]:
        return {
            "messageName": self.settings.deepseek_platform_message_name,
            "sender": self.settings.deepseek_platform_sender,
            "receiver": self.settings.deepseek_platform_receiver,
            "deviceId": request_context.device_id,
            "token": self._build_token(),
            "sessionId": request_context.session_id,
            "interactionId": request_context.interaction_id,
            "locate": request_context.country_code,
            "appVersion": request_context.app_version,
            "appName": request_context.app_name,
        }

    def _build_body(
            self,
            messages: list[dict[str, str]],
            request_context: ModelRequestContext,
    ) -> dict[str, Any]:
        message_name = self.settings.deepseek_platform_message_name
        sender = self.settings.deepseek_platform_sender
        receiver = self.settings.deepseek_platform_receiver
        copied_messages = [dict(message) for message in messages]
        return {
            "session": {
                "messageName": message_name,
                "sender": sender,
                "receiver": receiver,
                "deviceId": request_context.device_id,
                "sessionId": request_context.session_id,
                "interactionId": request_context.interaction_id,
            },
            "body": {
                "apiKey": self.settings.deepseek_platform_api_key,
                "modelName": self.settings.deepseek_platform_model_name,
                "modelParam": {},
                "extra_body": {
                    "enable_thinking": self.settings.deepseek_enable_thinking
                },
                "messages": copied_messages,
                "tools": None,
            },
        }

    def _build_token(self) -> str:
        timestamp = str(self._timestamp_provider())
        secret_key = self._load_secret_key()
        sign_source = f"{self.settings.deepseek_platform_access_key}{timestamp}"
        digest = hmac.new(
            secret_key,
            sign_source.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode("utf-8")
        return (
            f"{self.settings.deepseek_platform_access_key};"
            f"{timestamp};{signature};"
        )

    def _load_secret_key(self) -> bytes:
        config_key = self.settings.deepseek_platform_secret_key_sts_config_key
        try:
            encoded_secret = self._secret_loader(config_key)
            if isinstance(encoded_secret, str):
                encoded_secret = encoded_secret.encode("utf-8")
            secret_key = base64.b64decode(encoded_secret, validate=True)
            if not secret_key:
                raise ValueError("decoded secret key is empty")
            return secret_key
        except (KeyError, ValueError) as exc:
            logger.error(
                f"{_MODULE} secret_key_load_failed config_key={config_key} "
                f"exception_type={type(exc).__name__}"
            )
            raise ModelTransportError(
                f"DeepSeek Platform secret key is unavailable: {config_key}"
            ) from exc

    def _process_message(
            self,
            message: str | bytes,
            partial_texts: list[str],
            model_metrics: dict[str, Any] | None = None,
    ) -> str | None:
        try:
            data = json.loads(message)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            logger.warning(
                f"{_MODULE} response_json_ignored exception_type={type(exc).__name__}"
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                f"{_MODULE} response_shape_ignored "
                f"response_type={type(data).__name__}"
            )
            return None
        self._raise_for_platform_error(data, partial_texts)
        result = data.get("result")
        if not isinstance(result, dict):
            return None
        result_type = result.get("type")
        text = result.get("text")
        if result_type == "partialText" and isinstance(text, str) and text:
            partial_texts.append(text)
            return None
        if result_type != "finalText":
            return None

        model_info = data.get("modelRequestInfo")
        if isinstance(model_info, dict):
            self._extract_model_metrics(model_info, model_metrics)

        if not isinstance(text, str) or not text.strip():
            raise ModelTransportError(
                "DeepSeek Platform returned empty finalText",
                code="MODEL_EMPTY_OUTPUT",
                partial_output="".join(partial_texts),
            )
        return text

    @staticmethod
    def _extract_model_metrics(
            model_info: dict[str, Any],
            model_metrics: dict[str, Any] | None,
    ) -> None:
        """从 modelRequestInfo.contentBean 提取模型指标。"""
        content_bean = model_info.get("contentBean")
        if not isinstance(content_bean, dict):
            return
        for key in (
                "inputTokenNum",
                "generateTokenNum",
                "firstCostTime",
                "modelTime",
                "perTokenLantency",
                "contextTokenLantency",
                "prefixLen",
                "prefixHitRate",
                "meanAcceptTokens",
        ):
            if key in content_bean:
                model_metrics[key] = content_bean[key]

    @staticmethod
    def _raise_for_platform_error(
            data: dict[str, Any],
            partial_texts: list[str],
    ) -> None:
        result = data.get("result")
        result_data = result if isinstance(result, dict) else {}
        error_code = data.get("errorCode") or result_data.get("errorCode")
        has_error_code = error_code not in {None, "", 0, "0"}
        result_type = str(result_data.get("type", "")).casefold()
        has_error_type = result_type in {"error", "failed", "failure"}
        if not has_error_code and not has_error_type:
            return
        error_message = (
                data.get("errorMsg")
                or data.get("errorMessage")
                or result_data.get("errorMsg")
                or result_data.get("text")
                or "unknown platform error"
        )
        raise ModelTransportError(
            f"DeepSeek Platform returned error: code={error_code}, message={error_message}",
            code=str(error_code or result_type),
            partial_output="".join(partial_texts),
        )

    @staticmethod
    def _current_timestamp_ms() -> int:
        return int(time.time() * 1000)
