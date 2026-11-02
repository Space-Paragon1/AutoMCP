"""Keep tool results small enough for an AI agent's context window.

A single API response can be hundreds of kilobytes (pokeapi's Pikachu record
is ~300 KB, roughly 80k tokens). Results over the limit are shortened to fit:
the space is shared out so small fields stay whole, while long lists keep
only their first items, long text is cut and deeply nested data is
summarized. The result stays valid JSON, and a note tells the agent what was
left out so it can ask more narrowly. HTML pages are reduced to their text.
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

# Below this many characters a list or object is replaced by a short summary
_MIN_CONTAINER = 40
_NOTE_ROOM = 450


def json_size(value: Any) -> int:
    """Characters in the compact JSON an MCP client receives."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _share(budget: float, sizes: list[int]) -> list[int]:
    """Split a budget so small parts keep everything and big parts split the rest evenly."""
    alloc = [0] * len(sizes)
    remaining = budget
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    for pos, i in enumerate(order):
        give = min(sizes[i], remaining / (len(sizes) - pos))
        alloc[i] = int(give)
        remaining -= give
    return alloc


def _fit(value: Any, budget: int) -> Any:
    """Shrink value until its JSON is about budget characters or less."""
    if json_size(value) <= budget:
        return value
    if isinstance(value, str):
        return value[:max(budget - 20, 10)] + f"…({len(value)} chars)"

    if isinstance(value, dict):
        if budget < _MIN_CONTAINER:
            return f"{{…{len(value)} fields}}"
        fields = list(value.items())
        overhead = sum(len(json.dumps(k, ensure_ascii=False)) + 2 for k, _ in fields) + 2
        dropped = 0
        if overhead > budget // 2:
            # Thousands of fields (e.g. an object keyed by ID): keep the first ones
            kept, used = [], 30
            for k, v in fields:
                cost = len(json.dumps(k, ensure_ascii=False)) + 2
                if used + cost > budget // 2:
                    break
                kept.append((k, v))
                used += cost
            dropped, fields, overhead = len(fields) - len(kept), kept, used
        shares = _share(budget - overhead, [json_size(v) for _, v in fields])
        out = {k: _fit(v, share) for (k, v), share in zip(fields, shares)}
        if dropped:
            out["…"] = f"{dropped} more fields"
        return out

    if isinstance(value, list):
        if budget < _MIN_CONTAINER:
            return f"[…{len(value)} items]"
        average = json_size(value) / len(value)
        # As many whole items as fit, but at least the first three (shortened if needed)
        keep = min(len(value), max(min(3, len(value)), int(budget // max(average, 1))))
        marker = 30 if keep < len(value) else 0
        per_item = max((budget - marker) // keep - 1, 1)
        out = [_fit(v, per_item) for v in value[:keep]]
        if keep < len(value):
            out.append(f"…{len(value) - keep} more items")
        return out

    return value  # Numbers, booleans and null are already small


def trim_json(value: Any, max_chars: int) -> Any:
    """Return value unchanged if it fits in max_chars, else a shortened copy with a note."""
    if max_chars <= 0:
        return value
    full = json_size(value)
    if full <= max_chars:
        return value

    target = max_chars - _NOTE_ROOM
    for _ in range(10):
        if target < _MIN_CONTAINER:
            break
        trimmed = _fit(value, target)
        size = json_size(trimmed)
        if size <= max_chars - _NOTE_ROOM:
            note = (
                f"AutoMCP shortened this response from {full:,} to {size:,} characters to "
                "fit the AI's context. Long lists show only their first items, long text "
                "is cut, and deeply nested data is summarized (all marked with …). To see "
                "more, call the tool with narrower inputs, like a filter, a smaller page "
                "size or a specific ID."
            )
            return {"result": trimmed, "automcp_note": note}
        target = int(target * 0.8)  # Summary markers took more room than planned

    return trim_text(json.dumps(value, ensure_ascii=False, default=str), max_chars)


def trim_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + f"\n…[AutoMCP cut this response at {max_chars:,} of {len(text):,} characters]"
    )


class _TextExtractor(HTMLParser):
    """Collect the readable text of an HTML page, skipping scripts and styling."""

    # Never readable, or site menus repeated on every page
    SKIP = frozenset({"script", "style", "noscript", "svg", "template", "head", "nav"})
    BLOCK = frozenset({"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "main", "aside", "header", "footer", "ul", "ol", "dl",
             "dt", "dd", "table", "form", "blockquote", "pre", "figcaption"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Turn an HTML page into its readable text (a page is mostly markup and scripts)."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return html
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    title = parser.title.strip()
    return f"{title}\n\n{text}" if title and not text.startswith(title) else text
