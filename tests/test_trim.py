import random

import httpx

from core.config import settings
from core.runtime import executor
from core.runtime.trim import html_to_text, json_size, trim_json, trim_text


def _pokemon_like():
    """Shaped like pokeapi's Pikachu: a few small fields and some very long lists."""
    return {
        "id": 25, "name": "pikachu", "weight": 60,
        "types": [{"slot": 1, "type": {"name": "electric"}}],
        "stats": [{"base_stat": 35 + i, "stat": {"name": f"s{i}"}} for i in range(6)],
        "moves": [
            {"move": {"name": f"move-{i}"},
             "details": [{"level": j, "group": {"name": f"g{j}", "url": "x" * 60}} for j in range(20)]}
            for i in range(100)
        ],
        "sprites": {"versions": {f"gen-{g}": {"front": "u" * 80, "back": "u" * 80} for g in range(9)}},
        "flavor": "long text " * 2000,
    }


def test_small_results_are_untouched():
    value = {"id": 1, "items": [1, 2, 3]}
    assert trim_json(value, 20_000) is value
    assert trim_json(_pokemon_like(), 0) == _pokemon_like()  # 0 turns trimming off


def test_large_result_fits_and_keeps_every_field():
    out = trim_json(_pokemon_like(), 20_000)
    assert json_size(out) <= 20_000
    assert "shortened this response" in out["automcp_note"]
    result = out["result"]
    assert list(result) == list(_pokemon_like())  # No top-level field dropped
    assert result["types"] == _pokemon_like()["types"]  # Small fields stay whole
    assert len(result["stats"]) == 6
    assert result["moves"][0]["move"] == {"name": "move-0"}  # First items kept
    assert result["moves"][-1].startswith("…") and "more items" in result["moves"][-1]
    assert "chars)" in result["flavor"]
    assert json_size(out) > 10_000  # Uses the space it has instead of over-cutting


def test_top_level_lists_and_huge_objects_fit():
    rows = [{"id": i, "title": f"Card {i}", "body": "b" * 300} for i in range(2000)]
    out = trim_json(rows, 5_000)
    assert json_size(out) <= 5_000
    assert out["result"][0]["id"] == 0 and "more items" in out["result"][-1]

    by_id = {f"id-{i}": {"name": f"n{i}"} for i in range(5000)}
    out = trim_json(by_id, 5_000)
    assert json_size(out) <= 5_000
    assert "more fields" in out["result"]["…"]


def test_random_shapes_always_fit():
    rng = random.Random(7)

    def make(depth, budget):
        # budget[0] caps the total number of values, so data stays API-sized
        budget[0] -= 1
        kinds = ["dict", "list", "str", "num"] if depth < 5 and budget[0] > 0 else ["str", "num"]
        kind = rng.choice(kinds)
        if kind == "dict":
            return {f"k{i}": make(depth + 1, budget) for i in range(rng.randint(0, 10))}
        if kind == "list":
            return [make(depth + 1, budget) for _ in range(rng.randint(0, 30))]
        if kind == "str":
            return "x" * rng.randint(0, 2000)
        return rng.random()

    for _ in range(60):
        value = make(0, [rng.randint(10, 3000)])
        for limit in (1_000, 5_000, 20_000):
            assert json_size(trim_json(value, limit)) <= limit


def test_text_and_html():
    assert trim_text("a" * 50, 100) == "a" * 50
    assert trim_text("a" * 500, 100).startswith("a" * 100) and "cut this response" in trim_text("a" * 500, 100)
    html = (
        "<html><head><title>Board</title><style>.x{color:red}</style></head><body>"
        "<nav><a>Home</a><a>Settings</a></nav><script>var secret = 1;</script>"
        "<h1>To do</h1><ul><li>Buy&nbsp;milk</li><li>Call Bob</li></ul>"
        "<table><tr><td>Due</td><td>Friday</td></tr></table></body></html>"
    )
    text = html_to_text(html)
    assert text.splitlines() == ["Board", "", "To do", "Buy\xa0milk", "Call Bob", "Due", "Friday"]


async def test_served_tool_results_are_trimmed(monkeypatch):
    monkeypatch.setattr(settings, "max_response_chars", 3_000)
    spec = {"tool_name": "get_cards", "method": "GET", "session_id": None,
            "url_template": "https://x.com/api/cards", "response_type": "json"}
    executor._transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json=[{"id": i, "text": "t" * 200} for i in range(500)])
    )
    result = await executor.execute(spec, {})
    assert json_size(result) <= 3_000 and "automcp_note" in result

    spec = {**spec, "response_type": "text"}
    executor._transport = httpx.MockTransport(lambda r: httpx.Response(
        200, text="<html><body><script>junk()</script><p>Hello</p></body></html>",
        headers={"content-type": "text/html; charset=utf-8"},
    ))
    assert await executor.execute(spec, {}) == "Hello"
