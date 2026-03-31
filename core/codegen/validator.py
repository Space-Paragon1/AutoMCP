from __future__ import annotations

import ast
from pathlib import Path

from core.storage.models import ToolSpec, ValidationResult


class CodeValidator:
    """Validates generated tool files using AST analysis (never exec/eval)."""

    def validate_file(self, file_path: Path, spec: ToolSpec | None = None) -> ValidationResult:
        """
        Validate a generated tool file by parsing its AST.

        Checks:
        1. File exists
        2. Valid Python syntax
        3. No dangerous function calls (exec, eval, __import__)
        4. If spec provided: expected function exists with correct parameters
        """
        errors: list[str] = []

        if not file_path.exists():
            return ValidationResult(is_valid=False, errors=[f"File not found: {file_path}"])

        source = file_path.read_text(encoding="utf-8")

        # --- Syntax check ---
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return ValidationResult(is_valid=False, errors=[f"Syntax error: {e}"])

        # --- Dangerous pattern check ---
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in ("exec", "eval", "__import__"):
                    errors.append(f"Dangerous function call: {func.id}()")
                elif isinstance(func, ast.Attribute) and func.attr in ("exec", "eval"):
                    errors.append(f"Dangerous method call: .{func.attr}()")

        # --- Structural check against spec ---
        if spec:
            function_names: set[str] = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            }

            if spec.tool_name not in function_names:
                errors.append(f"Expected function '{spec.tool_name}' not found in file")
            else:
                # Verify all inputs appear as parameters
                all_params: set[str] = set()
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
                        and node.name == spec.tool_name
                    ):
                        all_params = {arg.arg for arg in node.args.args}
                        # Also include keyword-only args
                        all_params |= {arg.arg for arg in node.args.kwonlyargs}

                for inp in spec.inputs:
                    if inp.name not in all_params:
                        errors.append(
                            f"Input '{inp.name}' missing from function parameters"
                        )

        return ValidationResult(is_valid=len(errors) == 0, errors=errors)

    def validate_source(self, source: str, spec: ToolSpec | None = None) -> ValidationResult:
        """Validate Python source code directly (without writing to disk)."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as f:
            f.write(source)
            tmp_path = Path(f.name)

        try:
            return self.validate_file(tmp_path, spec=spec)
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass
