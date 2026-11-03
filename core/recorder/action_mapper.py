from __future__ import annotations

import re
from urllib.parse import urlparse

from core.storage.models import CapturedRequest

# Heuristic URL patterns mapped to human-readable action labels
# Each entry: (method, url_regex, label)
URL_HEURISTICS: list[tuple[str, re.Pattern[str], str]] = [
    ("POST", re.compile(r"/login", re.IGNORECASE), "submit login form"),
    ("POST", re.compile(r"/logout", re.IGNORECASE), "submit logout"),
    ("POST", re.compile(r"/sign[-_]?in", re.IGNORECASE), "submit sign-in form"),
    ("POST", re.compile(r"/sign[-_]?up", re.IGNORECASE), "submit sign-up form"),
    ("POST", re.compile(r"/register", re.IGNORECASE), "submit registration form"),
    ("POST", re.compile(r"/auth", re.IGNORECASE), "authenticate"),
    ("POST", re.compile(r"/tokens?", re.IGNORECASE), "request auth token"),
    ("POST", re.compile(r"/password", re.IGNORECASE), "change password"),
    ("POST", re.compile(r"/reset", re.IGNORECASE), "submit password reset"),
    ("POST", re.compile(r"/search", re.IGNORECASE), "submit search"),
    ("GET", re.compile(r"/search", re.IGNORECASE), "search"),
    ("POST", re.compile(r"/cards?", re.IGNORECASE), "create card"),
    ("POST", re.compile(r"/boards?", re.IGNORECASE), "create board"),
    ("POST", re.compile(r"/lists?", re.IGNORECASE), "create list"),
    ("POST", re.compile(r"/tasks?", re.IGNORECASE), "create task"),
    ("POST", re.compile(r"/issues?", re.IGNORECASE), "create issue"),
    ("POST", re.compile(r"/comments?", re.IGNORECASE), "post comment"),
    ("POST", re.compile(r"/messages?", re.IGNORECASE), "send message"),
    ("POST", re.compile(r"/posts?", re.IGNORECASE), "create post"),
    ("POST", re.compile(r"/upload", re.IGNORECASE), "upload file"),
    ("POST", re.compile(r"/import", re.IGNORECASE), "import data"),
    ("POST", re.compile(r"/export", re.IGNORECASE), "export data"),
    ("PUT", re.compile(r"/.+", re.IGNORECASE), "update resource"),
    ("PATCH", re.compile(r"/.+", re.IGNORECASE), "update resource"),
    ("DELETE", re.compile(r"/cards?/", re.IGNORECASE), "delete card"),
    ("DELETE", re.compile(r"/tasks?/", re.IGNORECASE), "delete task"),
    ("DELETE", re.compile(r"/items?/", re.IGNORECASE), "delete item"),
    ("DELETE", re.compile(r"/.+", re.IGNORECASE), "delete resource"),
    ("GET", re.compile(r"/users?/me", re.IGNORECASE), "get current user"),
    ("GET", re.compile(r"/profile", re.IGNORECASE), "get profile"),
    ("GET", re.compile(r"/settings", re.IGNORECASE), "get settings"),
    ("GET", re.compile(r"/dashboard", re.IGNORECASE), "get dashboard"),
    ("GET", re.compile(r"/notifications?", re.IGNORECASE), "get notifications"),
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
    if re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', segment, re.IGNORECASE):
        return True
    return bool(re.match(r'^[0-9a-f]{8,}$', segment, re.IGNORECASE))
