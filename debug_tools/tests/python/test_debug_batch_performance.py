"""批跑解耦、增量保存、轻量读取与故障恢复的机制回归，不调用真实模型。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from debug_tools.batch_testing import api, runner
from debug_tools.batch_testing.read_models import ExecutionIndex
from debug_tools.batch_testing.runner import BatchRunManager, atomic_write_json
from debug_tools.batch_testing.state_store import RunProgress, RunStateStore
from debug_tools.batch_testing.task_manager import BatchTaskManager
from fastapi import FastAPI
from fastapi.testclient import TestClient


def make_manager(tmp_path, monkeypatch, count=12, invoke=None):
    datasets = tmp_path / "datasets"
    for index in range(count):
        atomic_write_json(
            datasets / f"Q{index:03d}.json",
            {"content": {"userQuery": f"测试 {index}"}},
        )

    async def unsupported(*_args):
        return {"status": "unsupported", "data": {}}

    async def trace(*_args):
        return {"status": "complete", "recordCount": 1}

    manager = BatchRunManager(
        datasets,
        tmp_path / "output",
        tmp_path / "traces",
        tmp_path / "cloud",
        invoke=invoke or unsupported,
    )
    monkeypatch.setattr(manager, "_import_trace", trace)
    return manager


def start(manager, count=12, retries=0):
    started = manager.start_run(
        [f"Q{index:03d}" for index in range(count)],
        "ws://localhost/tools",
        4,
        retries,
    )
    run_id = started.get("runId")
    assert isinstance(run_id, str)
    return run_id


@pytest.mark.asyncio
async def test_requests_finish_while_all_collection_is_blocked(tmp_path, monkeypatch):
    release = asyncio.Event()
    all_requested = asyncio.Event()
    first_wave = asyncio.Event()
    calls = 0
    active = 0
    maximum = 0

    async def invoke(*_args):
        nonlocal calls, active, maximum
        active += 1
        maximum = max(maximum, active)
        if maximum == 4:
            first_wave.set()
        await first_wave.wait()
        await asyncio.sleep(0)
        active -= 1
        calls += 1
        if calls == 12:
            all_requested.set()
        return {"status": "unsupported"}

    async def blocked(*_args):
        await release.wait()
        return {"status": "complete"}

    manager = make_manager(tmp_path, monkeypatch, invoke=invoke)
    monkeypatch.setattr(manager, "_import_trace", blocked)
    run_id = start(manager)
    task = manager.tasks.get(run_id)
    assert task is not None
    try:
        await asyncio.wait_for(all_requested.wait(), 5)
        assert calls == 12
        assert maximum == 4
        assert not task.done()
        original = json.loads((manager.output_root / run_id / "summary.json").read_text())
        assert original.get("completed") == 0
    finally:
        release.set()
        await task
        await manager.close()
    assert RunStateStore(manager.output_root / run_id).overview().get("exportReady") is True


@pytest.mark.asyncio
async def test_collection_capacity_applies_backpressure_before_sending(tmp_path, monkeypatch):
    release = asyncio.Event()
    filled = asyncio.Event()
    calls = 0

    async def invoke(*_args):
        nonlocal calls
        calls += 1
        if calls == 4:
            filled.set()
        return {"status": "unsupported"}

    async def blocked(*_args):
        await release.wait()
        return {"status": "complete"}

    manager = make_manager(tmp_path, monkeypatch, invoke=invoke)
    manager.collection_capacity = 4
    monkeypatch.setattr(manager, "_import_trace", blocked)
    run_id = start(manager)
    task = manager.tasks.get(run_id)
    assert task is not None
    try:
        await asyncio.wait_for(filled.wait(), 5)
        await asyncio.sleep(0.05)
        assert calls == 4
        assert manager.progress_overview(run_id).get("collectionBackpressure") is True
    finally:
        release.set()
        await task
        await manager.close()
    assert calls == 12


@pytest.mark.asyncio
async def test_late_first_attempt_cannot_overwrite_retry_trace(tmp_path, monkeypatch):
    release = asyncio.Event()
    second_collected = asyncio.Event()
    calls = 0

    async def invoke(*_args):
        nonlocal calls
        calls += 1
        return {"status": "failed" if calls == 1 else "unsupported", "errorCode": "TEMPORARY"}

    async def trace(uid, *_args):
        if uid.endswith("000"):
            await release.wait()
            return {"status": "invalid", "recordCount": 999}
        second_collected.set()
        return {"status": "complete", "recordCount": 7}

    manager = make_manager(tmp_path, monkeypatch, count=1, invoke=invoke)
    monkeypatch.setattr(manager, "_import_trace", trace)
    run_id = start(manager, 1, 1)
    task = manager.tasks.get(run_id)
    assert task is not None
    try:
        await asyncio.wait_for(second_collected.wait(), 5)
    finally:
        release.set()
        await task
        await manager.close()
    samples = manager.get_run(run_id, include_metrics=False).get("samples")
    assert isinstance(samples, list)
    assert samples[0].get("traceStatus") == "complete"
    assert samples[0].get("traceRecordCount") == 7
    assert samples[0].get("attemptCount") == 2


@pytest.mark.asyncio
async def test_progress_exports_full_summary_only_at_boundaries(tmp_path, monkeypatch):
    exports = []
    original = runner.atomic_write_json

    def record(path, value):
        if path.name in {"summary.json", "manifest.json"}:
            exports.append(path.name)
        original(path, value)

    monkeypatch.setattr(runner, "atomic_write_json", record)
    manager = make_manager(tmp_path, monkeypatch)
    run_id = start(manager)
    task = manager.tasks.get(run_id)
    assert task is not None
    await task
    await manager.close()
    assert exports.count("summary.json") == 2
    assert exports.count("manifest.json") == 2


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_incremental_state_serializes_linear_data(tmp_path, monkeypatch, count):
    samples = []
    for index in range(count):
        samples.append({"id": f"Q{index:05d}", "sequence": index, "status": "queued"})
    store = RunStateStore(tmp_path / "run")
    store.initialize({"runId": "run", "status": "running", "samples": samples})
    serialized_bytes = 0
    original = json.dumps

    def measure(value, **kwargs):
        nonlocal serialized_bytes
        text = original(value, **kwargs)
        serialized_bytes += len(text.encode())
        return text

    monkeypatch.setattr("debug_tools.batch_testing.state_store.json.dumps", measure)
    progress = RunProgress(samples)
    for sample in samples:
        sample["status"] = "success"
        progress.replace(sample)
        store.update(progress.snapshot(), sample)
    assert serialized_bytes < count * 750
    assert store.overview().get("completed") == count
    assert len(store.read(store.overview().get("revision", 0)).get("sampleChanges", [])) == 0


def test_sample_page_filters_across_all_samples_and_bounds_deltas(tmp_path):
    samples = []
    for index in range(1200):
        samples.append(
            {
                "id": f"Q{index:04d}",
                "sequence": index,
                "status": "failed" if index == 1100 else "success",
            }
        )
    store = RunStateStore(tmp_path)
    store.initialize({"samples": samples})
    page = store.page(0, 100, status="failed")
    assert page.get("total") == 1
    assert page.get("items", [])[0].get("id") == "Q1100"
    assert store.read(0).get("resetRequired") is True


def test_execution_index_reads_each_history_file_once(tmp_path, monkeypatch):
    for index in range(100):
        atomic_write_json(tmp_path / f"{index}.json", {"taskId": str(index % 10)})
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    execution_index = ExecutionIndex(tmp_path)
    execution_index.refresh()
    for number in range(10):
        assert len(execution_index.records(str(number))) == 10
    execution_index.refresh()
    assert len(reads) == 100


@pytest.mark.asyncio
async def test_recovery_collects_confirmed_response_without_invoking_service(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, count=1)
    run_dir = manager.output_root / "run_recovery"
    sample = {"id": "Q000", "sequence": 0, "status": "success", "finalAttempt": 0}
    store = RunStateStore(run_dir)
    store.initialize({"runId": "run_recovery", "status": "running", "samples": [sample]})
    job = {"id": "Q000/attempt_000", "sampleId": "Q000", "uid": "test", "attempt": 0}
    store.update({}, sample, job)
    atomic_write_json(run_dir / "Q000/attempt_000/response.json", {"status": "success"})
    atomic_write_json(run_dir / "Q000/attempt_000/result.json", {"status": "success"})

    async def forbidden(*_args):
        raise AssertionError("恢复收集不得重发业务请求")

    manager.invoke = forbidden
    await manager.recover_collections()
    await manager.close()
    assert store.pending() == []
    assert store.overview().get("status") == "interrupted"
    assert store.overview().get("exportReady") is True
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary.get("samples", [])[0].get("status") == "success"


def test_conditional_progress_never_reads_trace_or_recalculates_metrics(tmp_path, monkeypatch):
    paths = (tmp_path / "datasets", tmp_path / "output", tmp_path / "traces", tmp_path / "cloud")
    monkeypatch.setattr(api, "_default_paths", lambda *_args: paths)
    store = RunStateStore(paths[1] / "run_001")
    store.initialize({"runId": "run_001", "status": "running", "samples": [{"id": "Q001"}]})
    app = FastAPI()
    manager = api.register_batch_routes(app)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("轻量查询不得读取或重新计算 Trace")

    monkeypatch.setattr(runner, "enrich_run_metrics", forbidden)
    monkeypatch.setattr(manager.run_manager, "get_run", forbidden)
    client = TestClient(app)
    headers = {"X-Batch-View": "progress"}
    response = client.get("/debug/batch/runs/run_001", headers=headers)
    assert response.status_code == 200
    etag = response.headers.get("etag")
    assert etag is not None
    response = client.get(
        "/debug/batch/runs/run_001",
        headers={**headers, "If-None-Match": etag},
    )
    assert response.status_code == 304
    assert response.content == b""
    reset = client.get(
        "/debug/batch/runs/run_001",
        headers={**headers, "X-Batch-Generation": "old-database-generation"},
    )
    assert reset.json().get("resetRequired") is True
    page = client.get("/debug/batch/runs/run_001/samples?limit=1")
    assert page.status_code == 200
    assert len(page.json().get("items", [])) == 1


def test_readonly_sample_keeps_legacy_files_unchanged(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, count=1)
    run_dir = manager.output_root / "old_run"
    atomic_write_json(run_dir / "summary.json", {"samples": [{"id": "Q000"}]})
    atomic_write_json(run_dir / "Q000/result.json", {"id": "Q000"})
    atomic_write_json(run_dir / "Q000/attempt_000/result.json", {"traceStatus": "missing"})
    before = (run_dir / "summary.json").read_bytes()

    def forbidden(*_args):
        raise AssertionError("只读详情不得恢复或修改历史证据")

    monkeypatch.setattr(manager, "_refresh_missing_artifact", forbidden)
    monkeypatch.setattr(manager, "_refresh_missing_trace", forbidden)
    manager.get_sample("old_run", "Q000", refresh=False)
    assert (run_dir / "summary.json").read_bytes() == before
    assert not (run_dir / "batch_state.sqlite3").exists()


@pytest.mark.asyncio
async def test_export_failure_remains_recoverable_and_does_not_publish_ready(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, count=1)
    original = runner.atomic_write_json

    def fail_final_summary(path, value):
        if path.name == "summary.json" and value.get("status") == "completed":
            raise PermissionError("模拟终态导出失败")
        original(path, value)

    monkeypatch.setattr(runner, "atomic_write_json", fail_final_summary)
    run_id = start(manager, 1)
    task = manager.tasks.get(run_id)
    assert task is not None
    with pytest.raises(PermissionError, match="终态导出失败"):
        await task
    store = RunStateStore(manager.output_root / run_id)
    assert store.overview().get("exportReady") is False
    monkeypatch.setattr(runner, "atomic_write_json", original)
    await manager.recover_collections()
    await manager.close()
    assert store.overview().get("exportReady") is True


@pytest.mark.asyncio
async def test_stop_keeps_confirmed_results_and_drains_collection(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, count=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def trace(*_args):
        entered.set()
        await release.wait()
        return {"status": "complete"}

    monkeypatch.setattr(manager, "_import_trace", trace)
    run_id = start(manager, 1)
    task = manager.tasks.get(run_id)
    assert task is not None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await manager.cancel_run(run_id)
        assert not task.done()
    finally:
        release.set()
        await task
        await manager.close()
    saved = RunStateStore(manager.output_root / run_id).read()
    assert saved.get("status") == "cancelled"
    assert saved.get("samples", [])[0].get("status") == "unsupported"


@pytest.mark.parametrize("name,value", [("collection_capacity", 0), ("collection_concurrency", 17)])
def test_collection_configuration_rejects_invalid_bounds(tmp_path, name, value):
    from debug_tools.batch_testing.service_manager import load_batch_defaults

    config = tmp_path / "debug_tools/debug_tool.yaml"
    config.parent.mkdir()
    config.write_text(f"batch_testing:\n  {name}: {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="必须在"):
        load_batch_defaults(tmp_path)


@pytest.mark.asyncio
async def test_collection_deadline_preserves_business_status(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, count=1)
    manager.collection_timeout_seconds = 0.02

    async def never_finishes(*_args):
        await asyncio.Future()

    monkeypatch.setattr(manager, "_import_trace", never_finishes)
    run_id = start(manager, 1)
    task = manager.tasks.get(run_id)
    assert task is not None
    await asyncio.wait_for(task, 5)
    await manager.close()
    result = manager.get_run(run_id, include_metrics=False)
    assert result.get("status") == "completed"
    assert result.get("samples", [])[0].get("status") == "unsupported"
    assert result.get("samples", [])[0].get("traceStatus") == "invalid"


@pytest.mark.asyncio
async def test_corrupt_recovery_is_isolated(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, count=1)
    database = manager.output_root / "broken/batch_state.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_text("not a database", encoding="utf-8")
    failures = await manager.recover_collections()
    await manager.close()
    assert "broken" in failures


@pytest.mark.asyncio
async def test_task_start_does_not_wait_for_collection_recovery(tmp_path, monkeypatch):
    manager = make_manager(tmp_path, monkeypatch, count=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def recover():
        entered.set()
        await release.wait()
        return {}

    monkeypatch.setattr(manager, "recover_collections", recover)
    tasks = BatchTaskManager(
        manager.dataset_root,
        manager.output_root,
        tmp_path / "traces",
        tmp_path / "cloud",
        run_manager=manager,
        service_controller=SimpleNamespace(recover_orphan=AsyncMock(), stop=AsyncMock()),
    )
    await asyncio.wait_for(tasks.start(), 5)
    await asyncio.wait_for(entered.wait(), 5)
    try:
        assert tasks._recovery_task is not None
        assert not tasks._recovery_task.done()
    finally:
        release.set()
        await tasks.close()
