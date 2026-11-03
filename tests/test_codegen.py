import ast

import pytest

from core.codegen.naming import check_spec_safe, normalize_spec, to_identifier, to_snake_case
from core.codegen.python_mcp_generator import GenerationError, PythonMcpGenerator
from core.codegen.validator import CodeValidator
from core.storage.models import ToolInput, ToolSpec


def _spec(**kw):
    base = {
        "tool_name": "get_board", "purpose": "Get a board", "method": "GET",
        "url_template": "https://api.example.com/boards/{board_id}", "auth_strategy": "cookies",
        "inputs": [ToolInput(name="board_id")], "session_id": "sess",
    }
    base.update(kw)
    return ToolSpec(**base)


def test_names_become_safe_identifiers():
    assert to_snake_case("getBoardCards") == "get_board_cards"
    assert to_identifier("board-id") == "board_id"
    assert to_identifier("from") == "from_"
    assert to_identifier("2fa") == "p_2fa"
    assert to_identifier("execute") == "execute_"  # would shadow the runtime helper


def test_normalize_follows_renames_and_adds_missing_url_inputs():
    spec = normalize_spec(_spec(
        tool_name="getBoard",
        url_template="https://api.example.com/boards/{board-id}/cards/{cardId}",
        inputs=[ToolInput(name="board-id"), ToolInput(name="from", required=False)],
        request_mapping={"from": "$input.from", "pos": "top", "x": "$input.nope"},
    ), set())
    assert spec.tool_name == "get_board"
    assert spec.url_template == "https://api.example.com/boards/{board_id}/cards/{cardId}"
    assert [i.name for i in spec.inputs] == ["board_id", "from_", "cardId"]
    assert spec.request_mapping == {"from": "$input.from_", "pos": "top"}
    assert check_spec_safe(spec) == []


def test_normalize_rejects_unusable_specs():
    with pytest.raises(ValueError):
        normalize_spec(_spec(method="CONNECT"), set())
    with pytest.raises(ValueError):
        normalize_spec(_spec(url_template="/relative/path"), set())


def test_generated_file_is_valid_and_minimal(tmp_path):
    spec = normalize_spec(_spec(
        inputs=[ToolInput(name="board_id"), ToolInput(name="limit", type="integer", required=False)],
    ), set())
    path = PythonMcpGenerator().generate(spec, tmp_path)
    result = CodeValidator().validate_file(path, spec)
    assert result.is_valid, result.errors

    tree = ast.parse(path.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef))
    assert [a.arg for a in fn.args.args] == ["board_id", "limit"]  # no _cookies/_headers


def test_llm_text_cannot_inject_code(tmp_path):
    evil = 'x"""\nimport os; os.system("echo pwned")\n"""'
    spec = normalize_spec(_spec(
        purpose=evil,
        url_template='https://api.example.com/b/{id}"); import os; ("',
        inputs=[ToolInput(name="id", description="'''" + evil)],
        request_mapping={'k"]=1;import os#': "$input.id"},
    ), set())
    source = PythonMcpGenerator().render(spec)
    tree = ast.parse(source)

    # Only: docstring, 4 imports, SPEC assignment, the tool function
    kinds = [type(n).__name__ for n in tree.body]
    assert kinds == ["Expr", "ImportFrom", "ImportFrom", "ImportFrom", "ImportFrom",
                     "Assign", "AsyncFunctionDef"]
    assert not any(isinstance(n, ast.Import) for n in ast.walk(tree))
    calls = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert calls <= {"Field", "execute"}


def test_unsafe_names_refuse_to_render():
    with pytest.raises(GenerationError):
        PythonMcpGenerator().render(_spec(tool_name="../../evil"))
    with pytest.raises(GenerationError):
        PythonMcpGenerator().render(_spec(inputs=[ToolInput(name="a=__import__('os')")]))


def test_validator_rejects_unexpected_imports(tmp_path):
    path = tmp_path / "get_board.py"
    path.write_text("import os\nasync def get_board(board_id):\n    pass\n")
    result = CodeValidator().validate_file(path, _spec())
    assert not result.is_valid
    assert any("Unexpected import" in e for e in result.errors)
