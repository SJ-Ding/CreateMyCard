"""批跑增量状态；旧目录不创建数据库，正文仍保存在兼容文件中。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

DATABASE_NAME = "batch_state.sqlite3"


class RunStateStore:
    """由宿主单写入线程调用；读查询使用独立连接和一致性事务。"""

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / DATABASE_NAME
        self._lock = RLock()

    @contextmanager
    def _connection(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        if readonly:
            connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        else:
            connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self, summary: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS samples ("
                "id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, revision INTEGER NOT NULL, "
                "payload TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS sample_revision ON samples(revision);"
                "CREATE INDEX IF NOT EXISTS sample_sequence ON samples(sequence);"
                "CREATE TABLE IF NOT EXISTS collections ("
                "id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL);"
            )
            overview = dict(summary)
            samples = overview.pop("samples", [])
            overview["revision"] = 1
            overview["exportReady"] = False
            overview["storageVersion"] = 2
            overview["generation"] = uuid.uuid4().hex
            self._write_overview(connection, overview)
            for sample in samples:
                self._write_sample(connection, sample, 1)

    @staticmethod
    def _write_overview(connection: sqlite3.Connection, overview: dict[str, Any]) -> None:
        connection.execute(
            "INSERT OR REPLACE INTO metadata VALUES ('summary', ?)",
            (json.dumps(overview, ensure_ascii=False),),
        )

    @staticmethod
    def _write_sample(
        connection: sqlite3.Connection, sample: dict[str, Any], revision: int
    ) -> None:
        payload = {**sample, "revision": revision}
        connection.execute(
            "INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?)",
            (
                sample.get("id"),
                sample.get("sequence", 0),
                revision,
                json.dumps(payload, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _overview(connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute("SELECT value FROM metadata WHERE key='summary'").fetchone()
        if row is None:
            raise ValueError("批跑增量状态缺少运行摘要")
        return json.loads(row[0])

    def update(
        self,
        overview: dict[str, Any],
        sample: dict[str, Any] | None = None,
        job: dict[str, Any] | None = None,
    ) -> int:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = self._overview(connection)
            revision = int(previous.get("revision", 0)) + 1
            saved = {**previous, **overview, "revision": revision}
            saved.pop("samples", None)
            self._write_overview(connection, saved)
            if sample is not None:
                self._write_sample(connection, sample, revision)
            if job is not None:
                connection.execute(
                    "INSERT OR REPLACE INTO collections VALUES (?, ?, ?)",
                    (
                        job.get("id"),
                        job.get("status", "pending"),
                        json.dumps(job, ensure_ascii=False),
                    ),
                )
        return revision

    def read(self, after: int | None = None) -> dict[str, Any]:
        with self._connection(readonly=True) as connection:
            connection.execute("BEGIN")
            overview = self._overview(connection)
            if after is not None and after > overview.get("revision", 0):
                return {**overview, "sampleChanges": [], "resetRequired": True}
            if after is None:
                rows = connection.execute(
                    "SELECT payload FROM samples ORDER BY sequence"
                ).fetchall()
                key = "samples"
            else:
                rows = connection.execute(
                    "SELECT payload FROM samples WHERE revision > ? ORDER BY sequence LIMIT 1001",
                    (after,),
                ).fetchall()
                key = "sampleChanges"
            overview[key] = [json.loads(row[0]) for row in rows]
            if after is not None and len(rows) > 1000:
                overview[key] = []
                overview["resetRequired"] = True
        return overview

    def page(
        self,
        offset: int,
        limit: int,
        status: str = "",
        trace: str = "",
        quality: str = "",
    ) -> dict[str, Any]:
        conditions = []
        values: list[Any] = []
        if status:
            conditions.append("json_extract(payload, '$.status') = ?")
            values.append(status)
        if trace == "warning":
            conditions.append(
                "json_extract(payload, '$.traceStatus') IN ('missing','partial','invalid')"
            )
        elif trace == "available":
            conditions.append("json_extract(payload, '$.traceStatus') = 'complete'")
        if quality in {"first_failed", "before_dsl_failure"}:
            conditions.append("json_extract(payload, '$.generationMetrics.firstDsl') = ?")
            values.append("failed" if quality == "first_failed" else "before_dsl_failure")
        elif quality == "retried":
            conditions.append("json_extract(payload, '$.attemptCount') > 1")
        elif quality == "missing_information":
            conditions.append(
                "json_extract(payload, '$.generationMetrics.missingInformationWarnings') > 0"
            )
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connection(readonly=True) as connection:
            connection.execute("BEGIN")
            overview = self._overview(connection)
            row = connection.execute("SELECT COUNT(*) FROM samples" + where, values).fetchone()
            if row is None:
                raise ValueError("样本分页计数不可用")
            rows = connection.execute(
                "SELECT payload FROM samples" + where + " ORDER BY sequence LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            return {
                "items": [json.loads(item[0]) for item in rows],
                "total": row[0],
                "offset": offset,
                "limit": limit,
                "revision": overview.get("revision"),
            }

    def overview(self) -> dict[str, Any]:
        with self._connection(readonly=True) as connection:
            return self._overview(connection)

    def pending(self) -> list[dict[str, Any]]:
        with self._connection(readonly=True) as connection:
            rows = connection.execute(
                "SELECT payload FROM collections WHERE status='pending' ORDER BY rowid"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]


class RunProgress:
    """每次只替换一个样本的计数贡献，避免进度保存遍历整批样本。"""

    def __init__(self, samples: list[dict[str, Any]]) -> None:
        self.previous: dict[str, dict[str, Any]] = {}
        self.counts = dict.fromkeys(
            ("completed", "success", "degraded", "failed", "cancelled", "traceWarnings"),
            0,
        )
        self.elapsed_sum = 0.0
        for sample in samples:
            self.replace(sample)

    def _apply(self, sample: dict[str, Any], direction: int) -> None:
        status = sample.get("status")
        terminal = status in {"success", "degraded", "failed", "unsupported", "cancelled"}
        for name in ("success", "degraded", "cancelled"):
            self.counts[name] += direction * int(status == name)
        self.counts["completed"] += direction * int(terminal)
        self.counts["failed"] += direction * int(status in {"failed", "unsupported"})
        warning = sample.get("traceStatus") in {"missing", "partial", "invalid"}
        self.counts["traceWarnings"] += direction * int(warning)
        if terminal:
            self.elapsed_sum += direction * float(sample.get("elapsedMs", 0.0))

    def replace(self, sample: dict[str, Any]) -> None:
        sample_id = str(sample.get("id"))
        previous = self.previous.get(sample_id)
        if previous is not None:
            self._apply(previous, -1)
        self._apply(sample, 1)
        self.previous[sample_id] = dict(sample)

    def snapshot(self) -> dict[str, Any]:
        completed = self.counts.get("completed", 0)
        return {
            **self.counts,
            "total": len(self.previous),
            "averageElapsedMs": round(self.elapsed_sum / completed, 2) if completed else 0.0,
        }
