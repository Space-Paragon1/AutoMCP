from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import urlparse

from core.analyzer import graphql
from core.storage.models import CapturedRequest, EndpointCluster, GraphQLInfo

# Regex patterns that identify ID-like path segments
UUID_PATTERN = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)
INTEGER_PATTERN = re.compile(r'^\d+$')
# Hex IDs (e.g. Mongo/Trello ObjectIds). Must contain a digit so plain words don't match.
HEX_PATTERN = re.compile(r'^(?=.*\d)[0-9a-f]{8,}$', re.IGNORECASE)
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


# (method, scheme://host, path segments)
ClusterKey = tuple[str, str, tuple[str, ...]]


def _cluster_key(method: str, url: str) -> ClusterKey:
    segments = tuple(p for p in _normalise_path(urlparse(url).path).split("/") if p)
    return method.upper(), _extract_base_url(url), segments


def _looks_like_name(segment: str) -> bool:
    """Names (AutoMCP, Resume-Analyzer, team42) rather than route words
    (issues, branch-infobar) or API versions (v2)."""
    if re.fullmatch(r"v\d+(\.\d+)*", segment, re.IGNORECASE):
        return False
    return bool(re.search(r"[A-Z0-9]", segment))


def _merge_variable_segments(
    groups: dict[ClusterKey, list[CapturedRequest]],
) -> dict[ClusterKey, list[CapturedRequest]]:
    """Turn path segments that vary between otherwise identical URLs into placeholders.

    IDs are caught by _normalise_path, but names aren't: /me/AutoMCP/star and
    /me/Other-Repo/star are one "star a repo" endpoint. A segment counts as
    variable when it sits between identical parts with different values: two
    or more names/slugs, or four or more plain words. The last segment is never
    merged, since /pull/2/files and /pull/2/commits are different endpoints.
    """
    while True:
        buckets: dict[tuple, list[ClusterKey]] = defaultdict(list)
        for key in groups:
            method, base, segs = key
            for pos in range(len(segs) - 1):
                if not segs[pos].startswith("{"):
                    buckets[(method, base, segs[:pos], segs[pos + 1:])].append(key)

        merged_any = False
        for (method, base, prefix, suffix), keys in buckets.items():
            values = {key[2][len(prefix)] for key in keys}
            if len(values) < 2:
                continue
            if len(values) < 4 and not all(_looks_like_name(v) for v in values):
                continue
            # Name it after a plural collection before it (/repos/{repo}), else {name}
            prev = prefix[-1] if prefix else ""
            is_collection = prev.endswith("s") and prev.isalpha() and prev.islower()
            stem = prev if is_collection else ""
            name = re.sub(r"[^a-z0-9]+", "_", _singular(stem.lower())).strip("_") or "name"
            taken = {seg.strip("{}") for seg in prefix + suffix if seg.startswith("{")}
            unique, n = name, 2
            while unique in taken:
                unique, n = f"{name}_{n}", n + 1
            new_key = (method, base, prefix + ("{" + unique + "}",) + suffix)
            merged = [req for key in keys for req in groups.pop(key)]
            groups.setdefault(new_key, []).extend(merged)
            merged_any = True
            break  # Keys changed; rebuild the buckets
        if not merged_any:
            return groups


def _infer_body_schema(requests: list[CapturedRequest]) -> dict | None:
    """Build a union schema of all JSON body keys across requests.

    For GraphQL calls this is the operation's variables, the part that changes.
    """
    schema: dict[str, str] = {}
    for req in requests:
        ops = graphql.operations(req)
        if ops:
            bodies = [op.payload.get("variables") for op in ops]
        else:
            try:
                bodies = [json.loads(req.request_body)] if req.request_body else []
            except (json.JSONDecodeError, ValueError):
                bodies = []
        for body in bodies:
            if isinstance(body, dict):
                for key, value in body.items():
                    if key not in schema:
                        schema[key] = type(value).__name__
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
        groups: dict[ClusterKey, list[CapturedRequest]] = {}
        # GraphQL calls share one URL, so they group by operation instead.
        # Batches should be split first (graphql.split_batches); one that
        # wasn't is grouped under all of its operations' names together.
        graphql_groups: dict[tuple[str, str, GraphQLInfo], list[CapturedRequest]] = {}

        for req in requests:
            ops = graphql.operations(req)
            if ops:
                url = urlparse(req.url)._replace(query="", fragment="").geturl()
                info = ops[0].info if len(ops) == 1 else GraphQLInfo(
                    operation="+".join(op.info.operation for op in ops), batched=True,
                )
                graphql_groups.setdefault((req.method.upper(), url, info), []).append(req)
                continue
            key = _cluster_key(req.method, req.url)
            groups.setdefault(key, []).append(req)
        groups = _merge_variable_segments(groups)

        flat: list[tuple[str, str, GraphQLInfo | None, list[CapturedRequest]]] = [
            (method, base_url + "/" + "/".join(segments), None, group_requests)
            for (method, base_url, segments), group_requests in groups.items()
        ] + [
            (method, url, info, group_requests)
            for (method, url, info), group_requests in graphql_groups.items()
        ]

        clusters: list[EndpointCluster] = []
        for method, url_template, info, group_requests in flat:

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
                    graphql=info,
                )
            )

        # Sort clusters: mutations first, then reads
        def _sort_key(c: EndpointCluster) -> int:
            order = {"POST": 0, "PUT": 1, "PATCH": 2, "DELETE": 3, "GET": 4}
            return order.get(c.method.upper(), 5)

        clusters.sort(key=_sort_key)
        return clusters
