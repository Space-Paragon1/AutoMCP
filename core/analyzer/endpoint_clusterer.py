from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from core.storage.models import CapturedRequest, EndpointCluster

# Regex patterns that identify ID-like path segments
UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I
)
INTEGER_PATTERN = re.compile(r'^\d+$')
# Hex IDs (e.g. Mongo/Trello ObjectIds). Must contain a digit so plain words don't match.
HEX_PATTERN = re.compile(r'^(?=.*\d)[0-9a-f]{8,}$', re.I)
# Base58/base62-style IDs: long, and a mix of letters and digits
BASE58_PATTERN = re.compile(r'^(?=.*\d)(?=.*[A-Za-z])[A-HJ-NP-Za-km-z1-9]{20,}$')


def _is_id_like(segment: str) -> bool:
    return bool(
        UUID_PATTERN.match(segment)
        or INTEGER_PATTERN.match(segment)
        or HEX_PATTERN.match(segment)
        or BASE58_PATTERN.match(segment)
    )


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "xes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _normalise_path(path: str) -> str:
    """Replace dynamic ID segments with named placeholders like {board_id}."""
    parts = [p for p in path.strip("/").split("/") if p]
    normalised: list[str] = []
    used: set[str] = set()
    for i, part in enumerate(parts):
        # A short number at the start is an API version (Trello's /1/...), not an ID
        is_version = i == 0 and part.isdigit() and len(part) <= 2
        if is_version or not _is_id_like(part):
            normalised.append(part)
            continue
        # Name the placeholder after the preceding literal segment (/boards/123 -> board_id)
        prev = parts[i - 1] if i > 0 and not normalised[-1].startswith("{") else ""
        stem = re.sub(r"[^a-z0-9]+", "_", _singular(prev.lower())).strip("_")
        name = f"{stem}_id" if stem and not stem[0].isdigit() else "id"
        unique, n = name, 2
        while unique in used:
            unique, n = f"{name}_{n}", n + 1
        used.add(unique)
        normalised.append("{" + unique + "}")
    return "/" + "/".join(normalised)


def _extract_base_url(url: str) -> str:
    """Return scheme + netloc from a URL."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _cluster_key(method: str, url: str) -> str:
    parsed = urlparse(url)
    normalised_path = _normalise_path(parsed.path)
    return f"{method.upper()}:{normalised_path}"


def _infer_body_schema(requests: list[CapturedRequest]) -> dict | None:
    """Build a union schema of all JSON body keys across requests."""
    schema: dict[str, str] = {}
    for req in requests:
        if not req.request_body:
            continue
        try:
            body = json.loads(req.request_body)
            if isinstance(body, dict):
                for key, value in body.items():
                    if key not in schema:
                        schema[key] = type(value).__name__
        except (json.JSONDecodeError, ValueError):
            pass
    return schema if schema else None


def _pick_representative(requests: list[CapturedRequest]) -> CapturedRequest:
    """
    Pick the request with the most complete body + response.
    Prefer requests that have both a non-empty body and a non-empty response.
    """
    def _score(r: CapturedRequest) -> int:
        score = 0
        if r.request_body:
            score += len(r.request_body)
        if r.response_body:
            score += len(r.response_body) // 2
        if r.action_label:
            score += 500
        return score

    return max(requests, key=_score)


class EndpointClusterer:
    """Groups captured requests by method + normalised URL template."""

    def cluster(self, requests: list[CapturedRequest]) -> list[EndpointCluster]:
        """
        Group requests into endpoint clusters.
        Returns one EndpointCluster per unique (method, url_template) combination.
        """
        groups: dict[str, list[CapturedRequest]] = {}

        for req in requests:
            key = _cluster_key(req.method, req.url)
            groups.setdefault(key, []).append(req)

        clusters: list[EndpointCluster] = []
        for key, group_requests in groups.items():
            method, _ = key.split(":", 1)

            # Use the URL template from the first request (they all share the same normalised path)
            parsed = urlparse(group_requests[0].url)
            normalised_path = _normalise_path(parsed.path)
            base_url = _extract_base_url(group_requests[0].url)
            url_template = base_url + normalised_path

            # Collect unique, non-None action labels
            action_labels: list[str] = list(
                dict.fromkeys(
                    r.action_label
                    for r in group_requests
                    if r.action_label is not None
                )
            )

            representative = _pick_representative(group_requests)
            body_schema = _infer_body_schema(group_requests)

            clusters.append(
                EndpointCluster(
                    method=method,
                    url_template=url_template,
                    request_ids=[r.id for r in group_requests],
                    representative_request_id=representative.id,
                    action_labels=action_labels,
                    body_schema=body_schema,
                )
            )

        # Sort clusters: mutations first, then reads
        def _sort_key(c: EndpointCluster) -> int:
            order = {"POST": 0, "PUT": 1, "PATCH": 2, "DELETE": 3, "GET": 4}
            return order.get(c.method.upper(), 5)

        clusters.sort(key=_sort_key)
        return clusters
