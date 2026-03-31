from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable

from core.storage.models import GeneratedTool


class ToolLoader:
    """Loads generated tool modules using importlib (never exec/eval)."""

    def __init__(self) -> None:
        # Maps resolved path string -> (mtime, loaded module)
        self._cache: dict[str, tuple[float, ModuleType]] = {}

    def load_tool(self, file_path: Path) -> ModuleType:
        """
        Load (or return cached) the module at *file_path*.

        The cache is invalidated when the file's modification time changes,
        so hot-reloading works automatically during development.
        """
        path_str = str(file_path.resolve())
        mtime = file_path.stat().st_mtime

        if path_str in self._cache:
            cached_mtime, cached_module = self._cache[path_str]
            if cached_mtime == mtime:
                return cached_module

        module_name = f"automcp_generated.{file_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load module from {file_path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        self._cache[path_str] = (mtime, module)
        return module

    def get_tool_function(self, module: ModuleType, tool_name: str) -> Callable:
        """Extract and validate the tool function from a loaded module."""
        fn = getattr(module, tool_name, None)
        if fn is None:
            raise AttributeError(f"Function '{tool_name}' not found in module")
        if not callable(fn):
            raise TypeError(f"'{tool_name}' is not callable")
        return fn

    def load_and_get(self, file_path: Path, tool_name: str) -> Callable:
        """Convenience: load module and return the named function."""
        module = self.load_tool(file_path)
        return self.get_tool_function(module, tool_name)

    def invalidate(self, file_path: Path) -> None:
        """Remove a cached module entry, forcing a fresh load on next access."""
        path_str = str(file_path.resolve())
        self._cache.pop(path_str, None)
        # Also remove from sys.modules
        module_name = f"automcp_generated.{file_path.stem}"
        sys.modules.pop(module_name, None)
