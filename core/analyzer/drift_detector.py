"""Detect when a live API response diverges from the recorded schema."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from core.analyzer.schema_inferrer import infer_schema
from core.auth.replay import build_request_headers
from core.storage.models import ToolSpec


@dataclass
class DriftReport:
    tool_name: str
    has_drift: bool
    added_fields: list[str]
    removed_fields: list[str]
    status_code: int | None = None
    error: str | None = None


def _get_keys(schema: dict, prefix: str = "") -> set[str]:
    """Recursively extract all property paths from a JSON schema."""
    keys: set[str] = set()
    if schema.get("type") == "object":
        for k, v in schema.get("properties", {}).items():
            full = f"{prefix}.{k}" if prefix else k
            keys.add(full)
            keys |= _get_keys(v, full)
    elif schema.get("type") == "array" and "items" in schema:
        keys |= _get_keys(schema["items"], f"{prefix}[]")
    return keys


class DriftDetector:
    async def check(
        self,
        spec: ToolSpec,
        auth_state: dict[str, Any],
    ) -> DriftReport:
        """Replay the recorded sample request and compare its response schema to the recording."""
        def _report(error: str, status: int | None = None) -> DriftReport:
            return DriftReport(
                tool_name=spec.tool_name, has_drift=False,
                added_fields=[], removed_fields=[], status_code=status, error=error,
            )

        if not spec.response_schema:
            return _report("No recorded schema to compare against")

        url = spec.sample_url
        if not url:
            if "{" in spec.url_template:
                return _report("No sample URL recorded (re-run analyze)")
            url = spec.url_template

        headers = build_request_headers(auth_state, url)
        headers.setdefault("accept", "application/json")

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
            if resp.status_code >= 400:
                return _report(f"HTTP {resp.status_code}", resp.status_code)
            live_schema = infer_schema(resp.json())
        except Exception as e:
            return _report(str(e))

        recorded_keys = _get_keys(spec.response_schema)
        live_keys = _get_keys(live_schema)

        added = sorted(live_keys - recorded_keys)
        removed = sorted(recorded_keys - live_keys)

        return DriftReport(
            tool_name=spec.tool_name,
            has_drift=bool(added or removed),
            added_fields=added,
            removed_fields=removed,
            status_code=resp.status_code,
        )
