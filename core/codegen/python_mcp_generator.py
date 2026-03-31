from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.config import settings
from core.storage.db import get_db
from core.storage.models import GeneratedTool, ToolSpec

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


def _filter_python_type(type_str: str) -> str:
    """Jinja2 filter: convert JSON schema type name to Python type annotation."""
    return _TYPE_MAP.get(type_str.lower(), "Any")


def _filter_to_title_case(snake_str: str) -> str:
    """Jinja2 filter: convert snake_case to TitleCase (PascalCase)."""
    return re.sub(r'(?:^|_)([a-z])', lambda m: m.group(1).upper(), snake_str)


def _build_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape([]),  # No HTML escaping for Python source
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["python_type"] = _filter_python_type
    env.filters["to_title_case"] = _filter_to_title_case
    # Also add a simple 'upper' filter for method comparisons in templates
    env.filters["upper"] = str.upper
    env.filters["lower"] = str.lower
    return env


class PythonMcpGenerator:
    """Renders Jinja2 templates into Python MCP tool files."""

    def __init__(self, output_dir: Path | None = None) -> None:
        self._env = _build_jinja_env()
        self._default_output_dir = output_dir or settings.generated_tools_dir

    def generate(self, spec: ToolSpec, output_dir: Path | None = None) -> Path:
        """
        Render the tool template for *spec* and write it to *output_dir*.

        Returns the path of the written file.
        Also persists a GeneratedTool record to the database (fire-and-forget
        via a sync helper to avoid requiring an event loop here).
        """
        out_dir = output_dir or self._default_output_dir
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        template = self._env.get_template("tool.py.jinja2")
        rendered = template.render(spec=spec)

        file_path = out_dir / f"{spec.tool_name}.py"
        file_path.write_text(rendered, encoding="utf-8")

        # Persist GeneratedTool record asynchronously
        import asyncio

        tool_record = GeneratedTool(
            spec_id=spec.spec_id,
            tool_name=spec.tool_name,
            file_path=str(file_path.resolve()),
            generated_at=datetime.utcnow(),
            validation_status="pending",
        )

        # Run DB save in a new event loop if there isn't one running,
        # otherwise schedule it as a coroutine.
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._save_tool_record(tool_record))
        except RuntimeError:
            # No running loop — use asyncio.run
            asyncio.run(self._save_tool_record(tool_record))

        return file_path

    @staticmethod
    async def _save_tool_record(tool_record: GeneratedTool) -> None:
        from core.storage.db import AsyncDatabase
        db = AsyncDatabase()
        async with db:
            await db.save_generated_tool(tool_record)
