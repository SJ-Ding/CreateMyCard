"""批跑 REST API，供调试平台 frontend/full 两种模式共用。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .runner import BatchRunManager


class StartBatchRequest(BaseModel):
    sampleIds: list[str] = Field(min_length=1)
    toolWsBaseUrl: str = Field(min_length=1)
    concurrency: int = Field(default=4, ge=1, le=16)
    maxRetries: int = Field(default=1, ge=0, le=5)


def _default_paths() -> tuple[Path, Path, Path, Path]:
    debug_root = Path(__file__).resolve().parents[1]
    project_root = debug_root.parent
    cloud_root = project_root / "cloud"
    configured_trace_root = Path(
        os.getenv("WIDGET_SERVICE_GENERATION_TRACE_ROOT", "workspace/traces")
    )
    trace_root = (
        configured_trace_root
        if configured_trace_root.is_absolute()
        else cloud_root / configured_trace_root
    )
    return (
        debug_root / "test_datas" / "request_dataset",
        debug_root / "batch_output",
        trace_root,
        cloud_root,
    )


def register_batch_routes(
    app: FastAPI,
    manager: BatchRunManager | None = None,
) -> BatchRunManager:
    """把本地批跑 API 注册到指定 FastAPI 应用。"""

    if manager is None:
        dataset_root, output_root, trace_root, cloud_root = _default_paths()
        manager = BatchRunManager(dataset_root, output_root, trace_root, cloud_root)
    app.state.batch_run_manager = manager

    @app.get("/debug/batch/datasets")
    async def batch_datasets() -> dict[str, Any]:
        return {"items": manager.datasets()}

    @app.get("/debug/batch/runs")
    async def batch_runs() -> dict[str, Any]:
        return {"items": manager.list_runs()}

    @app.post("/debug/batch/runs", status_code=202)
    async def start_batch(request: StartBatchRequest) -> dict[str, Any]:
        try:
            return manager.start_run(
                request.sampleIds,
                request.toolWsBaseUrl,
                request.concurrency,
                request.maxRetries,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/debug/batch/runs/{run_id}")
    async def batch_run(run_id: str) -> dict[str, Any]:
        try:
            return manager.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="批跑记录不存在") from exc

    @app.get("/debug/batch/runs/{run_id}/samples/{sample_id}")
    async def batch_sample(run_id: str, sample_id: str) -> dict[str, Any]:
        try:
            return manager.get_sample(run_id, sample_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="批跑样本结果不存在") from exc

    @app.post("/debug/batch/runs/{run_id}/cancel")
    async def cancel_batch(run_id: str) -> dict[str, Any]:
        try:
            return await manager.cancel_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="活动批跑不存在") from exc

    return manager
