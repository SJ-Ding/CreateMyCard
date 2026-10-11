"""浏览器设备调试接口。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .commands import DeviceError
from .config import Config
from .conversion import convert
from .devices import Devices
from .jobs import Jobs
from .profiles import DeviceProfileStore


class ConvertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=2_000_000)
    format: Literal["auto", "a2ui", "compact"] = "auto"
    size: Literal["2x2", "2x4"] = "2x2"


class RunRequest(ConvertRequest):
    targetId: str = Field(min_length=1, max_length=64)


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    name: str
    serial: str
    deviceName: str
    kind: Literal["physical", "emulator", "unknown"] = "unknown"
    emulatorId: str | None = None
    resolution: dict[str, int]
    crops: dict[str, dict[str, int]] = Field(default_factory=dict)
    renderWaitSeconds: float = 8.0
    version: int | None = None
    updatedAt: str | None = None


def register_device_routes(
    app: FastAPI,
    config: Config | None = None,
    profile_store: DeviceProfileStore | None = None,
) -> Jobs:
    config = config or Config.load()
    devices = Devices(config)
    jobs = Jobs(config, devices)
    profiles = profile_store or DeviceProfileStore()
    app.state.device_profiles = profiles
    app.state.device_jobs = jobs
    app.router.add_event_handler("startup", jobs.start)
    app.router.add_event_handler("shutdown", jobs.close)
    app.router.add_event_handler("shutdown", devices.close)

    @app.exception_handler(DeviceError)
    async def device_error(_request, exc: DeviceError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/debug/device/environment")
    async def environment() -> dict:
        checks = await asyncio.to_thread(config.checks)
        targets_value = list(devices.targets.values())
        discovery_error = ""
        emulators_value = devices.emulators()
        physical_count = sum(1 for target in targets_value if target.kind == "physical")
        emulator_count = sum(1 for target in targets_value if target.kind == "emulator")
        unknown_count = sum(1 for target in targets_value if target.kind == "unknown")
        startable_count = sum(1 for emulator in emulators_value if emulator.ready)
        return {
            "checks": checks,
            "status": config.status_items(
                checks,
                physical_count,
                emulator_count,
                unknown_count,
                startable_count,
                discovery_error,
            ),
        }

    @app.get("/debug/device/targets")
    async def targets() -> dict:
        return {
            "targets": [asdict(target) for target in await devices.refresh()],
            "emulators": [asdict(emulator) for emulator in devices.emulators()],
        }

    @app.get("/debug/device/profiles")
    async def list_profiles() -> dict:
        return {"profiles": await asyncio.to_thread(profiles.list)}

    @app.get("/debug/device/profiles/{profile_id}")
    async def get_profile(profile_id: str) -> dict:
        return await asyncio.to_thread(profiles.get, profile_id)

    @app.post("/debug/device/profiles", status_code=201)
    async def create_profile(request: ProfileRequest) -> dict:
        value: dict[str, Any] = request.model_dump(exclude_none=True)
        return await asyncio.to_thread(profiles.create, value)

    @app.put("/debug/device/profiles/{profile_id}")
    async def update_profile(profile_id: str, request: ProfileRequest) -> dict:
        value: dict[str, Any] = request.model_dump(exclude_none=True)
        return await asyncio.to_thread(profiles.update, profile_id, value)

    @app.post("/debug/device/emulators/{emulator_id}/start", status_code=202)
    async def start_emulator(emulator_id: str) -> dict:
        return devices.start(emulator_id)

    @app.get("/debug/device/operations/{operation_id}")
    async def operation(operation_id: str) -> dict:
        result = devices.operations.get(operation_id)
        if result is None:
            raise DeviceError("启动任务不存在", 404)
        return result

    @app.post("/debug/device/convert")
    async def conversion(request: ConvertRequest) -> dict:
        try:
            result = await asyncio.to_thread(convert, request.source, request.format, request.size)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"format": result.format, "text": result.text, "messageCount": len(result.messages)}

    @app.post("/debug/device/runs", status_code=202)
    async def submit(request: RunRequest) -> dict:
        try:
            return await jobs.submit(request.targetId, request.source, request.format, request.size)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/debug/device/runs/{job_id}")
    async def get_job(job_id: str) -> dict:
        return jobs.get(job_id)

    @app.get("/debug/device/runs/{job_id}/logs")
    async def logs(job_id: str, offset: int = Query(default=0, ge=0)) -> dict:
        return jobs.logs(job_id, offset)

    @app.post("/debug/device/runs/{job_id}/cancel")
    async def cancel(job_id: str) -> dict:
        return await jobs.cancel(job_id)

    @app.websocket("/debug/device/targets/{target_id}/screen")
    async def screen(websocket: WebSocket, target_id: str) -> None:
        await websocket.accept()
        try:
            target = await devices.target(target_id)
        except (DeviceError, OSError, TimeoutError) as exc:
            await websocket.send_json({"status": "error", "message": str(exc)})
            await websocket.close()
            return
        collector = devices.screen(target)
        queue = collector.subscribe()

        async def send() -> None:
            while True:
                info, data = await queue.get()
                await websocket.send_json(info)
                if data is not None:
                    await websocket.send_bytes(data)

        async def receive() -> None:
            while True:
                await websocket.receive_text()

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, RuntimeError, OSError):
            # WebSocket 关闭是正常生命周期事件，由 finally 回收唯一订阅。
            return
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await collector.unsubscribe(queue)

    return jobs
