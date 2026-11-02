from apps.cli import test_tool
from core.storage.models import ToolInput


def _answer(monkeypatch, *answers):
    replies = iter(answers)
    monkeypatch.setattr(test_tool.Prompt, "ask", lambda *a, **kw: next(replies))


def test_ask_inputs_repeats_until_required_value_given(monkeypatch):
    _answer(monkeypatch, "", "  ", "pikachu")
    assert test_tool._ask_inputs([ToolInput(name="name_or_id")]) == {"name_or_id": "pikachu"}


def test_ask_inputs_repeats_on_invalid_value_and_skips_empty_optional(monkeypatch):
    _answer(monkeypatch, "abc", "25", "")
    inputs = [
        ToolInput(name="limit", type="integer"),
        ToolInput(name="offset", type="integer", required=False),
    ]
    assert test_tool._ask_inputs(inputs) == {"limit": 25}
