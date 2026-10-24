"""截图与分析结果组装，以及合并截图采集的画廊入口。"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from ..data_analysis.metrics import aggregate
from ..export_names import gallery_name, gallery_title, sample_stem


def artifact_data(result: dict[str, Any], key: str) -> dict[str, Any]:
    for artifact in result.get("artifacts") or []:
        if artifact.get("key") == key and isinstance(artifact.get("data"), dict):
            return artifact.get("data")
    return {}


def _asset_path(
    run_dir: Path,
    run_id: str,
    artifact: dict[str, Any],
    plugin_id: str,
) -> Path | None:
    url = str(artifact.get("url") or "")
    parsed = urlsplit(url)
    prefix = f"/debug/batch/runs/{run_id}/postprocess/"
    path = unquote(parsed.path)
    if parsed.scheme or parsed.netloc or not path.startswith(prefix):
        return None
    tail = path.removeprefix(prefix)
    execution_id, separator, asset = tail.partition("/assets/")
    if not separator or not re.fullmatch(r"[A-Za-z0-9._-]+", execution_id):
        return None
    if not asset.startswith(f"{plugin_id}/"):
        return None
    resolved = (run_dir / "postprocess" / execution_id / "plugins" / asset).resolve()
    root = (run_dir / "postprocess" / execution_id / "plugins" / plugin_id).resolve()
    valid = resolved.is_relative_to(root) and resolved.is_file()
    return resolved if valid else None


def _read_products(run_dir: Path, sample_id: str) -> tuple[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", sample_id):
        return "", ""
    try:
        state = json.loads((run_dir / sample_id / "result.json").read_text(encoding="utf-8"))
        attempt = state.get("finalAttempt")
        if not isinstance(attempt, int) or attempt < 0:
            return "", ""
        directory = run_dir / sample_id / f"attempt_{attempt:03d}"
        blocks = json.loads((directory / "blocks.json").read_text(encoding="utf-8"))
        compact = blocks.get("designcompactdsl") or blocks.get("compactdsl") or ""
        genui = directory / "genui.jsonl"
        a2ui = genui.read_text(encoding="utf-8") if genui.is_file() else blocks.get("genui", "")
        if not isinstance(compact, str):
            compact = json.dumps(compact, ensure_ascii=False, indent=2)
        if not isinstance(a2ui, str):
            a2ui = json.dumps(a2ui, ensure_ascii=False, indent=2)
        return compact, a2ui
    except (OSError, ValueError, AttributeError) as exc:
        return f"产物读取失败：{type(exc).__name__}: {exc}", ""


def compatible_capture(facts: dict[str, Any], a2ui: str) -> bool:
    digest = facts.get("sampleDigest")
    if digest is not None:
        actual = hashlib.sha256(a2ui.encode("utf-8")).hexdigest()
        if digest != actual:
            return False
    if facts.get("captureMode", "preview") == "desktop":
        for key in (
            "sampleDigest",
            "adapterVersion",
            "appVersion",
            "rendererDigest",
            "formId",
            "screen",
        ):
            if not facts.get(key):
                return False
    return True


def build_document(title: str, run_id: str, cases: list[dict[str, Any]]) -> str:
    payload = json.dumps({"runId": run_id, "cases": cases}, ensure_ascii=False, allow_nan=False)
    # JSON script 文本中也必须转义 <，防止用户输入提前结束 script 标签。
    payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028")
    template = (Path(__file__).parent / "templates/report.html").read_text(encoding="utf-8")
    replacements = {
        "{{TITLE}}": html.escape(title),
        "{{CREATED}}": html.escape(datetime.now(UTC).isoformat()),
        "{{DATA}}": payload,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


async def run_gallery(
    manager: Any,
    kind: str,
    run_id: str,
    output_dir: Path,
    _config: dict[str, Any],
    *,
    upstream_results: list[dict[str, Any]],
    capture_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = (manager.output_root / run_id).resolve()
    if not run_dir.is_relative_to(manager.output_root.resolve()):
        raise ValueError("运行目录越界")
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    sources: dict[str, dict[str, Any]] = {}
    analyses: dict[str, dict[str, Any]] = {}
    for stage in upstream_results:
        for sample in stage.get("sampleResults") or []:
            sample_id = str(sample.get("sampleId") or "")
            if stage.get("id") == f"{kind}-capture":
                sources[sample_id] = sample
            elif stage.get("id") == "data-analysis":
                analyses[sample_id] = artifact_data(sample, "analysis")
    if capture_result is not None:
        for sample in capture_result.get("sampleResults") or []:
            sources[str(sample.get("sampleId") or "")] = sample
    cases = []
    results = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for sample in summary.get("samples") or []:
        sample_id = str(sample.get("id") or "")
        captured = sources.get(sample_id, {})
        facts = captured.get("facts") or {}
        if kind == "device":
            metadata = artifact_data(captured, "device-capture-metadata")
            facts = {**facts, **metadata}
        compact, a2ui = _read_products(run_dir, sample_id)
        reusable = compatible_capture(facts, a2ui)
        image = ""
        artifacts = []
        for artifact in captured.get("artifacts") or []:
            if not reusable:
                continue
            if artifact.get("key") != f"{kind}-capture":
                continue
            path = _asset_path(run_dir, run_id, artifact, f"{kind}-capture")
            if capture_result is not None:
                relative = artifact.get("path")
                if isinstance(relative, str):
                    candidate = (output_dir / relative).resolve()
                    if candidate.is_relative_to(output_dir.resolve()) and candidate.is_file():
                        path = candidate
            if path is not None:
                content = path.read_bytes()
                image = "data:image/png;base64," + base64.b64encode(content).decode("ascii")
                # 输出路径不引用其它插件目录；将本次画廊图片保存为自身快照。
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", sample_id):
                    raise ValueError("样本标识无效")
                image_name = f"{sample_stem(sample_id, summary)}.png"
                (output_dir / image_name).write_bytes(content)
                artifacts.append({"key": f"{kind}-capture", "path": image_name})
        analysis = analyses.get(sample_id, {})
        stats = aggregate([analysis]) if analysis else aggregate([])
        cases.append(
            {
                "id": sample_id,
                "title": sample.get("title") or sample_id,
                "query": sample.get("query") or "",
                "size": facts.get("size") or sample.get("size"),
                "status": sample.get("status"),
                "captureStatus": captured.get("status", "skipped"),
                "captureMode": facts.get("captureMode", "preview")
                if kind == "device"
                else "browser",
                "image": image,
                "error": ""
                if image
                else (
                    captured.get("summary")
                    if reusable
                    else "截图与当前产物或渲染证据不匹配，请重新截图"
                )
                or "没有截图记录",
                "warnings": facts.get("warnings") or [],
                "compact": compact,
                "a2ui": a2ui,
                "analysis": analysis,
                "statistics": stats,
            }
        )
        results.append(
            {
                "sampleId": sample_id,
                "status": captured.get("status", "skipped") if capture_result is not None
                else "success" if image else "skipped",
                "summary": "截图与分析已组装" if image else "无截图，画廊保留原因占位",
                "facts": {
                    **(captured.get("facts") or {}),
                    "captureStatus": captured.get("status", "skipped"),
                    "captureMode": facts.get("captureMode", "preview")
                    if kind == "device"
                    else "browser",
                },
                "artifacts": (
                    captured.get("artifacts", []) if capture_result is not None else artifacts
                ),
            }
        )
    title = "浏览器渲染画廊" if kind == "browser" else "真机截图画廊"
    title = gallery_title(title, summary)
    file_name = gallery_name(summary, kind)
    document = build_document(title, run_id, cases)
    temporary = output_dir / f"{file_name}.tmp"
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(output_dir / file_name)
    return {
        "status": (
            capture_result.get("status", "success") if capture_result is not None else "success"
        ),
        "sampleResults": results,
        "datasetResult": {
            "status": "success",
            "summary": f"{title}已生成，共 {len(cases)} 个样本",
            "facts": {"sampleCount": len(cases)},
            "artifacts": [
                {"key": f"{kind}-gallery", "path": file_name, "label": f"打开{title}"},
                *((capture_result or {}).get("datasetResult", {}).get("artifacts") or []),
            ],
        },
    }


async def run_capture_gallery(
    manager: Any,
    kind: str,
    run_id: str,
    output_dir: Path,
    config: dict[str, Any],
    checkpoint: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    *,
    upstream_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """每次画廊执行采集新截图，证据和离线报告保存到同一插件目录。"""
    from ..device_capture import run_builtin
    from .capture import run_capture

    if kind == "device":
        captured = await run_builtin(manager, run_id, output_dir, config, checkpoint=checkpoint)
    else:
        captured = await run_capture(manager, run_id, output_dir, config)
    return await run_gallery(
        manager, kind, run_id, output_dir, config,
        upstream_results=upstream_results, capture_result=captured,
    )
