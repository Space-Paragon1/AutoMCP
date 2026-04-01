"""Detect when a live API response diverges from the recorded schema."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from core.analyzer.schema_inferrer import infer_schema
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
        cookies: dict[str, str],
        sample_inputs: dict[str, Any] | None = None,
    ) -> DriftReport:
        """Make a live request and compare response schema to recorded schema."""
        if not spec.response_schema:
            return DriftReport(
                tool_name=spec.tool_name,
                has_drift=False,
                added_fields=[],
                removed_fields=[],
                error="No recorded schema to compare against",
            )

        headers: dict[str, str] = {"Accept": "application/json"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    spec.url_template,
                    params=sample_inputs or {},
                    headers=headers,
                    follow_redirects=True,
                )
                if resp.status_code >= 400:
                    return DriftReport(
                        tool_name=spec.tool_name,
                        has_drift=False,
                        added_fields=[],
                        removed_fields=[],
                        status_code=resp.status_code,
                        error=f"HTTP {resp.status_code}",
                    )

                live_data = resp.json()
                live_schema = infer_schema(live_data)

        except Exception as e:
            return DriftReport(
                tool_name=spec.tool_name,
                has_drift=False,
                added_fields=[],
                removed_fields=[],
                error=str(e),
            )

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
