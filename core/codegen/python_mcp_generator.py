from __future__ import annotations

import json
import pprint
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from core.codegen.naming import check_spec_safe
from core.config import settings
from core.storage.models import ToolSpec

# Path to the templates directory (relative to this file)
_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Maps JSON Schema / ToolInput types to Python type annotations
_TYPE_MAP: dict[str, str] = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


class GenerationError(Exception):
    """A spec can't be rendered into a safe, valid tool file."""


def _filter_python_type(type_str: str) -> str:
    """Jinja2 filter: convert JSON schema type name to Python type annotation."""
    return _TYPE_MAP.get(type_str.lower(), "Any")


def _filter_pyliteral(value: object) -> str:
    """Jinja2 filter: render JSON-like data as a readable Python literal."""
    return pprint.pformat(value, width=100, sort_dicts=False)


def _build_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=False,  # Python source, not HTML; values are escaped with pyrepr instead
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["python_type"] = _filter_python_type
    env.filters["pyrepr"] = repr
    env.filters["pyliteral"] = _filter_pyliteral
    return env


def _spec_data(spec: ToolSpec) -> dict:
    """The runtime data a generated tool passes to the executor."""
    data = {
        "tool_name": spec.tool_name,
        "method": spec.method.upper(),
        "url_template": spec.url_template,
        "session_id": spec.session_id,
        "request_mapping": spec.request_mapping,
        "response_type": spec.response_type,
        "body_encoding": spec.body_encoding,
        "headers": spec.headers,
        "csrf_field": spec.csrf_field,
        "csrf_page": spec.csrf_page,
    }
    if spec.graphql is not None:
        data["graphql"] = spec.graphql.model_dump()
    try:
        # Round-trip through strict JSON so only plain literals reach the template
        return json.loads(json.dumps(data, allow_nan=False))
    except (TypeError, ValueError) as e:
        raise GenerationError(f"Spec contains values that can't be rendered: {e}") from e


class PythonMcpGenerator:
    """Renders Jinja2 templates into Python MCP tool files."""

    def __init__(self, output_dir: Path | None = None) -> None:
        self._env = _build_jinja_env()
        self._default_output_dir = output_dir or settings.generated_tools_dir

    def render(self, spec: ToolSpec) -> str:
        """Render the tool source for *spec*. Raises GenerationError for unsafe specs."""
        errors = check_spec_safe(spec)
        if errors:
            raise GenerationError("; ".join(errors))

        # Python needs required parameters before optional ones
        inputs = sorted(spec.inputs, key=lambda i: not i.required)
        endpoint = f"{spec.method.upper()} {spec.url_template}"
        if spec.graphql is not None:
            endpoint += f" (GraphQL {spec.graphql.kind} {spec.graphql.operation})"
        module_doc = f"AutoMCP generated tool: {spec.tool_name}\n\n{spec.purpose}\n\n{endpoint}"
        docstring = f"{spec.purpose}\n\nHTTP: {endpoint}"
        if spec.is_readonly:
            docstring += "\nRead-only: this tool does not change data."

        template = self._env.get_template("tool.py.jinja2")
        return template.render(
            spec=spec,
            inputs=inputs,
            spec_data=_spec_data(spec),
            module_doc=module_doc,
            docstring=docstring,
        )

    def generate(self, spec: ToolSpec, output_dir: Path | None = None) -> Path:
        """Render the tool for *spec* and write it to *output_dir*. Returns the file path."""
        source = self.render(spec)
        out_dir = Path(output_dir or self._default_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        file_path = out_dir / f"{spec.tool_name}.py"
        file_path.write_text(source, encoding="utf-8")
        return file_path
