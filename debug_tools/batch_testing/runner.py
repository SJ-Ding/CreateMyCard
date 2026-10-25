"""DSL 批跑执行器与持久化状态管理。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import websockets

from debug_tools.dataset_metadata import read_version
from debug_tools.end_to_end_debug.backend.debug_agent.upstream_ws_client import (
    UpstreamToolError,
    parse_legacy_stream_content,
)
from debug_tools.file_io import atomic_write_text

from .artifacts import (
    download_artifact,
    find_local_artifact,
    parse_artifact_blocks,
    write_artifact_blocks,
)
from .collection import CollectionPool
from .metrics import enrich_run_metrics
from .read_models import evidence_signature
from .state_store import DATABASE_NAME, RunProgress, RunStateStore
from .trace_parser import TraceImporter

_OPERATION = "generateWidgetCardCompactDsl"
_logger = logging.getLogger(__name__)
_SAMPLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_TRACE_DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_DETERMINISTIC_ERROR_CODES = frozenset(
    {
        "INVALID_ARGUMENTS",
        "APP_VERSION_UNSUPPORTED",
        "PROTOCOL_CAPABILITY_UNSUPPORTED",
    }
)
InvokeCallable = Callable[[str, dict[str, Any], float], Awaitable[dict[str, Any]]]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def normalize_endpoint(base_url: str) -> str:
    configured = base_url.strip()
    parsed = urlsplit(configured)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("微服务地址必须是完整的 ws:// 或 wss:// URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("微服务地址不能包含认证信息或 fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith(f"/{_OPERATION}"):
        path = f"{path}/{_OPERATION}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


async def invoke_generate(
    endpoint: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """发送完整工具包络并等待、校验 final 帧。"""

    session = payload.get("session")
    if not isinstance(session, dict):
        raise ValueError("请求缺少 session")
    session_id = session.get("sessionId")
    interaction_id = session.get("interactionId")
    request_id = f"{session_id}&{interaction_id}"
    frames: list[dict[str, Any]] = []
    try:
        async with websockets.connect(
            endpoint,
            open_timeout=15,
            close_timeout=5,
            ping_interval=None,
            ping_timeout=None,
        ) as websocket:
            await websocket.send(json.dumps(payload, ensure_ascii=False))
            while True:
                raw = await asyncio.wait_for(websocket.recv(), timeout_seconds)
                if not isinstance(raw, str):
                    raise UpstreamToolError("正式工具返回非文本 WebSocket 帧")
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise UpstreamToolError("正式工具返回帧不是对象")
                frames.append(frame)
                outer_error = frame.get("errorCode")
                if outer_error not in (None, "", "0", 0):
                    raise UpstreamToolError(f"正式工具外层错误: {outer_error}")
                reply = frame.get("reply")
                stream_info = reply.get("streamInfo") if isinstance(reply, dict) else None
                if not isinstance(stream_info, dict):
                    raise UpstreamToolError("正式工具帧缺少 streamInfo")
                stream_type = stream_info.get("streamType")
                if stream_type in {"start", "partial", "command"}:
                    continue
                if stream_type not in {"final", "final_error"}:
                    raise UpstreamToolError("正式工具返回未知流类型")
                content = stream_info.get("streamContent")
                if not isinstance(content, str) or not content:
                    raise UpstreamToolError("正式工具 final 缺少 streamContent")
                parsed_content = parse_legacy_stream_content(content)
                if parsed_content.get("requestId") != request_id:
                    raise UpstreamToolError("正式工具 requestId 与当前请求不匹配")
                if parsed_content.get("operation") != _OPERATION:
                    raise UpstreamToolError("正式工具 operation 与当前请求不匹配")
                data = parsed_content.get("data")
                return {
                    "requestId": request_id,
                    "status": str(parsed_content.get("status") or "failed"),
                    "errorCode": str(parsed_content.get("errorCode") or ""),
                    "error": parsed_content.get("error"),
                    "data": data if isinstance(data, dict) else {},
                    "frames": frames,
                }
    except TimeoutError as exc:
        raise UpstreamToolError("正式工具 WebSocket 调用超时") from exc
    except json.JSONDecodeError as exc:
        raise UpstreamToolError("正式工具返回非法 JSON") from exc
    except UpstreamToolError as exc:
        exc.frames = tuple(frames)
        raise
    except (OSError, websockets.WebSocketException) as exc:
        raise UpstreamToolError(f"正式工具 WebSocket 调用失败: {type(exc).__name__}") from exc


@dataclass(frozen=True)
class DatasetItem:
    sample_id: str
    file_name: str
    path: Path
    title: str
    query: str
    size: str
    valid: bool
    error: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.sample_id,
            "fileName": self.file_name,
            "title": self.title,
            "query": self.query,
            "size": self.size,
            "valid": self.valid,
            "error": self.error,
        }


def discover_datasets(dataset_root: Path) -> list[DatasetItem]:
    items: list[DatasetItem] = []
    if not dataset_root.is_dir():
        return items
    for path in sorted(dataset_root.glob("*.json"), key=lambda item: item.name.lower()):
        if path.name == "version.json":
            continue
        sample_id = path.stem
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON 根节点必须是对象")
            content = payload.get("content")
            if not isinstance(content, dict):
                raise ValueError("请求缺少 content 对象")
            user_query = content.get("userQuery")
            if not isinstance(user_query, str) or not user_query.strip():
                raise ValueError("content.userQuery 必须是非空字符串")
            items.append(
                DatasetItem(
                    sample_id=sample_id,
                    file_name=path.name,
                    path=path,
                    title=str(content.get("title") or sample_id),
                    query=user_query,
                    size=str(content.get("size") or ""),
                    valid=True,
                )
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            items.append(
                DatasetItem(
                    sample_id=sample_id,
                    file_name=path.name,
                    path=path,
                    title=sample_id,
                    query="",
                    size="",
                    valid=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return items


def prepare_request(
    source: dict[str, Any],
    *,
    uid: str,
    run_token: str,
    sample_id: str,
    attempt: int,
) -> dict[str, Any]:
    payload = copy.deepcopy(source)
    content = payload.get("content")
    if not isinstance(content, dict):
        raise ValueError("请求缺少 content 对象")
    content["uid"] = uid
    user_auth = payload.get("userAuth")
    if not isinstance(user_auth, dict):
        user_auth = {}
        payload["userAuth"] = user_auth
    user = user_auth.get("user")
    if not isinstance(user, dict):
        user = {}
        user_auth["user"] = user
    user["userId"] = uid
    session = payload.get("session")
    if not isinstance(session, dict):
        session = {}
        payload["session"] = session
    session["sessionId"] = f"batch-{run_token}"
    session["interactionId"] = f"{sample_id}-{attempt:03d}"
    session["isNew"] = attempt == 0
    return payload


def apply_request_overrides(
    source: dict[str, Any],
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """把任务级公共字段覆盖到请求副本，不改变样本源文件。"""

    payload = copy.deepcopy(source)
    content = payload.get("content")
    if not isinstance(content, dict):
        raise ValueError("请求缺少 content 对象")
    device_info = payload.get("deviceInfo")
    if not isinstance(device_info, dict):
        device_info = {}
        payload["deviceInfo"] = device_info
    pagination = payload.get("pagination")
    if not isinstance(pagination, dict):
        pagination = {}
        payload["pagination"] = pagination

    protocol_version = overrides.get("protocolVersion")
    if isinstance(protocol_version, str) and protocol_version:
        payload["version"] = protocol_version
    bundle_name = overrides.get("bundleName")
    if isinstance(bundle_name, str) and bundle_name:
        payload["bundleName"] = bundle_name
        content["bundleName"] = bundle_name
    device_id = overrides.get("deviceId")
    if isinstance(device_id, str) and device_id:
        content["odid"] = device_id
        device_info["deviceId"] = device_id

    device_fields = {
        "phoneType": "phoneType",
        "appVersion": "prdVer",
        "romVersion": "romVersion",
        "locale": "locale",
        "countryCode": "countryCode",
        "deviceFormation": "deviceFormation",
        "deviceType": "deviceType",
        "sysVer": "sysVer",
    }
    for source_name, target_name in device_fields.items():
        value = overrides.get(source_name)
        if value is not None and value != "":
            device_info[target_name] = value

    pagination_limit = overrides.get("paginationLimit")
    if isinstance(pagination_limit, int) and not isinstance(pagination_limit, bool):
        pagination["limit"] = pagination_limit
    pagination_start = overrides.get("paginationStart")
    if isinstance(pagination_start, str):
        pagination["start"] = pagination_start
    return payload


@dataclass
class BatchRunState:
    run_id: str
    run_token: str
    endpoint: str
    concurrency: int
    max_retries: int
    timeout_seconds: float
    samples: list[dict[str, Any]]
    output_dir: Path
    task_id: str = ""
    dataset_id: str = ""
    dataset_version: dict[str, Any] = field(default_factory=dict)
    backend_mode: str = "deployed"
    service_status: str = "external"
    request_overrides: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    workers: list[asyncio.Task[None]] = field(default_factory=list)
    progress: RunProgress | None = None
    request_slots: asyncio.Semaphore | None = None
    collector: CollectionPool | None = None
    error: str = ""
    sample_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    generation_metrics: dict[str, Any] = field(default_factory=dict)
    retry_parameters: dict[str, Any] = field(default_factory=dict)
    metrics_task: asyncio.Task[None] | None = None

    def public_dict(self) -> dict[str, Any]:
        total = len(self.samples)
        completed_states = {"success", "degraded", "failed", "unsupported", "cancelled"}
        completed = sum(1 for item in self.samples if item.get("status") in completed_states)
        success = sum(1 for item in self.samples if item.get("status") == "success")
        degraded = sum(1 for item in self.samples if item.get("status") == "degraded")
        failed = sum(1 for item in self.samples if item.get("status") in {"failed", "unsupported"})
        cancelled = sum(1 for item in self.samples if item.get("status") == "cancelled")
        elapsed_values = [
            float(item.get("elapsedMs", 0.0))
            for item in self.samples
            if item.get("status") in completed_states
        ]
        trace_warnings = sum(
            1
            for item in self.samples
            if item.get("traceStatus") in {"missing", "partial", "invalid"}
        )
        return {
            "runId": self.run_id,
            "status": self.status,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "endpoint": self.endpoint,
            "concurrency": self.concurrency,
            "maxRetries": self.max_retries,
            "timeoutSeconds": self.timeout_seconds,
            "taskId": self.task_id,
            "datasetId": self.dataset_id,
            "datasetVersion": self.dataset_version,
            "backendMode": self.backend_mode,
            "serviceStatus": self.service_status,
            "interrupted": self.status == "interrupted",
            "total": total,
            "completed": completed,
            "success": success,
            "degraded": degraded,
            "failed": failed,
            "cancelled": cancelled,
            "averageElapsedMs": (
                round(sum(elapsed_values) / len(elapsed_values), 2) if elapsed_values else 0.0
            ),
            "traceWarnings": trace_warnings,
            "samples": self.samples,
        }


class BatchRunManager:
    """管理内存中的活动批跑，并把可恢复结果写入 batch_output。"""

    def __init__(
        self,
        dataset_root: Path,
        output_root: Path,
        trace_root: Path,
        cloud_root: Path,
        *,
        invoke: InvokeCallable = invoke_generate,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.dataset_root = dataset_root.resolve()
        self.output_root = output_root.resolve()
        self.trace_importer = TraceImporter(trace_root)
        self.cloud_root = cloud_root.resolve()
        self.invoke = invoke
        self.timeout_seconds = timeout_seconds
        self.runs: dict[str, BatchRunState] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._trace_refresh_lock = RLock()
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="batch-state")
        self._files = ThreadPoolExecutor(max_workers=2, thread_name_prefix="batch-collection")
        self._metric_cache: dict[str, tuple[object, dict[str, Any]]] = {}
        self._metric_lock = RLock()
        self.configure_collection(128, 2)

    def configure_collection(self, capacity: int, concurrency: int, timeout: float = 60.0) -> None:
        self.collection_capacity = capacity
        self.collection_concurrency = concurrency
        self.collection_timeout_seconds = timeout
        self._artifact_slots = asyncio.Semaphore(concurrency)
        self._trace_slots = asyncio.Semaphore(concurrency)

    async def _write(self, function: Callable, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._writer, function, *args)

    async def _collect_io(self, function: Callable, *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._files, function, *args)

    async def close(self) -> None:
        await asyncio.to_thread(self._files.shutdown, wait=True)
        await asyncio.to_thread(self._writer.shutdown, wait=True)

    async def recover_collections(self) -> dict[str, str]:
        """恢复已确认响应；损坏运行独立失败，不阻止其它调试功能启动。"""
        failures: dict[str, str] = {}
        for database in self.output_root.glob(f"*/{DATABASE_NAME}"):
            try:
                await self._recover_collection_store(database)
            except Exception as exc:
                message = f"收集恢复失败: {type(exc).__name__}: {exc}"
                failures[database.parent.name] = message
                _logger.warning("批跑 %s %s", database.parent.name, message)
                try:
                    await self._write(
                        RunStateStore(database.parent).update,
                        {
                            "status": "failed",
                            "error": message,
                            "exportReady": False,
                            "recoveryState": "failed",
                        },
                    )
                except Exception as persist_error:
                    _logger.warning(
                        "恢复错误保存失败: %s: %s", type(persist_error).__name__, persist_error
                    )
        return failures

    async def _recover_collection_store(self, database: Path) -> None:
        store = RunStateStore(database.parent)
        summary = await self._write(store.overview)
        pending = await self._write(store.pending)
        terminal = summary.get("status") in {"completed", "cancelled", "failed", "interrupted"}
        if not pending and terminal and summary.get("exportReady"):
            return
        summary = await self._write(store.read)
        await self._write(store.update, {"recoveryState": "running"})
        raw_samples = summary.get("samples")
        if not isinstance(raw_samples, list):
            raise ValueError("恢复批跑时缺少样本状态")
        state = BatchRunState(
            run_id=database.parent.name,
            run_token=database.parent.name.rsplit("_", 1)[-1],
            endpoint=str(summary.get("endpoint") or ""),
            concurrency=int(summary.get("concurrency", 4)),
            max_retries=int(summary.get("maxRetries", 0)),
            timeout_seconds=float(summary.get("timeoutSeconds", self.timeout_seconds)),
            samples=raw_samples,
            output_dir=database.parent,
            task_id=str(summary.get("taskId") or ""),
            dataset_id=str(summary.get("datasetId") or ""),
            dataset_version=dict(summary.get("datasetVersion") or {}),
            backend_mode=str(summary.get("backendMode") or "deployed"),
            status="interrupted",
            started_at=str(summary.get("startedAt") or utc_now()),
        )
        for sample in state.samples:
            state.sample_index[str(sample.get("id"))] = sample
            if sample.get("status") in {"running", "queued"}:
                sample["status"] = "cancelled"
                sample["traceStatus"] = "missing"
        state.progress = RunProgress(state.samples)
        collector = CollectionPool(
            lambda collection_job, current=state: self._collect_attempt(current, collection_job),
            concurrency=self.collection_capacity,
            capacity=self.collection_capacity,
        )
        try:
            for job in pending:
                await collector.reserve()
                collector.submit(job)
        finally:
            try:
                await collector.close()
            finally:
                await self._finish_metrics(state)
        state.finished_at = utc_now()
        await self._write(self._persist_state, state)
        await self._write(store.update, {"recoveryState": "complete"})

    @staticmethod
    def _overview(state: BatchRunState) -> dict[str, Any]:
        if state.progress is None:
            state.progress = RunProgress(state.samples)
        return {
            "runId": state.run_id,
            "status": state.status,
            "startedAt": state.started_at,
            "finishedAt": state.finished_at,
            "endpoint": state.endpoint,
            "concurrency": state.concurrency,
            "maxRetries": state.max_retries,
            "timeoutSeconds": state.timeout_seconds,
            "taskId": state.task_id,
            "datasetId": state.dataset_id,
            "datasetVersion": state.dataset_version,
            "backendMode": state.backend_mode,
            "serviceStatus": state.service_status,
            "interrupted": state.status == "interrupted",
            "error": state.error,
            **state.progress.snapshot(),
            "generationMetrics": state.generation_metrics,
            "observedRetryParameters": state.retry_parameters,
            "collectionPending": state.collector.pending if state.collector is not None else 0,
            "collectionBackpressure": (
                state.collector.slots.locked() if state.collector is not None else False
            ),
        }

    async def _save_sample(
        self,
        state: BatchRunState,
        sample: dict[str, Any],
        job: dict[str, Any] | None = None,
    ) -> None:
        if state.progress is None:
            state.progress = RunProgress(state.samples)
        state.progress.replace(sample)
        saved = dict(sample)
        revision = await self._write(
            self._write_sample,
            state.output_dir,
            self._overview(state),
            saved,
            job,
        )
        sample["revision"] = revision

    @staticmethod
    def _write_sample(
        run_dir: Path,
        overview: dict[str, Any],
        sample: dict[str, Any],
        job: dict[str, Any] | None,
    ) -> int:
        revision = RunStateStore(run_dir).update(overview, sample, job)
        atomic_write_json(run_dir / str(sample.get("id")) / "result.json", sample)
        return revision

    def datasets(self) -> list[dict[str, Any]]:
        return [item.public_dict() for item in discover_datasets(self.dataset_root)]

    def start_run(
        self,
        sample_ids: list[str],
        tool_ws_base_url: str,
        concurrency: int,
        max_retries: int,
        *,
        task_id: str = "",
        dataset_id: str = "",
        dataset_root: Path | None = None,
        request_overrides: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        backend_mode: str = "deployed",
        service_status: str = "external",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_dataset_root = (dataset_root or self.dataset_root).resolve()
        available = {item.sample_id: item for item in discover_datasets(resolved_dataset_root)}
        selected: list[DatasetItem] = []
        seen: set[str] = set()
        for sample_id in sample_ids:
            if sample_id in seen:
                continue
            seen.add(sample_id)
            item = available.get(sample_id)
            if item is None or not _SAMPLE_ID_PATTERN.fullmatch(sample_id):
                raise ValueError(f"未知数据样本: {sample_id}")
            if not item.valid:
                raise ValueError(f"数据样本无效: {sample_id}: {item.error}")
            selected.append(item)
        if not selected:
            raise ValueError("至少选择一个有效数据样本")
        endpoint = normalize_endpoint(tool_ws_base_url)
        run_token = uuid.uuid4().hex[:8]
        if run_id is None:
            run_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{run_token}"
        else:
            run_token = run_id.rsplit("_", maxsplit=1)[-1]
        samples: list[dict[str, Any]] = []
        for sequence, item in enumerate(selected, start=1):
            samples.append(
                {
                    "id": item.sample_id,
                    "fileName": item.file_name,
                    "title": item.title,
                    "query": item.query,
                    "size": item.size,
                    "sequence": sequence,
                    "status": "queued",
                    "attemptCount": 0,
                    "elapsedMs": 0.0,
                    "errorCode": "",
                    "error": "",
                    "artifactAvailable": False,
                    "dslAvailable": False,
                    "traceStatus": "pending",
                    "traceRecordCount": 0,
                    "traceSchemaVersion": "",
                    "traceDurationMs": 0.0,
                }
            )
        state = BatchRunState(
            run_id=run_id,
            run_token=run_token,
            endpoint=endpoint,
            concurrency=concurrency,
            max_retries=max_retries,
            timeout_seconds=timeout_seconds or self.timeout_seconds,
            samples=samples,
            output_dir=self.output_root / run_id,
            task_id=task_id,
            dataset_id=dataset_id,
            dataset_version=read_version(resolved_dataset_root),
            backend_mode=backend_mode,
            service_status=service_status,
            request_overrides=dict(request_overrides or {}),
        )
        self.runs[run_id] = state
        self._persist_state(state)
        if state.dataset_version:
            atomic_write_json(state.output_dir / "dataset_version.json", state.dataset_version)
        self._snapshot_evaluation(resolved_dataset_root, state.output_dir)
        self.tasks[run_id] = asyncio.create_task(self._execute_run(state, available))
        return state.public_dict()

    def continue_run(
        self,
        run_id: str,
        tool_ws_base_url: str,
        *,
        dataset_root: Path,
        service_status: str,
        request_overrides: dict[str, Any],
    ) -> dict[str, Any]:
        summary = self._read_run_file(run_id, "summary.json")
        if summary.get("status") not in {"cancelled", "interrupted"}:
            raise ValueError("只有已停止或已中断的任务可以继续执行")
        samples = copy.deepcopy(summary.get("samples"))
        if not isinstance(samples, list):
            raise ValueError("执行记录缺少样本状态")
        max_retries = int(summary.get("maxRetries", 0))
        resumable = 0
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            status = str(sample.get("status") or "")
            attempt_count = int(sample.get("attemptCount") or 0)
            can_resume = status in {"queued", "running", "cancelled"}
            if can_resume and attempt_count <= max_retries:
                sample["status"] = "queued"
                resumable += 1
        if not resumable:
            raise ValueError("没有未执行或中断且仍在重试上限内的样本")
        resolved_dataset_root = dataset_root.resolve()
        available = {item.sample_id: item for item in discover_datasets(resolved_dataset_root)}
        state = BatchRunState(
            run_id=run_id,
            run_token=str(summary.get("runId") or run_id).rsplit("_", maxsplit=1)[-1],
            endpoint=normalize_endpoint(tool_ws_base_url),
            concurrency=int(summary.get("concurrency", 4)),
            max_retries=max_retries,
            timeout_seconds=float(summary.get("timeoutSeconds", self.timeout_seconds)),
            samples=samples,
            output_dir=self.output_root / run_id,
            task_id=str(summary.get("taskId") or ""),
            dataset_id=str(summary.get("datasetId") or ""),
            dataset_version=dict(summary.get("datasetVersion") or {}),
            backend_mode=str(summary.get("backendMode") or "deployed"),
            service_status=service_status,
            request_overrides=dict(request_overrides),
        )
        self.runs[run_id] = state
        self._persist_state(state)
        self.tasks[run_id] = asyncio.create_task(self._execute_run(state, available))
        return state.public_dict()

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(run_id)
        if state.status not in {"queued", "running"}:
            return state.public_dict()
        state.cancel_event.set()
        for worker in state.workers:
            if not worker.done():
                worker.cancel()
        return state.public_dict()

    def list_runs(self) -> list[dict[str, Any]]:
        persisted: dict[str, dict[str, Any]] = {}
        if self.output_root.is_dir():
            for path in self.output_root.glob("*/summary.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(value, dict) and isinstance(value.get("runId"), str):
                    persisted[value["runId"]] = value
        for run_id, state in self.runs.items():
            persisted[run_id] = state.public_dict()
        return sorted(
            persisted.values(),
            key=lambda item: str(item.get("startedAt", "")),
            reverse=True,
        )

    def get_run(self, run_id: str, *, include_metrics: bool = True) -> dict[str, Any]:
        state = self.runs.get(run_id)
        if state is not None:
            run_dir = state.output_dir
            if (run_dir / DATABASE_NAME).is_file():
                summary = RunStateStore(run_dir).read()
            else:
                summary = state.public_dict()
        else:
            run_dir = self._safe_run_dir(run_id)
            if (run_dir / DATABASE_NAME).is_file():
                summary = RunStateStore(run_dir).read()
            else:
                summary = self._read_run_file(run_id, "summary.json")
        if include_metrics:
            signature = (evidence_signature(run_dir), summary)
            with self._metric_lock:
                cached = self._metric_cache.get(run_id)
                if cached is not None and cached[0] == signature:
                    summary = copy.deepcopy(cached[1])
                else:
                    enriched = enrich_run_metrics(summary, run_dir)
                    if run_id not in self._metric_cache and len(self._metric_cache) >= 8:
                        oldest = next(iter(self._metric_cache))
                        self._metric_cache.pop(oldest, None)
                    self._metric_cache[run_id] = (copy.deepcopy(signature), enriched)
                    summary = copy.deepcopy(enriched)
        return summary

    def progress(self, run_id: str, after: int = 0) -> dict[str, Any]:
        run_dir = self._safe_run_dir(run_id)
        store = RunStateStore(run_dir)
        if store.path.is_file():
            return store.read(after)
        return self.get_run(run_id, include_metrics=False)

    def progress_overview(self, run_id: str) -> dict[str, Any]:
        run_dir = self._safe_run_dir(run_id)
        store = RunStateStore(run_dir)
        if store.path.is_file():
            return store.overview()
        return self.get_run(run_id, include_metrics=False)

    def get_sample(
        self,
        run_id: str,
        sample_id: str,
        *,
        refresh: bool = True,
    ) -> dict[str, Any]:
        if not _SAMPLE_ID_PATTERN.fullmatch(sample_id):
            raise KeyError(sample_id)
        run_dir = self._safe_run_dir(run_id)
        sample_dir = (run_dir / sample_id).resolve()
        try:
            sample_dir.relative_to(run_dir)
        except ValueError as exc:
            raise KeyError(sample_id) from exc
        result = self._read_json(sample_dir / "result.json")
        attempts: list[dict[str, Any]] = []
        result_metadata_updated = False
        for attempt_dir in sorted(sample_dir.glob("attempt_*")):
            attempt: dict[str, Any] = {"name": attempt_dir.name}
            for name in ("request", "response", "result", "blocks"):
                path = attempt_dir / f"{name}.json"
                if path.is_file():
                    attempt[name] = self._read_json(path)
            if refresh and self._refresh_missing_artifact(attempt_dir, attempt):
                result_metadata_updated = True
                final_attempt = result.get("finalAttempt")
                if (
                    isinstance(final_attempt, int)
                    and attempt_dir.name == f"attempt_{final_attempt:03d}"
                ):
                    attempt_result = attempt.get("result")
                    if isinstance(attempt_result, dict):
                        self._copy_result_metadata(attempt_result, result)
            trace_view_path = attempt_dir / "trace" / "view.json"
            if trace_view_path.is_file():
                attempt["trace"] = self._read_json(trace_view_path)
            if refresh and self._refresh_missing_trace(run_dir, attempt_dir, attempt):
                result_metadata_updated = True
                final_attempt = result.get("finalAttempt")
                if isinstance(final_attempt, int) and attempt_dir.name == (
                    f"attempt_{final_attempt:03d}"
                ):
                    refreshed_view = attempt.get("trace")
                    if isinstance(refreshed_view, dict):
                        self._apply_trace_view_metadata(refreshed_view, result)
            genui_path = attempt_dir / "genui.jsonl"
            if genui_path.is_file():
                attempt["genui"] = genui_path.read_text(encoding="utf-8")
            attempts.append(attempt)
        if result_metadata_updated:
            atomic_write_json(sample_dir / "result.json", result)
            self._update_run_result_metadata(run_id, sample_id, result, run_dir)
        return {"summary": result, "attempts": attempts}

    def _refresh_missing_artifact(self, attempt_dir: Path, attempt: dict[str, Any]) -> bool:
        response = attempt.get("response")
        attempt_result = attempt.get("result")
        if not isinstance(response, dict) or not isinstance(attempt_result, dict):
            return False
        if response.get("status") not in {"success", "degraded"}:
            return False
        if (attempt_dir / "genui.jsonl").is_file():
            return False
        data = response.get("data")
        artifact_url = data.get("artifactUrl") if isinstance(data, dict) else None
        if not isinstance(artifact_url, str) or not artifact_url:
            return False
        try:
            roots = self._artifact_roots(attempt_dir.parent.parent)
            if find_local_artifact(artifact_url, roots) is None:
                return False
            content = download_artifact(
                artifact_url, attempt_dir / "artifact.md", roots, local_only=True
            )
            blocks = self._save_artifact_preview(attempt_dir, content)
        except (OSError, ValueError) as exc:
            _logger.warning("补齐本地产物失败: %s: %s", type(exc).__name__, exc)
            return False
        attempt_result["artifactAvailable"] = True
        attempt_result["dslAvailable"] = True
        attempt_result["blockNames"] = sorted(blocks)
        if attempt_result.get("traceStatus") != "invalid":
            attempt_result["error"] = ""
        attempt["blocks"] = blocks
        atomic_write_json(attempt_dir / "result.json", attempt_result)
        return True

    @staticmethod
    def _save_artifact_preview(attempt_dir: Path, content: str) -> dict[str, Any]:
        blocks = parse_artifact_blocks(content)
        genui = blocks.get("genui")
        if not isinstance(genui, str) or not genui.strip():
            raise ValueError("artifact 缺少有效 genui 代码块")
        atomic_write_json(attempt_dir / "blocks.json", blocks)
        write_artifact_blocks(attempt_dir, blocks)
        (attempt_dir / "genui.jsonl").write_text(genui, encoding="utf-8")
        return blocks

    def _refresh_missing_trace(
        self,
        run_dir: Path,
        attempt_dir: Path,
        attempt: dict[str, Any],
    ) -> bool:
        view = attempt.get("trace")
        attempt_result = attempt.get("result")
        if not isinstance(view, dict) or view.get("status") != "missing":
            return False
        if not isinstance(attempt_result, dict):
            return False
        uid = attempt_result.get("uid")
        if not isinstance(uid, str) or not uid:
            return False
        with self._trace_refresh_lock:
            importer = self._trace_importer_for_run(run_dir)
            if not importer.is_terminal(uid):
                return False
            refreshed = importer.import_trace(
                uid,
                attempt_dir / "trace",
                run_dir / "trace_blobs",
            )
            attempt["trace"] = refreshed
            self._apply_trace_view_metadata(refreshed, attempt_result)
            atomic_write_json(attempt_dir / "result.json", attempt_result)
        return True

    @staticmethod
    def _apply_trace_view_metadata(view: dict[str, Any], target: dict[str, Any]) -> None:
        target["traceStatus"] = view.get("status", "missing")
        target["traceRecordCount"] = view.get("recordCount", 0)
        target["traceSchemaVersion"] = view.get("sourceSchemaVersion", "")
        target["traceDurationMs"] = _trace_duration(view)

    @staticmethod
    def _copy_result_metadata(source: dict[str, Any], target: dict[str, Any]) -> None:
        for name in (
            "traceStatus",
            "traceRecordCount",
            "traceSchemaVersion",
            "traceDurationMs",
        ):
            target[name] = source.get(name)
        for name in ("artifactAvailable", "dslAvailable", "error"):
            if name in source:
                target[name] = source.get(name)

    def _update_run_result_metadata(
        self,
        run_id: str,
        sample_id: str,
        result: dict[str, Any],
        run_dir: Path,
    ) -> None:
        state = self.runs.get(run_id)
        if state is not None:
            for sample in state.samples:
                if sample.get("id") == sample_id:
                    self._copy_result_metadata(result, sample)
                    break
            self._persist_state(state)
            return
        for name in ("manifest.json", "summary.json"):
            path = run_dir / name
            if not path.is_file():
                continue
            summary = self._read_json(path)
            samples = summary.get("samples")
            if not isinstance(samples, list):
                continue
            for sample in samples:
                if isinstance(sample, dict) and sample.get("id") == sample_id:
                    self._copy_result_metadata(result, sample)
                    break
            summary["traceWarnings"] = sum(
                1
                for sample in samples
                if isinstance(sample, dict)
                and sample.get("traceStatus") in {"missing", "partial", "invalid"}
            )
            atomic_write_json(path, summary)
            if name == "summary.json":
                (run_dir / "summary.md").write_text(
                    self._summary_markdown(summary),
                    encoding="utf-8",
                )

    def get_trace_artifact(self, run_id: str, digest: str) -> tuple[Path, str]:
        if not _TRACE_DIGEST_PATTERN.fullmatch(digest):
            raise KeyError(digest)
        run_dir = self._safe_run_dir(run_id)
        manifest_path = run_dir / "trace_blob_manifest.json"
        if not manifest_path.is_file():
            raise KeyError("Trace 附件未登记")
        manifest = self._read_json(manifest_path)
        blobs = manifest.get("blobs")
        registration = blobs.get(digest) if isinstance(blobs, dict) else None
        if not isinstance(registration, dict):
            raise KeyError("Trace 附件未登记")
        blob_root = (run_dir / "trace_blobs").resolve()
        artifact_path = (blob_root / digest).resolve()
        if not artifact_path.is_relative_to(run_dir):
            raise KeyError("Trace 附件路径越界")
        if not artifact_path.is_file():
            raise KeyError("Trace 附件文件缺失")
        content = artifact_path.read_bytes()
        if len(content) != registration.get("bytes"):
            raise ValueError("Trace 附件字节数不匹配")
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Trace 附件 SHA-256 不匹配")
        return artifact_path, str(registration.get("mediaType") or "application/octet-stream")

    async def _execute_run(
        self,
        state: BatchRunState,
        available: dict[str, DatasetItem],
    ) -> None:
        state.status = "running"
        await self._write(RunStateStore(state.output_dir).update, self._overview(state))
        state.request_slots = asyncio.Semaphore(state.concurrency)
        state.collector = CollectionPool(
            lambda job: self._collect_attempt(state, job),
            concurrency=self.collection_capacity,
            capacity=self.collection_capacity,
        )

        async def execute_sample(sample: dict[str, Any]) -> None:
            try:
                if state.cancel_event.is_set():
                    sample["status"] = "cancelled"
                    return
                item = available.get(str(sample.get("id")))
                if item is None:
                    sample["status"] = "failed"
                    sample["error"] = "数据样本不存在"
                    return
                await self._execute_sample(state, sample, item)
            except asyncio.CancelledError:
                if sample.get("status") not in {"success", "degraded", "failed", "unsupported"}:
                    sample["status"] = "cancelled"
            except Exception as exc:
                state.error = f"{type(exc).__name__}: {exc}"
                state.cancel_event.set()
                for worker in state.workers:
                    if worker is not asyncio.current_task():
                        worker.cancel()

        state.workers = [
            asyncio.create_task(execute_sample(sample))
            for sample in state.samples
            if sample.get("status") == "queued"
        ]
        await asyncio.gather(*state.workers, return_exceptions=True)
        try:
            await state.collector.close()
        except Exception as exc:
            state.error = f"{type(exc).__name__}: {exc}"
        await self._finish_metrics(state)
        if state.cancel_event.is_set():
            for sample in state.samples:
                if sample.get("status") in {"queued", "running"}:
                    sample["status"] = "cancelled"
                    sample["traceStatus"] = "missing"
            state.status = "cancelled"
        else:
            state.status = "completed"
        if state.error:
            state.status = "failed"
        state.finished_at = utc_now()
        await self._write(self._persist_state, state)
        self.tasks.pop(state.run_id, None)

    async def _execute_sample(
        self,
        state: BatchRunState,
        sample: dict[str, Any],
        item: DatasetItem,
    ) -> None:
        sample["status"] = "running"
        source = await self._write(self._read_json, item.path)
        generation_elapsed = float(sample.get("elapsedMs", 0.0))
        final_attempt: dict[str, Any] | None = None
        start_attempt = int(sample.get("attemptCount") or 0)
        for attempt in range(start_attempt, state.max_retries + 1):
            if state.cancel_event.is_set():
                sample["status"] = "cancelled"
                break
            collector = state.collector
            if collector is None:
                raise RuntimeError("批跑收集器未初始化")
            await collector.reserve()
            submitted = False
            try:
                uid = f"batch-{state.run_token}-{int(sample.get('sequence', 0)):05d}-{attempt:03d}"
                request = prepare_request(
                    source,
                    uid=uid,
                    run_token=state.run_token,
                    sample_id=str(sample.get("id")),
                    attempt=attempt,
                )
                request = apply_request_overrides(request, state.request_overrides)
                attempt_dir = state.output_dir / str(sample.get("id")) / f"attempt_{attempt:03d}"
                await self._write(atomic_write_json, attempt_dir / "request.json", request)
                sample["attemptCount"] = attempt + 1
                sample["status"] = "running"
                sample["traceStatus"] = "pending"
                sample["traceRecordCount"] = 0
                sample["finalAttempt"] = attempt
                await self._save_sample(state, sample)

                def publish_response(
                    response: dict[str, Any],
                    elapsed_ms: float,
                    response_uid: str = uid,
                    response_attempt: int = attempt,
                ) -> None:
                    nonlocal generation_elapsed
                    generation_elapsed += elapsed_ms
                    sample["uid"] = response_uid
                    sample["status"] = response.get("status") or "failed"
                    sample["errorCode"] = response.get("errorCode") or ""
                    sample["error"] = _error_text(response.get("error"))
                    sample["elapsedMs"] = round(generation_elapsed, 2)
                    sample["finalAttempt"] = response_attempt

                final_attempt = await self._execute_attempt(
                    state,
                    request,
                    uid,
                    attempt_dir,
                    attempt,
                    publish_response,
                )
                job = {
                    "id": f"{sample.get('id')}/{attempt_dir.name}",
                    "uid": uid,
                    "sampleId": sample.get("id"),
                    "attempt": attempt,
                    "status": "pending",
                }
                await self._save_sample(state, sample, job)
                collector.submit(job)
                submitted = True
            finally:
                if not submitted:
                    collector.release()
            if final_attempt.get("completed"):
                break
            error_code = str(final_attempt.get("errorCode") or "")
            if final_attempt.get("status") == "unsupported":
                break
            if error_code in _DETERMINISTIC_ERROR_CODES:
                break
        await self._save_sample(state, sample)

    async def _execute_attempt(
        self,
        state: BatchRunState,
        request: dict[str, Any],
        uid: str,
        attempt_dir: Path,
        attempt: int,
        publish_response: Callable[[dict[str, Any], float], None],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        response: dict[str, Any] = {}
        error = ""
        try:
            if state.request_slots is None:
                raise RuntimeError("请求并发控制未初始化")
            async with state.request_slots:
                started = time.perf_counter()
                response = await self.invoke(state.endpoint, request, state.timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, UpstreamToolError) and exc.frames:
                response = {"frames": list(exc.frames)}
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        publish_response(response, elapsed_ms)
        result = {
            "attempt": attempt,
            "uid": uid,
            "status": response.get("status") or "failed",
            "errorCode": response.get("errorCode") or "",
            "error": error,
            "elapsedMs": elapsed_ms,
            "artifactAvailable": False,
            "dslAvailable": False,
            "traceStatus": "pending",
            "completed": response.get("status") in {"success", "degraded"},
        }
        await self._write(atomic_write_json, attempt_dir / "response.json", response)
        await self._write(atomic_write_json, attempt_dir / "result.json", result)
        return result

    async def _collect_attempt(self, state: BatchRunState, job: dict[str, Any]) -> None:
        sample_id = str(job.get("sampleId"))
        attempt = int(job.get("attempt", 0))
        if not _SAMPLE_ID_PATTERN.fullmatch(sample_id) or attempt < 0:
            raise ValueError("收集任务样本或尝试编号不合法")
        attempt_dir = state.output_dir / sample_id / f"attempt_{attempt:03d}"
        response = await self._collect_io(self._read_json, attempt_dir / "response.json")
        result = await self._collect_io(self._read_json, attempt_dir / "result.json")
        uid = str(job.get("uid"))
        error = str(result.get("error") or "")
        artifact_available = False
        dsl_available = False
        blocks: dict[str, Any] = {}

        async def collect_artifact() -> None:
            nonlocal artifact_available, dsl_available, blocks, error
            try:
                data = response.get("data")
                artifact_url = data.get("artifactUrl") if isinstance(data, dict) else None
                if response.get("status") not in {"success", "degraded"}:
                    return
                if not artifact_url:
                    error = "响应未返回 artifactUrl"
                    return
                async with asyncio.timeout(self.collection_timeout_seconds):
                    roots = await self._collect_io(self._artifact_roots, state.output_dir)
                    async with self._artifact_slots:
                        artifact_text = await asyncio.to_thread(
                            download_artifact,
                            str(artifact_url),
                            attempt_dir / "artifact.md",
                            roots,
                        )
                    artifact_available = True
                    blocks = await self._collect_io(
                        self._save_artifact_preview, attempt_dir, artifact_text
                    )
                    dsl_available = True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"

        async def collect_trace() -> dict[str, Any]:
            try:
                return await asyncio.wait_for(
                    self._import_trace(uid, attempt_dir, state.output_dir),
                    self.collection_timeout_seconds,
                )
            except Exception as exc:
                return {
                    "status": "invalid",
                    "error": f"Trace 导入失败: {type(exc).__name__}: {exc}",
                }

        _, trace = await asyncio.gather(collect_artifact(), collect_trace())
        trace_error = trace.get("error")
        if trace_error:
            error = f"{error}; {trace_error}" if error else str(trace_error)
        result.update(
            {
                "artifactAvailable": artifact_available,
                "dslAvailable": dsl_available,
                "blockNames": sorted(blocks),
                "error": error or _error_text(response.get("error")),
                "traceStatus": trace.get("status", "missing"),
                "traceRecordCount": trace.get("recordCount", 0),
                "traceSchemaVersion": trace.get("sourceSchemaVersion", ""),
                "traceDurationMs": _trace_duration(trace),
            }
        )
        await self._collect_io(atomic_write_json, attempt_dir / "result.json", result)
        sample = state.sample_index.get(sample_id)
        completed_job = {**job, "status": "complete"}
        analysis: dict[str, Any] = {}
        if sample is not None:
            analysis = await self._collect_io(
                enrich_run_metrics,
                {"samples": [dict(sample)]},
                state.output_dir,
            )
        if sample is not None:
            if sample.get("finalAttempt") == attempt:
                self._copy_result_metadata(result, sample)
            analyzed_samples = analysis.get("samples") or []
            if analyzed_samples:
                sample["generationMetrics"] = analyzed_samples[0].get("generationMetrics", {})
            await self._save_sample(state, sample, completed_job)
        else:
            await self._write(RunStateStore(state.output_dir).update, {}, None, completed_job)
        if state.metrics_task is None or state.metrics_task.done():
            state.metrics_task = asyncio.create_task(self._refresh_metrics(state))

    async def _refresh_metrics(self, state: BatchRunState) -> None:
        await asyncio.sleep(0.5)
        samples = [dict(sample) for sample in state.samples]
        analysis = await self._collect_io(self._aggregate_metrics, samples, state.output_dir)
        state.generation_metrics = analysis.get("generationMetrics", {})
        state.retry_parameters = analysis.get("observedRetryParameters", {})
        await self._write(RunStateStore(state.output_dir).update, self._overview(state))

    @staticmethod
    async def _finish_metrics(state: BatchRunState) -> None:
        if state.metrics_task is not None:
            if not state.metrics_task.done():
                state.metrics_task.cancel()
            results = await asyncio.gather(state.metrics_task, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    state.error = f"指标更新失败: {type(result).__name__}: {result}"

    @staticmethod
    def _aggregate_metrics(samples: list[dict[str, Any]], run_dir: Path) -> dict[str, Any]:
        return enrich_run_metrics({"samples": samples}, run_dir, stored_evidence=True)

    async def _import_trace(
        self,
        uid: str,
        attempt_dir: Path,
        run_dir: Path,
    ) -> dict[str, Any]:
        importer = await self._collect_io(self._trace_importer_for_run, run_dir)
        deadline = time.monotonic() + 5.0
        while True:
            async with self._trace_slots:
                terminal = await asyncio.to_thread(importer.is_terminal, uid)
            if terminal or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.1)
        async with self._trace_slots:
            return await asyncio.to_thread(
                importer.import_trace,
                uid,
                attempt_dir / "trace",
                run_dir / "trace_blobs",
            )

    def _effective_service_config(self, run_dir: Path) -> dict[str, Any]:
        path = self.output_root / "executions" / f"{run_dir.name}.json"
        result: dict[str, Any] = {}
        if path.is_file():
            effective = self._read_json(path).get("effectiveServiceConfig")
            if isinstance(effective, dict):
                result = effective
        return result

    def _trace_importer_for_run(self, run_dir: Path) -> TraceImporter:
        value = self._effective_service_config(run_dir).get("generation_trace_root")
        importer = self.trace_importer
        if isinstance(value, str) and value:
            root = Path(value)
            if not root.is_absolute():
                root = self.cloud_root.parent / root
            if root.resolve() != self.trace_importer.trace_root:
                importer = TraceImporter(root)
        return importer

    def _artifact_roots(self, run_dir: Path | None = None) -> tuple[Path, ...]:
        from .service_manager import resolve_artifact_workspace

        service_config: dict[str, Any] | None = None
        summary_path = run_dir / "summary.json" if run_dir is not None else None
        if summary_path is not None and summary_path.is_file():
            summary = self._read_json(summary_path)
            task_id = summary.get("taskId")
            if isinstance(task_id, str) and _SAMPLE_ID_PATTERN.fullmatch(task_id):
                task_path = (self.output_root / "tasks" / f"{task_id}.json").resolve()
                if task_path.is_relative_to(self.output_root) and task_path.is_file():
                    task = self._read_json(task_path)
                    config = task.get("serviceConfig")
                    if task.get("backendMode") == "managed" and isinstance(config, dict):
                        service_config = config
        if run_dir is not None:
            effective = self._effective_service_config(run_dir)
            if effective:
                service_config = effective
        workspace = self.cloud_root / "workspace"
        configured = resolve_artifact_workspace(self.cloud_root.parent, service_config)
        return (
            configured,
            configured / "mock_obs",
            workspace,
            workspace / "mock_obs",
            self.cloud_root,
        )

    @staticmethod
    def _snapshot_evaluation(dataset_root: Path, output_dir: Path) -> None:
        source = (dataset_root / "evaluation").resolve()
        if not source.is_relative_to(dataset_root.resolve()) or not source.is_dir():
            return
        target = output_dir / "dataset_evaluation"
        shutil.copytree(source, target, dirs_exist_ok=True)
        files: list[dict[str, Any]] = []
        for path in sorted(target.glob("*.json")):
            if path.name == "summary.json":
                continue
            content = path.read_bytes()
            item: dict[str, Any] = {
                "name": path.name,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            try:
                value = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict):
                item["schemaVersion"] = str(value.get("schemaVersion") or "")
                samples = value.get("samples")
                if isinstance(samples, dict):
                    item["annotatedSamples"] = len(samples)
            files.append(item)
        atomic_write_json(
            target / "summary.json",
            {
                "schemaVersion": "batch-dataset-evaluation-snapshot-v1",
                "files": files,
            },
        )

    def _persist_state(self, state: BatchRunState) -> None:
        summary = state.public_dict()
        for sample in state.samples:
            state.sample_index[str(sample.get("id"))] = sample
        state.progress = RunProgress(state.samples)
        store = RunStateStore(state.output_dir)
        if not store.path.is_file():
            store.initialize(summary)
        elif state.status in {"completed", "cancelled", "failed", "interrupted"}:
            for sample in state.samples:
                store.update({}, sample)
            store.update(self._overview(state))
        else:
            store.update(self._overview(state))
            return
        if state.status in {"completed", "cancelled", "failed", "interrupted"}:
            summary = enrich_run_metrics(summary, state.output_dir)
        summary["error"] = state.error
        atomic_write_json(state.output_dir / "manifest.json", summary)
        atomic_write_json(state.output_dir / "summary.json", summary)
        (state.output_dir / "summary.md").write_text(
            self._summary_markdown(summary),
            encoding="utf-8",
        )
        if state.status in {"completed", "cancelled", "failed", "interrupted"}:
            exported = dict(summary)
            exported.pop("samples", None)
            exported["exportReady"] = True
            store.update(exported)

    @staticmethod
    def _summary_markdown(summary: dict[str, Any]) -> str:
        lines = [
            f"# Batch {summary.get('runId', '')}",
            "",
            f"- 状态: {summary.get('status', '')}",
            f"- 开始时间: {summary.get('startedAt', '')}",
            f"- 总数: {summary.get('total', 0)}",
            f"- 成功/降级/失败: {summary.get('success', 0)}/"
            f"{summary.get('degraded', 0)}/{summary.get('failed', 0)}",
            f"- Trace 告警: {summary.get('traceWarnings', 0)}",
        ]
        metrics = summary.get("generationMetrics")
        if isinstance(metrics, dict):
            for key, label in (
                ("firstDslPassRate", "首稿校验通过率"),
                ("firstRequestDeliveryRate", "首次请求交付率（含内部修复，不含批跑重试）"),
                ("finalDeliveryRate", "最终交付率（含降级）"),
                ("repairRescueRate", "内部修复挽救率"),
                ("batchRetryRate", "批跑重试率"),
                ("batchRetryRescueRate", "批跑重试挽救率"),
            ):
                rate = metrics.get(key)
                if isinstance(rate, dict):
                    percent = rate.get("percent")
                    display = f"{percent}%" if percent is not None else "未知"
                    lines.append(
                        f"- {label}: {display}（{rate.get('numerator')}/{rate.get('denominator')}）"
                    )
            lines.append(
                f"- 生成前失败 / 首稿证据缺失: {metrics.get('preDslFailed')} / "
                f"{metrics.get('firstDslUnknown')}"
            )
            for key, label in (
                ("qualityRepairAttempts", "内部修复尝试"),
                ("modelPhysicalCalls", "模型物理调用"),
                ("p95ElapsedMs", "P95 总耗时(ms)"),
            ):
                value = metrics.get(key)
                lines.append(f"- {label}: {value if value is not None else '未知'}")
            observed = metrics.get("coverageObserved", 0)
            missing = metrics.get("coverageMissing") if observed else "未知"
            lines.append(f"- 交付信息缺失告警: {missing} 个样本，覆盖检查证据 {observed} 个样本")
        lines.extend(
            [
                "",
                "| 样本 | 状态 | 尝试 | 耗时(ms) | Trace | 错误码 |",
                "|---|---|---:|---:|---|---|",
            ]
        )
        samples = summary.get("samples")
        if isinstance(samples, list):
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                lines.append(
                    f"| {sample.get('id', '')} | {sample.get('status', '')} | "
                    f"{sample.get('attemptCount', 0)} | {sample.get('elapsedMs', 0)} | "
                    f"{sample.get('traceStatus', '')} | {sample.get('errorCode', '')} |"
                )
        return "\n".join(lines) + "\n"

    def _safe_run_dir(self, run_id: str) -> Path:
        if not _SAMPLE_ID_PATTERN.fullmatch(run_id):
            raise KeyError(run_id)
        run_dir = (self.output_root / run_id).resolve()
        try:
            run_dir.relative_to(self.output_root)
        except ValueError as exc:
            raise KeyError(run_id) from exc
        if not run_dir.is_dir():
            raise KeyError(run_id)
        return run_dir

    def _read_run_file(self, run_id: str, name: str) -> dict[str, Any]:
        return self._read_json(self._safe_run_dir(run_id) / name)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KeyError(str(path)) from exc
        if not isinstance(value, dict):
            raise KeyError(str(path))
        return value


def _error_text(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _trace_duration(trace: dict[str, Any]) -> float:
    summary = trace.get("summary")
    if not isinstance(summary, dict):
        return 0.0
    value = summary.get("totalDurationMs")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return round(float(value), 2)
