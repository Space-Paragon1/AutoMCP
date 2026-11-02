"""Name handling for generated code.

Tool specs come from an LLM that reads website traffic, so every name that ends
up in generated Python must be checked first. These helpers turn arbitrary
strings into safe identifiers and check that a spec is safe to render.
"""
from __future__ import annotations

import keyword
import re

from core.storage.models import ToolInput, ToolSpec

# Names the generated module uses itself; an input with one of these names
# would shadow it inside the tool function.
RESERVED = {"SPEC", "execute", "Annotated", "Field", "Any"}

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
ALLOWED_INPUT_TYPES = {"string", "integer", "number", "boolean", "array", "object"}

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_ANY_PLACEHOLDER = re.compile(r"\{([^{}/?&=]+)\}")


def is_safe_identifier(name: str) -> bool:
    return bool(_IDENTIFIER.match(name)) and not keyword.iskeyword(name) and name not in RESERVED


def to_identifier(name: str) -> str:
    """Turn any string into a safe Python identifier, keeping its case."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
    s = re.sub(r"_+", "_", s).strip("_") or "param"
    if s[0].isdigit():
        s = "p_" + s
    if keyword.iskeyword(s) or s in RESERVED:
        s += "_"
    return s


def to_snake_case(name: str) -> str:
    """Turn any string into a safe snake_case identifier (for tool names)."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.strip())
    return to_identifier(s.lower())


def normalize_spec(spec: ToolSpec, taken_names: set[str]) -> ToolSpec:
    """Clean up an LLM-produced spec so it can be rendered safely.

    - tool and input names become safe identifiers (tool names unique in *taken_names*)
    - URL placeholders and request mappings follow any input renames
    - URL placeholders with no matching input get a required string input
    Raises ValueError for specs that can't be fixed (bad method or URL).
    """
    method = spec.method.upper().strip()
    if method not in ALLOWED_METHODS:
        raise ValueError(f"unsupported HTTP method {spec.method!r}")
    if not spec.url_template.startswith(("http://", "https://")):
        raise ValueError(f"URL must be absolute, got {spec.url_template!r}")

    base = to_snake_case(spec.tool_name)
    tool_name, n = base, 2
    while tool_name in taken_names:
        tool_name, n = f"{base}_{n}", n + 1
    taken_names.add(tool_name)

    renames: dict[str, str] = {}
    inputs: list[ToolInput] = []
    used: set[str] = set()
    for inp in spec.inputs:
        new = to_identifier(inp.name)
        while new in used:
            new += "_"
        used.add(new)
        renames[inp.name] = new
        inputs.append(inp.model_copy(update={
            "name": new,
            "type": inp.type if inp.type in ALLOWED_INPUT_TYPES else "string",
        }))

    def _placeholder(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        name = renames.get(raw) or to_identifier(raw)
        if name not in used:
            used.add(name)
            inputs.append(ToolInput(name=name, type="string", required=True,
                                    description=f"Value for '{raw}' in the URL"))
        return "{" + name + "}"

    url_template = _ANY_PLACEHOLDER.sub(_placeholder, spec.url_template)

    mapping: dict[str, object] = {}
    for target, source in spec.request_mapping.items():
        if isinstance(source, str) and source.startswith("$input."):
            raw = source[len("$input."):]
            name = renames.get(raw) or to_identifier(raw)
            if name not in used:
                continue  # Mapping points at an input that doesn't exist
            source = f"$input.{name}"
        mapping[str(target)] = source

    return spec.model_copy(update={
        "tool_name": tool_name,
        "method": method,
        "url_template": url_template,
        "inputs": inputs,
        "request_mapping": mapping,
    })


def check_spec_safe(spec: ToolSpec) -> list[str]:
    """Return problems that would make a spec unsafe or invalid to render."""
    errors: list[str] = []
    if not is_safe_identifier(spec.tool_name):
        errors.append(f"Tool name {spec.tool_name!r} is not a valid identifier")
    if spec.method.upper() not in ALLOWED_METHODS:
        errors.append(f"Unsupported HTTP method {spec.method!r}")
    names = [i.name for i in spec.inputs]
    for name in names:
        if not is_safe_identifier(name):
            errors.append(f"Input name {name!r} is not a valid identifier")
    if len(set(names)) != len(names):
        errors.append("Duplicate input names")
    for match in _ANY_PLACEHOLDER.finditer(spec.url_template):
        if match.group(1) not in names:
            errors.append(f"URL placeholder {{{match.group(1)}}} has no matching input")
    return errors
