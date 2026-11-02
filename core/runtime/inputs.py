"""Turn typed-in text (CLI prompts, dashboard forms) into tool argument values."""
from __future__ import annotations

import json


def coerce_input(value: str, type_name: str) -> object:
    """Convert text to the input's declared type. Raises ValueError if it doesn't fit."""
    if type_name == "integer":
        return int(value)
    if type_name == "number":
        return float(value)
    if type_name == "boolean":
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "y", "on"):
            return True
        if lowered in ("false", "0", "no", "n", "off"):
            return False
        raise ValueError(f"expected true or false, got {value!r}")
    if type_name in ("array", "object"):
        parsed = json.loads(value)
        expected = list if type_name == "array" else dict
        if not isinstance(parsed, expected):
            raise ValueError(f"expected a JSON {type_name}")
        return parsed
    return value
