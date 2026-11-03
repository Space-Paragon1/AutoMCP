"""Recognise GraphQL calls in recorded traffic.

GraphQL sites send every action to one address (usually /graphql). What a call
does is named inside the request: an operation name, the query text, or the
hash of a query the server saved earlier (a "persisted query"). A few sites,
such as X, put the operation's name in the address instead. These helpers read
that, so each operation can become its own tool.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlparse

from core.storage.models import CapturedRequest, GraphQLInfo

_KINDS = ("query", "mutation", "subscription")
_TOKEN = re.compile(r'"""|"|#|[A-Za-z_][A-Za-z0-9_]*|[{}()\[\]@]|.', re.DOTALL)


@dataclass
class Operation:
    info: GraphQLInfo
    # The operation as sent: operationName, query, variables, extensions
    payload: dict[str, Any]


def _skip_string(document: str, start: int, quote: str) -> int:
    """Index just past the string literal that opens at *start*."""
    if quote == '"""':
        end = document.find('"""', start + 3)
        return len(document) if end < 0 else end + 3
    i = start + 1
    while i < len(document) and document[i] not in '"\n':
        i += 2 if document[i] == "\\" else 1
    return i + 1


def definitions(document: str) -> list[tuple[str, str | None]] | None:
    """(kind, name) for each operation in a GraphQL document, or None if it isn't one.

    Only top-level words count, so a field called "query" inside braces is
    ignored. Fragments are skipped, and a document that starts with "{" is an
    unnamed query. Plain text such as a search term ("cats") returns None.
    """
    ops: list[list[str | None]] = []
    depth = 0
    header: str | None = None  # "operation" or "fragment" while reading a definition's first line
    want_name = False
    saw_brace = False
    i = 0
    while i < len(document):
        match = _TOKEN.match(document, i)
        token = match.group(0) if match else document[i]
        if token in ('"', '"""'):
            i = _skip_string(document, i, token)
            continue
        if token == "#":
            end = document.find("\n", i)
            i = len(document) if end < 0 else end + 1
            continue
        i += len(token)
        if token in "{([":
            if token == "{" and depth == 0:
                saw_brace = True
                if header is None:
                    ops.append(["query", None])  # { me { name } }
                header, want_name = None, False
            elif depth == 0:
                want_name = False  # Variable list or directive arguments: the name is over
            depth += 1
        elif token in "})]":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0 and (token[0].isalpha() or token[0] == "_"):
            if header is None:
                if token in _KINDS:
                    ops.append([token, None])
                    header, want_name = "operation", True
                elif token == "fragment":
                    header, want_name = "fragment", False
                else:
                    return None  # Not GraphQL
            elif want_name:
                ops[-1][1] = token
                want_name = False
        elif depth == 0 and token == "@":
            want_name = False
    if not ops or not saw_brace or depth != 0:
        return None
    return [(str(kind), name) for kind, name in ops]


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _path_operation(url: str) -> str | None:
    """The operation named in the address, for sites that put it there.

    X (Twitter) calls /i/api/graphql/<queryId>/<OperationName> and sends only
    variables and settings such as "features": no operation name or query text.
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    lowered = [p.lower() for p in parts]
    if "graphql" not in lowered:
        return None
    after = parts[lowered.index("graphql") + 1:]
    if len(after) in (1, 2) and _IDENTIFIER.fullmatch(after[-1]):
        return after[-1]
    return None


def _operation(
    payload: dict[str, Any], batched: bool, path_name: str | None = None, over_get: bool = False,
) -> Operation | None:
    """Read one GraphQL operation, or None if *payload* isn't one.

    *path_name* is an operation named in the address, used when the request
    itself doesn't say which operation it is.
    """
    name = payload.get("operationName")
    name = name if isinstance(name, str) and name else None
    query = payload.get("query")
    extensions = payload.get("extensions")
    persisted = extensions.get("persistedQuery") if isinstance(extensions, dict) else None
    saved_hash = persisted.get("sha256Hash") if isinstance(persisted, dict) else None

    # GraphQL over GET is only allowed to read, so a call whose query text
    # wasn't recorded is still known to be a query
    kind = "query" if over_get else "unknown"
    if isinstance(query, str):
        defs = definitions(query)
        if not defs:
            return None
        chosen = next((d for d in defs if d[1] == name), defs[0])
        kind = chosen[0]
        name = name or chosen[1]
        if not name:
            compact = " ".join(query.split())
            name = "anonymous_" + hashlib.sha256(compact.encode()).hexdigest()[:12]
    elif isinstance(saved_hash, str) and saved_hash:
        name = name or "persisted_" + saved_hash[:12]
    else:
        name = name or path_name
        if not (name and "variables" in payload):
            return None
    return Operation(GraphQLInfo(operation=name, kind=kind, batched=batched), payload)


def operations(request: CapturedRequest) -> list[Operation] | None:
    """The GraphQL operations a recorded request carries, or None if it isn't a GraphQL call."""
    path_name = _path_operation(request.url)
    if request.method.upper() == "GET":
        params = dict(parse_qsl(urlparse(request.url).query, keep_blank_values=True))
        if not {"query", "operationName", "extensions"} & params.keys() and not (
            path_name and "variables" in params
        ):
            return None
        payload: dict[str, Any] = dict(params)
        for key in ("variables", "extensions"):  # Sent as JSON text in the URL
            if key in payload:
                try:
                    payload[key] = json.loads(payload[key])
                except ValueError:
                    return None
        op = _operation(payload, batched=False, path_name=path_name, over_get=True)
        return [op] if op else None

    if not request.request_body:
        return None
    try:
        body = json.loads(request.request_body)
    except ValueError:
        return None
    if isinstance(body, dict):
        op = _operation(body, batched=False, path_name=path_name)
        return [op] if op else None
    if isinstance(body, list) and body and all(isinstance(item, dict) for item in body):
        ops = [_operation(item, batched=True) for item in body]
        if all(ops):
            return ops  # type: ignore[return-value]
    return None


def split_batches(requests: list[CapturedRequest]) -> list[CapturedRequest]:
    """Turn each recorded batch of GraphQL operations into one request per operation.

    Some pages send several operations together as a JSON list. Each part
    becomes its own one-item batch, with its own slice of the response, so
    every operation can become a tool. Other requests pass through unchanged.
    """
    out: list[CapturedRequest] = []
    for request in requests:
        ops = operations(request)
        if not ops or len(ops) == 1:
            out.append(request)
            continue
        try:
            answers = json.loads(request.response_body or "")
        except ValueError:
            answers = None
        for n, op in enumerate(ops):
            answer = answers[n] if isinstance(answers, list) and len(answers) == len(ops) else None
            out.append(request.model_copy(update={
                "id": str(uuid.uuid4()),
                "request_body": json.dumps([op.payload]),
                "response_body": json.dumps([answer]) if answer is not None else request.response_body,
            }))
    return out


def _words(name: str) -> list[str]:
    """GetRepoIssues / get_repo_issues -> ["get", "repo", "issues"]"""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


_LOGIN_WORDS = {
    "login", "logout", "signin", "signout", "signup", "register", "twofactor", "2fa",
    "mfa", "otp", "webauthn", "passwordreset", "resetpassword", "forgotpassword",
    "changepassword", "updatepassword",
}


def is_login_or_token(operation: str) -> bool:
    """True for operations that sign in or hand out tokens (LoginUser, RefreshToken)."""
    words = _words(operation)
    joined = set(words) | {a + b for a, b in itertools.pairwise(words)}
    return bool(joined & _LOGIN_WORDS) or bool(words and words[-1] in ("token", "tokens", "csrf"))
