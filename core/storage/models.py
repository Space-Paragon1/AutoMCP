from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (the format already stored in the database)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ToolInput(BaseModel):
    name: str
    type: str = "string"
    required: bool = True
    description: str = ""


class QualityScore(BaseModel):
    usefulness: float
    stability: float
    side_effect_risk: float

    @computed_field  # type: ignore[misc]
    @property
    def composite(self) -> float:
        return (
            self.usefulness * 0.4
            + self.stability * 0.3
            + (1.0 - self.side_effect_risk) * 0.3
        )


class ToolSpec(BaseModel):
    tool_name: str
    purpose: str
    method: str
    url_template: str
    auth_strategy: Literal["cookies", "bearer", "api_key", "none"]
    csrf_strategy: str | None = None
    inputs: list[ToolInput] = Field(default_factory=list)
    # Request field -> "$input.<name>" or a fixed value the browser always sent
    request_mapping: dict[str, Any] = Field(default_factory=dict)
    response_type: Literal["json", "text", "binary"] = "json"
    confidence: float = 0.0
    quality_score: QualityScore | None = None
    session_id: str | None = None
    spec_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=utcnow)
    approved: bool = False
    is_readonly: bool = False
    version: int = 1
    response_schema: dict | None = None
    # A concrete recorded URL for this endpoint (used by drift checks and auth probes)
    sample_url: str | None = None
    # How the request body is encoded: JSON or classic HTML form encoding
    body_encoding: Literal["json", "form"] = "json"


class CapturedRequest(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    method: str
    url: str
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: str | None = None  # base64 if binary
    response_status: int
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: str | None = None
    action_label: str | None = None
    dom_context: str | None = None
    # Playwright resource type: "xhr", "fetch", "document", "script", ...
    resource_type: str | None = None


class RecordingSession(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    url: str
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    browser_context_state: dict = Field(default_factory=dict)
    request_count: int = 0
    project_id: str | None = None


class EndpointCluster(BaseModel):
    cluster_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    method: str
    url_template: str
    request_ids: list[str] = Field(default_factory=list)
    representative_request_id: str
    action_labels: list[str] = Field(default_factory=list)
    body_schema: dict | None = None


class GeneratedTool(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    spec_id: str
    tool_name: str
    file_path: str
    generated_at: datetime = Field(default_factory=utcnow)
    validation_status: Literal["valid", "invalid", "pending"] = "pending"
    validation_errors: list[str] = Field(default_factory=list)
    version: int = 1


class ValidationResult(BaseModel):
    is_valid: bool
    errors: list[str] = Field(default_factory=list)


class Project(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    site_urls: list[str] = Field(default=[])


class ToolExecution(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tool_name: str
    inputs: dict = Field(default={})
    result: str | None = None
    error: str | None = None
    duration_ms: float = 0.0
    executed_at: datetime = Field(default_factory=utcnow)
    success: bool = True
