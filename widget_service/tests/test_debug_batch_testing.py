from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from debug_tools.batch_testing.runner import (
    BatchRunManager,
    discover_datasets,
    normalize_endpoint,
    prepare_request,
)
from debug_tools.batch_testing.trace_parser import TraceBundleReader
from debug_tools.end_to_end_debug.backend.debug_agent.config import DebugSettings
from debug_tools.end_to_end_debug.backend.server import create_app as create_full_app
from debug_tools.static_site import create_frontend_app


def _request(query: str = "生成天气卡片") -> dict:
    return {
        "content": {
            "userQuery": query,
            "title": "天气",
            "description": "天气卡片",
            "size": "2x2",
        },
        "session": {"sessionId": "source", "interactionId": "1"},
        "userAuth": {"user": {"userId": "source-user"}},
        "deviceInfo": {"prdVer": "12.0.0.1", "romVersion": "7.0"},
        "bundleName": "com.omega_w_0823.hmservice",
    }


def _write_dataset(root: Path, name: str, query: str = "生成天气卡片") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.json").write_text(
        json.dumps(_request(query), ensure_ascii=False),
        encoding="utf-8",
    )


def _artifact_text() -> str:
    return """```cardspec
{"title":"天气","description":"天气卡片","suggestSize":"2x2"}
```
```genui
{"version":"v0.9","createSurface":{}}
{"version":"v0.9","updateComponents":{}}
{"version":"v0.9","updateDataModel":{}}
```
```schema
{"schemaVersion":"widget-artifact-v2"}
```
"""


def test_dataset_discovery_keeps_invalid_entries_visible(tmp_path):
    _write_dataset(tmp_path, "Q001")
    (tmp_path / "Q002.json").write_text("[]", encoding="utf-8")

    items = discover_datasets(tmp_path)

    assert [item.sample_id for item in items] == ["Q001", "Q002"]
    assert items[0].valid is True
    assert items[1].valid is False
    assert "根节点" in items[1].error


def test_prepare_request_overrides_uid_without_mutating_source():
    source = _request()

    prepared = prepare_request(
        source,
        uid="batch-1234abcd-00001-002",
        run_token="1234abcd",
        sample_id="Q001",
        attempt=2,
    )

    content = prepared.get("content")
    user_auth = prepared.get("userAuth")
    session = prepared.get("session")
    source_content = source.get("content")
    source_user_auth = source.get("userAuth")
    assert isinstance(content, dict)
    assert isinstance(user_auth, dict)
    assert isinstance(session, dict)
    assert isinstance(source_content, dict)
    assert isinstance(source_user_auth, dict)
    user = user_auth.get("user")
    source_user = source_user_auth.get("user")
    assert isinstance(user, dict)
    assert isinstance(source_user, dict)
    assert content.get("uid") == "batch-1234abcd-00001-002"
    assert user.get("userId") == "batch-1234abcd-00001-002"
    assert session.get("interactionId") == "Q001-002"
    assert source_content.get("uid") is None
    assert source_user.get("userId") == "source-user"


def test_trace_reader_supports_registered_unknown_and_broken_lines(tmp_path):
    trace_dir = tmp_path / "batch" / "1234abcd" / "00001" / "000"
    trace_dir.mkdir(parents=True)
    (trace_dir / "model_trace.jsonl").write_text(
        '{"model_output":"first"}\ninvalid\n{"model_output":"second"}\n',
        encoding="utf-8",
    )
    (trace_dir / "future_trace.jsonl").write_text(
        '{"kind":"prompt","value":"hello"}\n',
        encoding="utf-8",
    )

    bundle = TraceBundleReader(tmp_path).read("batch-1234abcd-00001-000")

    assert bundle.get("status") == "parse_error"
    assert bundle.get("recordCount") == 3
    sections = bundle.get("sections")
    assert isinstance(sections, list)
    assert {section.get("name") for section in sections} == {
        "future_trace.jsonl",
        "model_trace.jsonl",
    }
    assert len(bundle.get("warnings", [])) == 1


