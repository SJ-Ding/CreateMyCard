from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent import DebugAgentSession
from .config import DebugSettings, load_debug_config
from .schemas import ConfigureFrame, DebugEvent, DeviceDebugContext, MessageFrame, SimpleFrame
from .skill_loader import SkillLoadError

if TYPE_CHECKING:
    from config.config import Settings


def create_app(
    debug_settings: DebugSettings | None = None,
    *,
    production_settings: Settings | None = None,
    model_client_factory: Any | None = None,
    upstream_client: Any | None = None,
) -> FastAPI:
    """创建独立 Debug FastAPI 应用，不导入正式路由。"""
    if production_settings is None:
        from config.config import get_settings

        production = get_settings()
    else:
        production = production_settings
    local = debug_settings or DebugSettings.from_settings(production)
    config_path = Path(production.repo_root) / "cloud" / "debug_agent.yaml"
    if not config_path.is_file():
        config_path = Path(production.repo_root) / "widget_service" / "cloud" / "debug_agent.yaml"
    debug_config = load_debug_config(config_path, production.repo_root)
    provider = str(getattr(production, "openai_master_client", "") or "")
    allowed_providers = {"deepseek_platform", "llmclient"}
    for role in ("openai_master_client", "openai_fallback_client"):
        configured_provider = str(getattr(production, role, "") or "")
        if configured_provider and configured_provider not in allowed_providers:
            raise ValueError(f"Debug 服务不支持当前模型 Provider: {configured_provider}")
    app = FastAPI(title="Main Agent Debug Service", version="0.1.0")
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="debug-static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        model_key = (
            "deepseek_platform_model_name" if provider == "deepseek_platform" else "deepseek_model"
        )
        return {
            "status": "ok",
            "debugHost": local.host,
            "debugPort": local.port,
            "upstream": local.upstream_base_url,
            "provider": provider or "未配置",
            "model": str(getattr(production, model_key, "") or "未配置"),
            "enableArtifactDownloadMock": bool(
                getattr(production, "enable_artifact_download_mock", False)
            ),
            "enableWidgetEdit": bool(getattr(production, "enable_widget_edit", False)),
            "skillProfile": local.skill_profile,
            "skillName": local.skill_name,
            "skillProfiles": list(local.available_profiles),
        }

    @app.get("/api/v1/skills")
    async def skills() -> dict[str, Any]:
        return {"profiles": debug_config.available_profiles(), "selected": local.skill_profile}

    @app.websocket("/api/v1/ws/main-agent")
    async def main_agent_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        session_settings = local
        sequence = 0

        async def send_event(event_type: str, data: dict[str, Any], run_id: str = "") -> None:
            nonlocal sequence
            sequence += 1
            event = DebugEvent(
                type=event_type,
                sequence=sequence,
                sessionId=session.session_id,
                runId=run_id,
                timestamp=_timestamp(),
                data=data,
            )
            try:
                await websocket.send_json(event.model_dump(mode="json"))
            except (RuntimeError, WebSocketDisconnect):
                return

        try:
            session = DebugAgentSession(
                session_settings,
                production_settings=production,
                event_sink=send_event,
                model_client_factory=model_client_factory,
                upstream_client=upstream_client,
            )
        except (ValueError, SkillLoadError) as exc:
            await websocket.send_json({"type": "diagnostic", "error": str(exc)})
            await websocket.close(code=1011)
            return
        await send_event(
            "session_started",
            {
                "skillName": session_settings.skill_name,
                "skillProfile": session_settings.skill_profile,
                "skillVersion": session_settings.skill_version,
                "skillProfiles": list(session_settings.available_profiles),
                "quickPrompts": [
                    {"label": item.label, "prompt": item.prompt}
                    for item in session_settings.quick_prompts
                ],
                "systemPromptPath": str(session_settings.system_prompt_path or ""),
                "bundleName": session_settings.bundle_name,
                "context": DeviceDebugContext(
                    uid=session_settings.default_uid,
                    odid=session_settings.default_device_id,
                    deviceId=session_settings.default_device_id,
                    phoneType=session_settings.default_phone_type,
                    appVersion=session_settings.default_app_version,
                    romVersion=session_settings.default_rom_version,
                    locale=session_settings.default_locale,
                    countryCode=session_settings.default_country_code,
                ).model_dump(mode="json"),
            },
        )
        running_task: asyncio.Task[None] | None = None
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await send_event(
                        "diagnostic", {"kind": "invalid_frame", "message": "帧必须是合法 JSON"}
                    )
                    continue
                if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
                    await send_event(
                        "diagnostic", {"kind": "invalid_frame", "message": "帧类型无效"}
                    )
                    continue
                frame_type = payload["type"]
                if frame_type == "configure":
                    if running_task is not None and not running_task.done():
                        await send_event(
                            "diagnostic", {"kind": "busy", "message": "运行中不能修改配置"}
                        )
                        continue
                    try:
                        frame = ConfigureFrame.model_validate(payload)
                        session.configure(frame.context)
                    except (ValueError, TypeError) as exc:
                        await send_event(
                            "diagnostic", {"kind": "invalid_config", "message": str(exc)}
                        )
                        continue
                    await send_event(
                        "diagnostic",
                        {"kind": "configured", "context": frame.context.model_dump(mode="json")},
                    )
                    continue
                if frame_type == "message":
                    if running_task is not None and not running_task.done():
                        await send_event(
                            "diagnostic", {"kind": "busy", "message": "已有运行中的任务"}
                        )
                        continue
                    try:
                        message = MessageFrame.model_validate(payload)
                    except (ValueError, TypeError) as exc:
                        await send_event(
                            "diagnostic", {"kind": "invalid_message", "message": str(exc)}
                        )
                        continue
                    running_task = asyncio.create_task(session.run(message.content))
                    continue
                if frame_type == "cancel":
                    try:
                        SimpleFrame.model_validate(payload)
                    except (ValueError, TypeError) as exc:
                        await send_event(
                            "diagnostic", {"kind": "invalid_cancel", "message": str(exc)}
                        )
                        continue
                    if running_task is not None and not running_task.done():
                        await session.cancel()
                        running_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await running_task
                    continue
                if frame_type == "reset":
                    try:
                        reset_frame = SimpleFrame.model_validate(payload)
                        selected = session_settings
                        if reset_frame.profile:
                            selected = DebugSettings.from_config(
                                debug_config,
                                production,
                                profile=reset_frame.profile,
                                log_level=session_settings.log_level,
                                trace=session_settings.log_trace,
                            )
                            session_settings = selected
                            session = DebugAgentSession(
                                session_settings,
                                production_settings=production,
                                event_sink=send_event,
                                model_client_factory=model_client_factory,
                                upstream_client=upstream_client,
                            )
                        else:
                            session.reset()
                    except (ValueError, TypeError) as exc:
                        await send_event(
                            "diagnostic", {"kind": "invalid_reset", "message": str(exc)}
                        )
                        continue
                    sequence = 0
                    await send_event(
                        "session_reset",
                        {
                            "skillName": session_settings.skill_name,
                            "skillProfile": session_settings.skill_profile,
                            "skillProfiles": list(session_settings.available_profiles),
                            "quickPrompts": [
                                {"label": item.label, "prompt": item.prompt}
                                for item in session_settings.quick_prompts
                            ],
                        },
                    )
                    continue
                await send_event("diagnostic", {"kind": "unknown_frame", "message": frame_type})
        except WebSocketDisconnect:
            if running_task is not None and not running_task.done():
                running_task.cancel()
                with suppress(asyncio.CancelledError):
                    await running_task
        finally:
            if running_task is not None and not running_task.done():
                running_task.cancel()
                with suppress(asyncio.CancelledError):
                    await running_task

    return app


def _timestamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
