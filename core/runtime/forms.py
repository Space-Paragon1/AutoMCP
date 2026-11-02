"""Read a fresh security token for a classic web form.

Sites built on Rails, Django or Laravel put a one-time token in each form
(`authenticity_token`, `csrfmiddlewaretoken`, `_token`) and reject posts
without a valid one. A recorded token goes stale, and on GitHub each form even
has its own. So before posting, the executor loads the page the form was on
and takes the token from the form that posts to the same address.
"""
from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

_META_NAMES = {"csrf-token", "csrf_token", "_csrf", "csrf-param"}


class _FormTokens(HTMLParser):
    def __init__(self, field: str) -> None:
        super().__init__(convert_charrefs=True)
        self.field = field
        self.by_action: dict[str, str] = {}
        self.loose: str | None = None
        self._action: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self._action = a.get("action", "")
        elif tag == "input" and a.get("name") == self.field and a.get("value"):
            if self._action is not None:
                self.by_action.setdefault(self._action, a["value"])
            self.loose = self.loose or a["value"]
        elif tag == "meta" and a.get("name", "").lower() in _META_NAMES and a.get("content"):
            # Page-wide token (Rails, Laravel): fine wherever forms share one token
            self.loose = self.loose or a["content"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._action = None


def find_form_token(
    html: str, field: str, page_url: str, action_url: str
) -> tuple[str | None, str | None]:
    """Return (token of the form posting to action_url, any token on the page)."""
    parser = _FormTokens(field)
    try:
        parser.feed(html)
    except Exception:
        return None, None
    target = urlparse(action_url).path.rstrip("/")
    for action, value in parser.by_action.items():
        if urlparse(urljoin(page_url, action)).path.rstrip("/") == target:
            return value, parser.loose
    return None, parser.loose