def test_trace_reader_reports_missing_without_failing(tmp_path):
    bundle = TraceBundleReader(tmp_path).read("batch-1234abcd-00001-000")

    assert bundle.get("status") == "missing"
    assert bundle.get("recordCount") == 0


def test_normalize_endpoint_appends_operation_and_rejects_invalid_scheme():
    assert normalize_endpoint("ws://127.0.0.1:8855/api/v1/ws/tools").endswith(
        "/generateWidgetCardCompactDsl"
    )
    with pytest.raises(ValueError, match="ws://"):
        normalize_endpoint("http://127.0.0.1:8855")


@pytest.mark.asyncio
async def test_batch_runner_honors_concurrency_retries_and_persists_trace(tmp_path):
    dataset_root = tmp_path / "datasets"
    output_root = tmp_path / "output"
    trace_root = tmp_path / "traces"
    cloud_root = tmp_path / "cloud"
    workspace = cloud_root / "workspace"
    workspace.mkdir(parents=True)
    artifact_path = workspace / "artifact_test.md"
    artifact_path.write_text(_artifact_text(), encoding="utf-8")
    for index in range(1, 4):
        _write_dataset(dataset_root, f"Q{index:03d}", f"query-{index}")
    active = 0
    maximum_active = 0
    calls: dict[str, int] = {}

    async def invoke(_endpoint, request, _timeout):
        nonlocal active, maximum_active
        content = request.get("content")
        assert isinstance(content, dict)
        uid = content.get("uid")
        assert isinstance(uid, str)
        session = request.get("session")
        assert isinstance(session, dict)
        interaction_id = session.get("interactionId")
        assert isinstance(interaction_id, str)
        sample = interaction_id.split("-")[0]
        calls[sample] = calls.get(sample, 0) + 1
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        parts = uid.split("-")
        trace_dir = trace_root.joinpath(parts[0], parts[1], parts[2], parts[3])
        trace_dir.mkdir(parents=True, exist_ok=True)
        (trace_dir / "model_trace.jsonl").write_text(
            json.dumps({"model_output": sample}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if calls[sample] == 1:
            return {"status": "failed", "errorCode": "VALIDATION_FAILED", "data": {}}
        return {
            "status": "success",
            "errorCode": "",
            "data": {"artifactUrl": f"https://obs.todo.local/{artifact_path.name}"},
        }

    manager = BatchRunManager(
        dataset_root,
        output_root,
        trace_root,
        cloud_root,
        invoke=invoke,
    )
    started = manager.start_run(
        ["Q001", "Q002", "Q003"],
        "ws://127.0.0.1:8855/api/v1/ws/tools",
        concurrency=2,
        max_retries=1,
    )
    run_id = started.get("runId")
    assert isinstance(run_id, str)
    task = manager.tasks[run_id]
    await task
    summary = manager.get_run(run_id)

    assert maximum_active == 2
    assert summary.get("status") == "completed"
    assert summary.get("success") == 3
    assert summary.get("traceWarnings") == 0
    assert all(sample.get("attemptCount") == 2 for sample in summary.get("samples", []))
    run_dir = output_root / run_id
    assert (run_dir / "summary.json").is_file()
    assert (run_dir / "Q001" / "attempt_001" / "genui.jsonl").is_file()
    assert manager.get_sample(run_id, "Q001").get("attempts")


@pytest.mark.parametrize(
    "app",
    [
        create_frontend_app(),
        create_full_app(DebugSettings(), production_settings=SimpleNamespace()),
    ],
    ids=["frontend", "full"],
)
def test_debug_apps_expose_batch_routes(app):
    paths = {route.path for route in app.routes}

    assert "/debug/batch/datasets" in paths
    assert "/debug/batch/runs" in paths
    assert "/debug/batch/runs/{run_id}/samples/{sample_id}" in paths
