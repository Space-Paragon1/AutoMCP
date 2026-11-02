"""Turn approved specs into validated tool files (shared by the CLI and the dashboard)."""
from __future__ import annotations

from pathlib import Path

from core.codegen.python_mcp_generator import GenerationError, PythonMcpGenerator
from core.codegen.validator import CodeValidator
from core.config import settings
from core.storage.db import get_db
from core.storage.models import GeneratedTool, ToolSpec


async def generate_tools(
    specs: list[ToolSpec], output_dir: Path | None = None
) -> list[GeneratedTool]:
    """Write, validate and record a tool file for each spec.

    Tool files are named after the tool, so a name already used by a valid
    tool from another spec is refused instead of overwriting that file.
    """
    out_dir = Path(output_dir or settings.generated_tools_dir)
    async with get_db() as db:
        all_tools = await db.get_generated_tools()

    spec_ids = {s.spec_id for s in specs}
    names_elsewhere = {
        t.tool_name for t in all_tools
        if t.spec_id not in spec_ids and t.validation_status == "valid"
    }
    generator = PythonMcpGenerator(output_dir=out_dir)
    validator = CodeValidator()

    records: list[GeneratedTool] = []
    for spec in specs:
        record = GeneratedTool(spec_id=spec.spec_id, tool_name=spec.tool_name, file_path="")
        if spec.tool_name in names_elsewhere:
            record.validation_status = "invalid"
            record.validation_errors = [(
                f"Name '{spec.tool_name}' is already used by a tool from another session; "
                "rename it first"
            )]
        else:
            try:
                path = generator.generate(spec, output_dir=out_dir)
                record.file_path = str(path.resolve())
                result = validator.validate_file(path, spec)
                record.validation_status = "valid" if result.is_valid else "invalid"
                record.validation_errors = result.errors
            except GenerationError as e:
                record.validation_status = "invalid"
                record.validation_errors = [str(e)]
        async with get_db() as db:
            await db.save_generated_tool(record)
        records.append(record)
    return records
