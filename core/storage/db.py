from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from core.config import settings
from core.storage.models import (
    CapturedRequest,
    EndpointCluster,
    GeneratedTool,
    RecordingSession,
    ToolSpec,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    browser_context_state TEXT NOT NULL DEFAULT '{}',
    request_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS captured_requests (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    request_headers TEXT NOT NULL DEFAULT '{}',
    request_body TEXT,
    response_status INTEGER NOT NULL,
    response_headers TEXT NOT NULL DEFAULT '{}',
    response_body TEXT,
    action_label TEXT,
    dom_context TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS tool_specs (
    spec_id TEXT PRIMARY KEY,
    session_id TEXT,
    tool_name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    method TEXT NOT NULL,
    url_template TEXT NOT NULL,
    auth_strategy TEXT NOT NULL,
    csrf_strategy TEXT,
    inputs TEXT NOT NULL DEFAULT '[]',
    request_mapping TEXT NOT NULL DEFAULT '{}',
    response_type TEXT NOT NULL DEFAULT 'json',
    confidence REAL NOT NULL DEFAULT 0.0,
    quality_score TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generated_tools (
    id TEXT PRIMARY KEY,
    spec_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    validation_status TEXT NOT NULL DEFAULT 'pending',
    validation_errors TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (spec_id) REFERENCES tool_specs(spec_id)
);

CREATE TABLE IF NOT EXISTS endpoint_clusters (
    cluster_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    method TEXT NOT NULL,
    url_template TEXT NOT NULL,
    request_ids TEXT NOT NULL DEFAULT '[]',
    representative_request_id TEXT NOT NULL,
    action_labels TEXT NOT NULL DEFAULT '[]',
    body_schema TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_requests_session ON captured_requests(session_id);
CREATE INDEX IF NOT EXISTS idx_specs_session ON tool_specs(session_id);
CREATE INDEX IF NOT EXISTS idx_tools_spec ON generated_tools(spec_id);
CREATE INDEX IF NOT EXISTS idx_clusters_session ON endpoint_clusters(session_id);
"""


def _dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _dt_str(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


class AsyncDatabase:
    def __init__(self, db_path: Path | str | None = None):
        self._db_path = str(db_path or settings.db_path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> "AsyncDatabase":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Use 'async with db:' or call connect().")
        return self._conn

    # -------------------------------------------------------------------------
    # Sessions
    # -------------------------------------------------------------------------

    async def save_session(self, session: RecordingSession) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO sessions
                (id, url, started_at, ended_at, browser_context_state, request_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.url,
                session.started_at.isoformat(),
                _dt_str(session.ended_at),
                json.dumps(session.browser_context_state),
                session.request_count,
            ),
        )
        await self.conn.commit()

    async def resolve_session_id(self, session_id: str) -> str | None:
        """Resolve a full or partial session ID to the full UUID."""
        async with self.conn.execute(
            "SELECT id FROM sessions WHERE id = ? OR id LIKE ?",
            (session_id, f"{session_id}%"),
        ) as cursor:
            row = await cursor.fetchone()
        return row["id"] if row else None

    async def get_session(self, session_id: str) -> RecordingSession | None:
        full_id = await self.resolve_session_id(session_id)
        if full_id is None:
            return None
        async with self.conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (full_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return RecordingSession(
            id=row["id"],
            url=row["url"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=_dt(row["ended_at"]),
            browser_context_state=json.loads(row["browser_context_state"]),
            request_count=row["request_count"],
        )

    # -------------------------------------------------------------------------
    # Captured Requests
    # -------------------------------------------------------------------------

    async def save_request(self, req: CapturedRequest) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO captured_requests
                (id, session_id, timestamp, method, url, request_headers,
                 request_body, response_status, response_headers, response_body,
                 action_label, dom_context)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                req.id,
                req.session_id,
                req.timestamp.isoformat(),
                req.method,
                req.url,
                json.dumps(req.request_headers),
                req.request_body,
                req.response_status,
                json.dumps(req.response_headers),
                req.response_body,
                req.action_label,
                req.dom_context,
            ),
        )
        await self.conn.commit()

    async def get_requests_for_session(self, session_id: str) -> list[CapturedRequest]:
        full_id = await self.resolve_session_id(session_id) or session_id
        async with self.conn.execute(
            "SELECT * FROM captured_requests WHERE session_id = ? ORDER BY timestamp ASC",
            (full_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            CapturedRequest(
                id=row["id"],
                session_id=row["session_id"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                method=row["method"],
                url=row["url"],
                request_headers=json.loads(row["request_headers"]),
                request_body=row["request_body"],
                response_status=row["response_status"],
                response_headers=json.loads(row["response_headers"]),
                response_body=row["response_body"],
                action_label=row["action_label"],
                dom_context=row["dom_context"],
            )
            for row in rows
        ]

    # -------------------------------------------------------------------------
    # Tool Specs
    # -------------------------------------------------------------------------

    async def save_tool_spec(self, spec: ToolSpec) -> None:
        quality_json = spec.quality_score.model_dump_json() if spec.quality_score else None
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO tool_specs
                (spec_id, session_id, tool_name, purpose, method, url_template,
                 auth_strategy, csrf_strategy, inputs, request_mapping,
                 response_type, confidence, quality_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                spec.spec_id,
                spec.session_id,
                spec.tool_name,
                spec.purpose,
                spec.method,
                spec.url_template,
                spec.auth_strategy,
                spec.csrf_strategy,
                json.dumps([i.model_dump() for i in spec.inputs]),
                json.dumps(spec.request_mapping),
                spec.response_type,
                spec.confidence,
                quality_json,
                spec.created_at.isoformat(),
            ),
        )
        await self.conn.commit()

    async def get_tool_specs(self, session_id: str | None = None) -> list[ToolSpec]:
        if session_id is not None:
            full_id = await self.resolve_session_id(session_id) or session_id
            query = "SELECT * FROM tool_specs WHERE session_id = ? ORDER BY created_at ASC"
            params: tuple = (full_id,)
        else:
            query = "SELECT * FROM tool_specs ORDER BY created_at ASC"
            params = ()

        async with self.conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        specs = []
        for row in rows:
            from core.storage.models import QualityScore, ToolInput

            quality = None
            if row["quality_score"]:
                quality = QualityScore.model_validate_json(row["quality_score"])

            inputs_raw = json.loads(row["inputs"])
            inputs = [ToolInput.model_validate(i) for i in inputs_raw]

            specs.append(
                ToolSpec(
                    spec_id=row["spec_id"],
                    session_id=row["session_id"],
                    tool_name=row["tool_name"],
                    purpose=row["purpose"],
                    method=row["method"],
                    url_template=row["url_template"],
                    auth_strategy=row["auth_strategy"],
                    csrf_strategy=row["csrf_strategy"],
                    inputs=inputs,
                    request_mapping=json.loads(row["request_mapping"]),
                    response_type=row["response_type"],
                    confidence=row["confidence"],
                    quality_score=quality,
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
            )
        return specs

    async def get_tool_spec(self, spec_id: str) -> ToolSpec | None:
        async with self.conn.execute(
            "SELECT * FROM tool_specs WHERE spec_id = ?", (spec_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None

        from core.storage.models import QualityScore, ToolInput

        quality = None
        if row["quality_score"]:
            quality = QualityScore.model_validate_json(row["quality_score"])

        inputs_raw = json.loads(row["inputs"])
        inputs = [ToolInput.model_validate(i) for i in inputs_raw]

        return ToolSpec(
            spec_id=row["spec_id"],
            session_id=row["session_id"],
            tool_name=row["tool_name"],
            purpose=row["purpose"],
            method=row["method"],
            url_template=row["url_template"],
            auth_strategy=row["auth_strategy"],
            csrf_strategy=row["csrf_strategy"],
            inputs=inputs,
            request_mapping=json.loads(row["request_mapping"]),
            response_type=row["response_type"],
            confidence=row["confidence"],
            quality_score=quality,
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # -------------------------------------------------------------------------
    # Generated Tools
    # -------------------------------------------------------------------------

    async def save_generated_tool(self, tool: GeneratedTool) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO generated_tools
                (id, spec_id, tool_name, file_path, generated_at,
                 validation_status, validation_errors)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tool.id,
                tool.spec_id,
                tool.tool_name,
                tool.file_path,
                tool.generated_at.isoformat(),
                tool.validation_status,
                json.dumps(tool.validation_errors),
            ),
        )
        await self.conn.commit()

    async def get_generated_tools(self, session_id: str | None = None) -> list[GeneratedTool]:
        if session_id is not None:
            query = """
                SELECT gt.* FROM generated_tools gt
                JOIN tool_specs ts ON gt.spec_id = ts.spec_id
                WHERE ts.session_id = ?
                ORDER BY gt.generated_at ASC
            """
            params: tuple = (session_id,)
        else:
            query = "SELECT * FROM generated_tools ORDER BY generated_at ASC"
            params = ()

        async with self.conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        return [
            GeneratedTool(
                id=row["id"],
                spec_id=row["spec_id"],
                tool_name=row["tool_name"],
                file_path=row["file_path"],
                generated_at=datetime.fromisoformat(row["generated_at"]),
                validation_status=row["validation_status"],
                validation_errors=json.loads(row["validation_errors"]),
            )
            for row in rows
        ]

    async def update_generated_tool_validation(
        self, tool_id: str, status: str, errors: list[str]
    ) -> None:
        await self.conn.execute(
            "UPDATE generated_tools SET validation_status = ?, validation_errors = ? WHERE id = ?",
            (status, json.dumps(errors), tool_id),
        )
        await self.conn.commit()

    # -------------------------------------------------------------------------
    # Endpoint Clusters
    # -------------------------------------------------------------------------

    async def save_endpoint_cluster(self, cluster: EndpointCluster, session_id: str) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO endpoint_clusters
                (cluster_id, session_id, method, url_template, request_ids,
                 representative_request_id, action_labels, body_schema)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cluster.cluster_id,
                session_id,
                cluster.method,
                cluster.url_template,
                json.dumps(cluster.request_ids),
                cluster.representative_request_id,
                json.dumps(cluster.action_labels),
                json.dumps(cluster.body_schema) if cluster.body_schema else None,
            ),
        )
        await self.conn.commit()

    async def get_clusters_for_session(self, session_id: str) -> list[EndpointCluster]:
        async with self.conn.execute(
            "SELECT * FROM endpoint_clusters WHERE session_id = ? ORDER BY cluster_id ASC",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()

        return [
            EndpointCluster(
                cluster_id=row["cluster_id"],
                method=row["method"],
                url_template=row["url_template"],
                request_ids=json.loads(row["request_ids"]),
                representative_request_id=row["representative_request_id"],
                action_labels=json.loads(row["action_labels"]),
                body_schema=json.loads(row["body_schema"]) if row["body_schema"] else None,
            )
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_db_instance: AsyncDatabase | None = None


def get_db() -> AsyncDatabase:
    """Return the singleton AsyncDatabase instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = AsyncDatabase()
    return _db_instance
