"""Infer JSON Schema from a sample of response bodies."""
from __future__ import annotations

import json
from typing import Any


def infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def infer_schema(value: Any) -> dict:
    """Recursively infer a JSON Schema dict from a value."""
    if isinstance(value, dict):
        properties = {k: infer_schema(v) for k, v in value.items()}
        return {
            "type": "object",
            "properties": properties,
        }
    if isinstance(value, list):
        if value:
            return {"type": "array", "items": infer_schema(value[0])}
        return {"type": "array"}
    return {"type": infer_type(value)}


def merge_schemas(schemas: list[dict]) -> dict:
    """Merge multiple inferred schemas into one (union of properties)."""
    if not schemas:
        return {}
    if len(schemas) == 1:
        return schemas[0]

    base = schemas[0].copy()
    for schema in schemas[1:]:
        if base.get("type") == "object" and schema.get("type") == "object":
            for k, v in schema.get("properties", {}).items():
                if k not in base.setdefault("properties", {}):
                    base["properties"][k] = v
    return base


class SchemaInferrer:
    def infer_from_responses(self, response_bodies: list[str | None]) -> dict | None:
        """Infer a JSON Schema from a list of response body strings."""
        schemas = []
        for body in response_bodies:
            if not body:
                continue
            try:
                parsed = json.loads(body)
                schemas.append(infer_schema(parsed))
            except (json.JSONDecodeError, ValueError):
                continue
        if not schemas:
            return None
        return merge_schemas(schemas)
