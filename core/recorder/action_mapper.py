from __future__ import annotations

import re
from urllib.parse import urlparse

from core.storage.models import CapturedRequest

# Heuristic URL patterns mapped to human-readable action labels
# Each entry: (method, url_regex, label)
URL_HEURISTICS: list[tuple[str, re.Pattern[str], str]] = [
    ("POST", re.compile(r"/login", re.I), "submit login form"),
    ("POST", re.compile(r"/logout", re.I), "submit logout"),
    ("POST", re.compile(r"/sign[-_]?in", re.I), "submit sign-in form"),
    ("POST", re.compile(r"/sign[-_]?up", re.I), "submit sign-up form"),
    ("POST", re.compile(r"/register", re.I), "submit registration form"),
    ("POST", re.compile(r"/auth", re.I), "authenticate"),
    ("POST", re.compile(r"/tokens?", re.I), "request auth token"),
    ("POST", re.compile(r"/password", re.I), "change password"),
    ("POST", re.compile(r"/reset", re.I), "submit password reset"),
    ("POST", re.compile(r"/search", re.I), "submit search"),
    ("GET", re.compile(r"/search", re.I), "search"),
    ("POST", re.compile(r"/cards?", re.I), "create card"),
    ("POST", re.compile(r"/boards?", re.I), "create board"),
    ("POST", re.compile(r"/lists?", re.I), "create list"),
    ("POST", re.compile(r"/tasks?", re.I), "create task"),
    ("POST", re.compile(r"/issues?", re.I), "create issue"),
    ("POST", re.compile(r"/comments?", re.I), "post comment"),
    ("POST", re.compile(r"/messages?", re.I), "send message"),
    ("POST", re.compile(r"/posts?", re.I), "create post"),
    ("POST", re.compile(r"/upload", re.I), "upload file"),
    ("POST", re.compile(r"/import", re.I), "import data"),
    ("POST", re.compile(r"/export", re.I), "export data"),
    ("PUT", re.compile(r"/.+", re.I), "update resource"),
    ("PATCH", re.compile(r"/.+", re.I), "update resource"),
    ("DELETE", re.compile(r"/cards?/", re.I), "delete card"),
    ("DELETE", re.compile(r"/tasks?/", re.I), "delete task"),
    ("DELETE", re.compile(r"/items?/", re.I), "delete item"),
    ("DELETE", re.compile(r"/.+", re.I), "delete resource"),
    ("GET", re.compile(r"/users?/me", re.I), "get current user"),
    ("GET", re.compile(r"/profile", re.I), "get profile"),
    ("GET", re.compile(r"/settings", re.I), "get settings"),
    ("GET", re.compile(r"/dashboard", re.I), "get dashboard"),
    ("GET", re.compile(r"/notifications?", re.I), "get notifications"),
]


class ActionMapper:
    """Maps captured HTTP requests to human-readable action labels."""

    def __init__(self) -> None:
        self._pending_label: str | None = None

    def record_action(self, label: str) -> None:
        """Manually set an action label (called when user presses a hotkey)."""
        self._pending_label = label

    def consume_pending_label(self) -> str | None:
        """Pop and return any manually set label."""
        label = self._pending_label
        self._pending_label = None
        return label

    def infer_from_request(
        self,
        request: CapturedRequest,
        dom_before: str | None = None,
    ) -> str:
        """
        Heuristically infer a human-readable action label from method + URL + DOM context.
        Falls back to a generic description if nothing matches.
        """
        method = request.method.upper()
        path = urlparse(request.url).path

        for req_method, pattern, label in URL_HEURISTICS:
            if method == req_method and pattern.search(path):
                return label

        # Generic fallback labels based on method
        fallback_labels = {
            "GET": f"fetch {_path_resource(path)}",
            "POST": f"create {_path_resource(path)}",
            "PUT": f"replace {_path_resource(path)}",
            "PATCH": f"update {_path_resource(path)}",
            "DELETE": f"delete {_path_resource(path)}",
        }
        return fallback_labels.get(method, f"{method} {path}")

    def annotate(
        self,
        requests: list[CapturedRequest],
        dom_snapshots: dict[str, str] | None = None,
    ) -> list[CapturedRequest]:
        """
        Annotate a list of requests with inferred action labels where missing.
        dom_snapshots: mapping of request id -> DOM snapshot captured before that request.
        """
        dom_snapshots = dom_snapshots or {}
        for req in requests:
            if req.action_label is None:
                dom_ctx = dom_snapshots.get(req.id)
                req.action_label = self.infer_from_request(req, dom_before=dom_ctx)
        return requests


def _path_resource(path: str) -> str:
    """Extract the last meaningful path segment as a resource name."""
    parts = [p for p in path.strip("/").split("/") if p and not _is_id_like(p)]
    if parts:
        return parts[-1].replace("-", " ").replace("_", " ")
    return "resource"


def _is_id_like(segment: str) -> bool:
    """Return True if a URL segment looks like a dynamic ID."""
    if re.match(r'^\d+$', segment):
        return True
    if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', segment, re.I):
        return True
    if re.match(r'^[0-9a-f]{8,}$', segment, re.I):
        return True
    return False
