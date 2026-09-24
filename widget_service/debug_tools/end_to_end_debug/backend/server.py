"""端到端调试平台后端与工具 WebSocket BFF。

平台只负责调试会话和透明转发，不负责启动或回收 8855 工具服务。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse


def _discover_roots() -> tuple[Path, Path]:
    """定位 widget_service 项目根和外层仓库根，避免依赖固定 parents 层级。"""

    project_root: Path | None = None
    repository_root: Path | None = None
    for candidate in Path(__file__).resolve().parents:
        if project_root is None and (candidate / "cloud").is_dir() and (
            candidate / "pyproject.toml"
        ).is_file():
            project_root = candidate
        if repository_root is None and (candidate / "widget_service").is_dir() and (
            candidate / "skills"
        ).is_dir():
            repository_root = candidate
    if project_root is None:
        project_root = Path(__file__).resolve().parents[3]
    if repository_root is None:
        repository_root = project_root.parent
    return project_root, repository_root


_PROJECT_ROOT, _REPO_ROOT = _discover_roots()
_CLOUD_ROOT = str(_PROJECT_ROOT / "cloud")
if _CLOUD_ROOT not in sys.path:
    sys.path.insert(0, _CLOUD_ROOT)

if TYPE_CHECKING:
    from .debug_agent.config import DebugSettings

_ALLOWED_OPERATIONS = frozenset(
    {
        "getWidgetCapabilityOverview",
        "getDataCapabilitySchemas",
        "generateWidgetCardCompactDsl",
    }
)


def create_app(
    debug_settings: DebugSettings | None = None,
    *,
    production_settings: Any | None = None,
    model_client_factory: Any | None = None,
    upstream_client: Any | None = None,
) -> FastAPI:
    """创建工作台 FastAPI 应用。

    ``production_settings`` 和客户端工厂参数用于单元测试及本地调试注入；生产启动时
    会从云侧配置读取模型设置，但不会启动上游工具服务。
    """

    production = production_settings or _load_production_settings()
    local = debug_settings or _load_debug_settings(production)
    config_path = (
        _PROJECT_ROOT / "debug_tools" / "end_to_end_debug" / "backend" / "debug_agent.yaml"
    )
    if not config_path.is_file():
        raise RuntimeError(f"debug config does not exist: {config_path}")

    from .debug_agent.config import load_debug_config

    debug_config = load_debug_config(config_path, _REPO_ROOT)
    app = FastAPI(title="AI Widget Debug Platform", version="0.1.0")
    static_dir = _PROJECT_ROOT / "debug_tools" / "end_to_end_debug" / "backend" / "static"

    @app.get("/debug/health")
    async def health() -> dict[str, Any]:
        provider = str(getattr(production, "openai_master_client", "") or "")
        model_key = (
            "deepseek_platform_model_name" if provider == "deepseek_platform" else "deepseek_model"
        )
        upstream_reachable = await _probe_upstream(local.upstream_base_url)
        return {
            "status": "ok",
            "version": app.version,
            "debugHost": local.host,
            "debugPort": local.port,
            "upstream": local.upstream_base_url,
            "upstreamReachable": upstream_reachable,
            "upstreamStatus": "available" if upstream_reachable else "unavailable",
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

    @app.get("/debug/skills")
    async def skills() -> dict[str, Any]:
        return {
            "profiles": debug_config.available_profiles(),
            "selected": local.skill_profile,
            "quickPrompts": [
                {"label": item.label, "prompt": item.prompt} for item in local.quick_prompts
            ],
        }

    @app.websocket("/debug/e2e/ws")
    async def e2e_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        await _run_e2e_session(
            websocket,
            local,
            production,
            debug_config,
            model_client_factory=model_client_factory,
            upstream_client=upstream_client,
        )

    @app.websocket("/debug/tools/{operation}")
    async def tool_proxy(websocket: WebSocket, operation: str) -> None:
        await websocket.accept()
        if operation not in _ALLOWED_OPERATIONS:
            await websocket.close(code=1008, reason="unsupported operation")
            return
        await _proxy_tool_socket(websocket, local.upstream_base_url, operation)

    @app.get("/debug/")
    async def debug_index() -> Any:
        return _static_response(static_dir, "")

    @app.get("/debug/{asset_path:path}")
    async def debug_asset(asset_path: str) -> Any:
        """提供 SPA fallback，同时只允许读取构建目录内的文件。"""

        return _static_response(static_dir, asset_path)

    return app


async def _run_e2e_session(
    websocket: WebSocket,
    settings: DebugSettings,
    production: Any,
    debug_config: Any,
    *,
    model_client_factory: Any | None,
    upstream_client: Any | None,
) -> None:
    from .debug_agent.agent import DebugAgentSession
    from .debug_agent.config import DebugSettings
    from .debug_agent.schemas import (
        ConfigureFrame,
        DebugEvent,
        DeviceDebugContext,
        MessageFrame,
        SimpleFrame,
    )
    from .debug_agent.skill_loader import SkillLoadError

    sequence = 0
    session: DebugAgentSession | None = None

    async def send_event(event_type: str, data: dict[str, Any], run_id: str = "") -> None:
        nonlocal sequence
        sequence += 1
        if session is None:
            return
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
            settings,
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
            "skillName": settings.skill_name,
            "skillProfile": settings.skill_profile,
            "skillVersion": settings.skill_version,
            "skillProfiles": list(settings.available_profiles),
            "quickPrompts": [
                {"label": item.label, "prompt": item.prompt} for item in settings.quick_prompts
            ],
            "systemPromptPath": str(settings.system_prompt_path or ""),
            "bundleName": settings.bundle_name,
            "context": DeviceDebugContext(
                uid=settings.default_uid,
                odid=settings.default_device_id,
                deviceId=settings.default_device_id,
                phoneType=settings.default_phone_type,
                appVersion=settings.default_app_version,
                romVersion=settings.default_rom_version,
                locale=settings.default_locale,
                countryCode=settings.default_country_code,
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
                await send_event("diagnostic", {"kind": "invalid_frame", "message": "帧类型无效"})
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
                except (TypeError, ValueError) as exc:
                    await send_event("diagnostic", {"kind": "invalid_config", "message": str(exc)})
                    continue
                await send_event(
                    "diagnostic",
                    {"kind": "configured", "context": frame.context.model_dump(mode="json")},
                )
                continue
            if frame_type == "message":
                if running_task is not None and not running_task.done():
                    await send_event("diagnostic", {"kind": "busy", "message": "已有运行中的任务"})
                    continue
                try:
                    message = MessageFrame.model_validate(payload)
                except (TypeError, ValueError) as exc:
                    await send_event("diagnostic", {"kind": "invalid_message", "message": str(exc)})
                    continue
                running_task = asyncio.create_task(session.run(message.content))
                continue
            if frame_type == "cancel":
                try:
                    SimpleFrame.model_validate(payload)
                except (TypeError, ValueError) as exc:
                    await send_event("diagnostic", {"kind": "invalid_cancel", "message": str(exc)})
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
                    if reset_frame.profile:
                        selected = DebugSettings.from_config(
                            debug_config,
                            production,
                            profile=reset_frame.profile,
                            log_level=settings.log_level,
                            trace=settings.log_trace,
                            port=settings.port,
                        )
                        settings = selected
                        session = DebugAgentSession(
                            settings,
                            production_settings=production,
                            event_sink=send_event,
                            model_client_factory=model_client_factory,
                            upstream_client=upstream_client,
                        )
                    else:
                        session.reset()
                except (TypeError, ValueError) as exc:
                    await send_event("diagnostic", {"kind": "invalid_reset", "message": str(exc)})
                    continue
                sequence = 0
                await send_event(
                    "session_reset",
                    {
                        "skillName": settings.skill_name,
                        "skillProfile": settings.skill_profile,
                        "skillProfiles": list(settings.available_profiles),
                        "quickPrompts": [
                            {"label": item.label, "prompt": item.prompt}
                            for item in settings.quick_prompts
                        ],
                    },
                )
                continue
            await send_event("diagnostic", {"kind": "unknown_frame", "message": frame_type})
    except WebSocketDisconnect:
        return
    finally:
        if running_task is not None and not running_task.done():
            running_task.cancel()
            with suppress(asyncio.CancelledError):
                await running_task


async def _proxy_tool_socket(websocket: WebSocket, base_url: str, operation: str) -> None:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "wss" else "ws"
    host = parsed.netloc
    upstream_uri = f"{scheme}://{host}/api/v1/ws/tools/{operation}"
    try:
        async with websockets.connect(upstream_uri, open_timeout=10, close_timeout=2) as upstream:
            client_to_upstream = asyncio.create_task(_forward_client(websocket, upstream))
            upstream_to_client = asyncio.create_task(_forward_upstream(upstream, websocket))
            done, pending = await asyncio.wait(
                (client_to_upstream, upstream_to_client),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.result()
                except websockets.ConnectionClosed as exc:
                    with suppress(RuntimeError, WebSocketDisconnect):
                        await websocket.close(
                            code=exc.code or 1011,
                            reason=exc.reason or "upstream closed",
                        )
                except (asyncio.CancelledError, WebSocketDisconnect):
                    return
    except websockets.ConnectionClosed as exc:
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=exc.code or 1011, reason=exc.reason or "upstream closed")
    except (OSError, websockets.WebSocketException, WebSocketDisconnect):
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=1011, reason="upstream unavailable")


async def _forward_client(websocket: WebSocket, upstream: Any) -> None:
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return
        text = message.get("text")
        if text is not None:
            await upstream.send(text)
            continue
        data = message.get("bytes")
        if data is not None:
            await upstream.send(data)


async def _forward_upstream(upstream: Any, websocket: WebSocket) -> None:
    while True:
        message = await upstream.recv()
        if isinstance(message, str):
            await websocket.send_text(message)
        else:
            await websocket.send_bytes(message)


def _load_production_settings() -> Any:
    _ensure_cloud_import_path()
    from config.config import get_settings

    return get_settings()


def _load_debug_settings(production: Any) -> DebugSettings:
    from .debug_agent.config import DebugSettings, load_debug_config

    config_path = (
        _PROJECT_ROOT / "debug_tools" / "end_to_end_debug" / "backend" / "debug_agent.yaml"
    )
    if not config_path.is_file():
        raise RuntimeError(f"debug config does not exist: {config_path}")
    settings = DebugSettings.from_config(
        load_debug_config(config_path, _REPO_ROOT),
        production,
        profile=os.getenv("DEBUG_AGENT_PROFILE"),
        port=int(os.getenv("DEBUG_AGENT_PORT", "8888")),
        log_level=os.getenv("DEBUG_AGENT_LOG_LEVEL"),
        trace=True,
    )
    upstream = os.getenv("DEBUG_AGENT_UPSTREAM_URL")
    if upstream:
        from dataclasses import replace

        settings = replace(settings, upstream_base_url=upstream)
    return settings


def _ensure_cloud_import_path() -> None:
    if _CLOUD_ROOT not in sys.path:
        sys.path.insert(0, _CLOUD_ROOT)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _static_response(static_dir: Path, asset_path: str) -> Any:
    """返回静态资源或 index.html；构建目录不存在时给出可读提示。"""

    if not static_dir.is_dir():
        return JSONResponse({"status": "ok", "message": "debug platform assets are not built"})
    normalized = Path(asset_path)
    if asset_path and (normalized.is_absolute() or ".." in normalized.parts):
        return JSONResponse({"detail": "invalid asset path"}, status_code=400)
    candidate = (static_dir / normalized).resolve() if asset_path else static_dir / "index.html"
    try:
        candidate.relative_to(static_dir.resolve())
    except ValueError:
        return JSONResponse({"detail": "invalid asset path"}, status_code=400)
    if candidate.is_file():
        return FileResponse(candidate)
    index_path = static_dir / "index.html"
    if index_path.is_file():
        return FileResponse(index_path)
    return JSONResponse(
        {"status": "ok", "message": "debug platform index is missing"},
        status_code=503,
    )


async def _probe_upstream(base_url: str) -> bool:
    parsed = urlsplit(base_url)
    if not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(parsed.hostname, port),
            timeout=0.25,
        )
    except (OSError, TimeoutError, ValueError):
        return False
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
    del reader
    return True


app = create_app() if __name__ != "__main__" else None


if __name__ == "__main__":
    import uvicorn

    _ensure_cloud_import_path()
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8888, log_config=None)
