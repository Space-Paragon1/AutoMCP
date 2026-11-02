from __future__ import annotations

from pathlib import Path
from typing import Callable

from rich.console import Console

from core.runtime.tool_loader import ToolLoader
from core.storage.models import GeneratedTool, ToolSpec

# stderr: the MCP stdio transport uses stdout
console = Console(stderr=True)


class ToolRegistry:
    """In-memory registry of loaded MCP tool functions and their specs."""

    def __init__(self) -> None:
        # Maps tool_name -> (callable, ToolSpec | None)
        self._tools: dict[str, tuple[Callable, ToolSpec | None]] = {}
        self._loader = ToolLoader()

    def register(self, tool_name: str, fn: Callable, spec: ToolSpec) -> None:
        """Register a tool function with its spec."""
        self._tools[tool_name] = (fn, spec)

    def unregister(self, tool_name: str) -> None:
        """Remove a tool from the registry."""
        self._tools.pop(tool_name, None)

    def names(self) -> list[str]:
        return list(self._tools)

    def list_tools(self) -> list[ToolSpec]:
        """Return all registered tool specs (filters out None specs)."""
        return [spec for _, spec in self._tools.values() if spec is not None]

    def get(self, tool_name: str) -> Callable | None:
        """Return the callable for *tool_name*, or None if not registered."""
        entry = self._tools.get(tool_name)
        return entry[0] if entry else None

    def get_spec(self, tool_name: str) -> ToolSpec | None:
        """Return the ToolSpec for *tool_name*, or None."""
        entry = self._tools.get(tool_name)
        return entry[1] if entry else None

    async def load_all(
        self,
        generated_tools: list[GeneratedTool],
        specs: dict[str, ToolSpec] | None = None,
    ) -> int:
        """
        Load all tools whose validation_status is "valid".

        When two records share a tool name, the most recently generated wins.
        Returns the number of successfully loaded tools.
        """
        specs = specs or {}
        latest: dict[str, GeneratedTool] = {}
        for record in sorted(generated_tools, key=lambda t: t.generated_at):
            if record.validation_status != "valid":
                console.print(
                    f"[dim]Skipping {record.tool_name} (status: {record.validation_status})[/]"
                )
                continue
            latest[record.tool_name] = record

        loaded = 0
        for tool_record in latest.values():
            file_path = Path(tool_record.file_path)
            if not file_path.exists():
                console.print(f"[yellow]Warning:[/] Tool file not found: {file_path}")
                continue

            try:
                fn = self._loader.load_and_get(file_path, tool_record.tool_name)
                self._tools[tool_record.tool_name] = (fn, specs.get(tool_record.spec_id))
                loaded += 1
            except Exception as e:
                console.print(
                    f"[yellow]Warning:[/] Failed to load {tool_record.tool_name}: {e}"
                )

        return loaded

    def reload_tool(self, tool_record: GeneratedTool) -> bool:
        """
        Hot-reload a single tool from its file.
        Returns True on success, False on failure.
        """
        file_path = Path(tool_record.file_path)
        self._loader.invalidate(file_path)
        try:
            fn = self._loader.load_and_get(file_path, tool_record.tool_name)
            existing_spec = self.get_spec(tool_record.tool_name)
            self._tools[tool_record.tool_name] = (fn, existing_spec)
            return True
        except Exception as e:
            console.print(f"[red]Reload failed for {tool_record.tool_name}:[/] {e}")
            return False

    @property
    def count(self) -> int:
        """Number of registered tools."""
        return len(self._tools)
