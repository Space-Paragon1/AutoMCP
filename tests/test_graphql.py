import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
import pytest
from fastmcp import Client

from core.analyzer import graphql
from core.analyzer.endpoint_clusterer import EndpointClusterer
from core.analyzer.event_classifier import EventClassifier
from core.analyzer.tool_spec_builder import ToolSpecBuilder
from core.config import settings
from core.runtime import executor
from core.runtime.middleware import ToolError
from core.runtime.tool_loader import ToolLoader
from core.storage.db import get_db
from core.storage.models import GraphQLInfo, RecordingSession, ToolInput, ToolSpec
from tests.test_analyzer import FakeAnthropic, _req, _spec_for
from tests.test_runtime import _field

URL = "https://app.example.com/graphql"
ISSUES = "query GetIssues($repo: String!, $first: Int) { repo(name: $repo) { issues(first: $first) { title } } }"
CREATE = "mutation CreateIssue($input: IssueInput!) { createIssue(input: $input) { id } }"


def _gql(body, url=URL, method="POST", response=None, **kw):
    return _req(
        url, method=method, request_body=json.dumps(body) if body is not None else None,
        request_headers={"content-type": "application/json"},
        response_body=json.dumps(response or {"data": {}}), **kw,
    )


def _issues(repo="AutoMCP", first=20, token="t0p"):
    return _gql({"operationName": "GetIssues", "query": ISSUES,
                 "variables": {"repo": repo, "first": first, "authToken": token}})


def _create(title="Hi"):
    return _gql({"operationName": "CreateIssue", "query": CREATE,
                 "variables": {"input": {"title": title, "repoId": "R_1"}}})


# X (Twitter) style: the operation is named in the address, and the request
# carries only variables and settings, no operation name or query text
X_USER = "https://x.com/i/api/graphql/Yka-W8dz7RaEuQNkroPkYw/UserByScreenName"
X_LIKE = "https://x.com/i/api/graphql/lI07N6Otwv1PhnEgXILM7A/FavoriteTweet"
FEATURES = {"hidden_profile_subscriptions_enabled": True, "verified_phone_label_enabled": False}


def _x_user(screen_name="nasa"):
    return _req(X_USER + "?" + urlencode({
        "variables": json.dumps({"screen_name": screen_name, "withSafetyModeUserFields": True}),
        "features": json.dumps(FEATURES),
    }), response_body=json.dumps({"data": {"user": {"name": screen_name}}}))


def _x_like(tweet_id="1"):
    return _gql({"variables": {"tweet_id": tweet_id}, "queryId": "lI07N6Otwv1PhnEgXILM7A"},
                url=X_LIKE)


# ---------------------------------------------------------------- reading operations

@pytest.mark.parametrize("document,expected", [
    ("query GetUser($id: ID!) { user(id: $id) { name } }", [("query", "GetUser")]),
    ("fragment F on User { query } query Q { me { ...F } }", [("query", "Q")]),
    ("{ me { name } }", [("query", None)]),
    ('mutation { add(input: {text: "a } b"}) { id } }', [("mutation", None)]),
    ("# query Fake\nsubscription OnMessage @live { message }", [("subscription", "OnMessage")]),
    ("query A { a } mutation B { b }", [("query", "A"), ("mutation", "B")]),
    ("cats", None),
    ("query", None),
    ("query X { unclosed", None),
])
def test_graphql_documents_are_recognised(document, expected):
    assert graphql.definitions(document) == expected


def test_operations_are_found_in_posts_gets_saved_queries_and_batches():
    [op] = graphql.operations(_issues())
    assert op.info == GraphQLInfo(operation="GetIssues", kind="query")

    get = _req(URL + "?" + urlencode({
        "operationName": "GetIssues", "variables": json.dumps({"repo": "x"}),
        "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "ab" * 32}}),
    }))
    [op] = graphql.operations(get)
    # GraphQL over GET may only read, so it's a query even without the query text
    assert op.info.operation == "GetIssues" and op.info.kind == "query"
    assert op.payload["variables"] == {"repo": "x"}

    saved = _gql({"extensions": {"persistedQuery": {"sha256Hash": "cd" * 32}}, "variables": {}})
    assert graphql.operations(saved)[0].info.operation == "persisted_" + "cd" * 6

    batch = _gql([{"query": "{ me { id } }"}, {"query": CREATE, "variables": {}}])
    ops = graphql.operations(batch)
    assert [(o.info.kind, o.info.batched) for o in ops] == [("query", True), ("mutation", True)]
    assert ops[0].info.operation.startswith("anonymous_")


def test_operations_named_in_the_address_are_found():
    [op] = graphql.operations(_x_user())
    # Sent over GET, so it only reads
    assert op.info == GraphQLInfo(operation="UserByScreenName", kind="query")
    assert op.payload["variables"] == {"screen_name": "nasa", "withSafetyModeUserFields": True}
    assert json.loads(op.payload["features"]) == FEATURES  # Kept exactly as the page sent it

    [op] = graphql.operations(_x_like())
    assert op.info == GraphQLInfo(operation="FavoriteTweet", kind="unknown")
    assert op.payload["queryId"] == "lI07N6Otwv1PhnEgXILM7A"

    # A name in the request still wins over the address
    named = _gql({"operationName": "GetIssues", "variables": {}}, url=URL + "/v1")
    assert graphql.operations(named)[0].info.operation == "GetIssues"
    # Anonymous queries keep their own names, so they aren't all grouped under "v1"
    anonymous = _gql({"query": "{ me { id } }"}, url=URL + "/v1")
    assert graphql.operations(anonymous)[0].info.operation.startswith("anonymous_")


def test_ordinary_api_calls_are_not_graphql():
    assert graphql.operations(_req("https://x.com/api/search?query=cats")) is None
    assert graphql.operations(_gql({"query": "cats", "page": 2}, url="https://x.com/api/search")) is None
    assert graphql.operations(_gql({"title": "x"}, url="https://x.com/api/cards")) is None
    # Variables alone aren't enough without "graphql" in the address
    assert graphql.operations(_req("https://x.com/api/UserByScreenName?variables={}")) is None
    assert graphql.operations(_gql({"variables": {}}, url="https://x.com/api/Like")) is None
    # Nor is an address with "graphql" in it, if the call sends no variables
    assert graphql.operations(_req(X_USER)) is None
    assert graphql.operations(_req("https://x.com/graphql/docs/a/b/Intro?variables={}")) is None


def test_batches_split_into_one_request_per_operation():
    batch = _gql(
        [{"operationName": "A", "query": "query A { a }"},
         {"operationName": "B", "query": "query B { b }"}],
        response=[{"data": {"a": 1}}, {"data": {"b": 2}}],
    )
    single = _issues()
    parts = graphql.split_batches([batch, single])
    assert len(parts) == 3 and parts[2] is single
    assert len({p.id for p in parts}) == 3
    assert json.loads(parts[1].request_body) == [{"operationName": "B", "query": "query B { b }"}]
    assert json.loads(parts[1].response_body) == [{"data": {"b": 2}}]


# ---------------------------------------------------------------- grouping + filtering

def test_each_operation_on_one_url_becomes_its_own_cluster():
    reqs = [_issues("a"), _issues("b"), _create(), _req("https://app.example.com/api/me")]
    clusters = EndpointClusterer().cluster(reqs)
    by_op = {c.graphql.operation if c.graphql else c.url_template: c for c in clusters}
    assert set(by_op) == {"GetIssues", "CreateIssue", "https://app.example.com/api/me"}
    assert len(by_op["GetIssues"].request_ids) == 2
    assert by_op["GetIssues"].url_template == URL
    assert by_op["GetIssues"].body_schema == {"repo": "str", "first": "int", "authToken": "str"}


def test_operations_named_in_the_address_group_by_operation():
    clusters = EndpointClusterer().cluster([_x_user("nasa"), _x_user("esa"), _x_like()])
    by_op = {c.graphql.operation: c for c in clusters}
    assert set(by_op) == {"UserByScreenName", "FavoriteTweet"}
    user = by_op["UserByScreenName"]
    assert len(user.request_ids) == 2
    # The saved query's ID in the address is kept exactly, not turned into an input
    assert user.url_template == X_USER and user.method == "GET"


def test_graphql_logins_and_token_calls_are_never_tools():
    login = _gql({"operationName": "LoginUser", "query": "mutation LoginUser { login { ok } }"})
    refresh = _gql({"operationName": "RefreshToken", "query": "mutation RefreshToken { t }"})
    kept = EventClassifier().classify([login, refresh, _issues()])
    assert [graphql.operations(r)[0].info.operation for r in kept] == ["GetIssues"]


# ---------------------------------------------------------------- tool specs

async def test_specs_run_the_recorded_operation_and_map_only_variables():
    reqs = [_issues("AutoMCP"), _issues("Other"), _create(),
            _gql({"query": 'mutation Login2 { signIn(password: "hunter2") { ok } }'})]
    clusters = EndpointClusterer().cluster(reqs)
    prompts = []

    def respond(cs):
        prompts.extend(cs)
        specs = []
        for c in cs:
            if c["graphql"]["operation_name"] == "GetIssues":
                specs.append({
                    **_spec_for(c), "tool_name": "list_issues",
                    # The model invents a URL and a query; both are replaced by the recording
                    "url_template": "https://app.example.com/graphql/{repo}",
                    "inputs": [{"name": "repo", "type": "string", "required": True, "description": ""}],
                    "request_mapping": [{"field": "variables.repo", "value": "$input.repo"},
                                        {"field": "query", "value": "{ wrong }"}],
                })
            elif c["graphql"]["operation_name"] == "CreateIssue":
                specs.append({
                    **_spec_for(c), "tool_name": "create_issue",
                    "inputs": [{"name": "title", "type": "string", "required": True, "description": ""}],
                    "request_mapping": [{"field": "variables.input.title", "value": "$input.title"}],
                })
        return json.dumps({"specs": specs}), "end_turn"

    specs = await ToolSpecBuilder(client=FakeAnthropic(respond)).build_specs(
        clusters, {r.id: r for r in reqs}, "s"
    )
    by_name = {s.tool_name: s for s in specs}
    assert set(by_name) == {"list_issues", "create_issue"}  # Same URL, not deduplicated

    issues = by_name["list_issues"]
    assert issues.url_template == URL and issues.method == "POST"
    assert issues.graphql == GraphQLInfo(operation="GetIssues", kind="query")
    assert [i.name for i in issues.inputs] == ["repo"]  # No input made up for the URL
    assert issues.request_mapping == {
        "operationName": "GetIssues",
        "query": ISSUES,
        "variables.first": 20,  # Same in every recorded call: kept
        "variables.repo": "$input.repo",
        # authToken looks secret and isn't kept
    }
    assert issues.is_readonly and issues.quality_score.side_effect_risk == 0.1

    create = by_name["create_issue"]
    assert create.request_mapping["variables.input.repoId"] == "R_1"
    assert create.request_mapping["variables.input.title"] == "$input.title"
    assert not create.is_readonly

    sent = json.dumps(prompts)
    assert "hunter2" not in sent and "t0p" not in sent
    issues_prompt = next(c for c in prompts if c["graphql"]["operation_name"] == "GetIssues")
    assert issues_prompt["graphql"]["operation_type"] == "query"
    assert issues_prompt["representative_request"]["body"] is None


async def test_operations_named_in_the_address_become_working_tools():
    reqs = [_x_user("nasa"), _x_user("esa"), _x_like()]
    clusters = EndpointClusterer().cluster(reqs)

    def respond(cs):
        specs = []
        for c in cs:
            name = "screen_name" if c["graphql"]["operation_name"] == "UserByScreenName" else "tweet_id"
            specs.append({
                **_spec_for(c), "tool_name": "get_user" if name == "screen_name" else "like_post",
                "inputs": [{"name": name, "type": "string", "required": True, "description": ""}],
                "request_mapping": [{"field": f"variables.{name}", "value": f"$input.{name}"}],
            })
        return json.dumps({"specs": specs}), "end_turn"

    specs = await ToolSpecBuilder(client=FakeAnthropic(respond)).build_specs(
        clusters, {r.id: r for r in reqs}, "s"
    )
    by_name = {s.tool_name: s for s in specs}
    user, like = by_name["get_user"], by_name["like_post"]
    assert user.url_template == X_USER and user.method == "GET" and user.is_readonly
    assert user.request_mapping == {
        "features": json.dumps(FEATURES),
        "variables.withSafetyModeUserFields": True,
        "variables.screen_name": "$input.screen_name",
    }
    assert like.request_mapping == {
        "queryId": "lI07N6Otwv1PhnEgXILM7A", "variables.tweet_id": "$input.tweet_id",
    }
    assert not like.is_readonly

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {"ok": True}})

    executor._transport = httpx.MockTransport(handler)
    fields = ("tool_name", "purpose", "method", "url_template", "inputs", "request_mapping",
              "graphql", "is_readonly")
    _, mcp = await _serve([{f: getattr(s, f) for f in fields} for s in (user, like)])

    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert _field(tools["get_user"].annotations, "read_only_hint", "readOnlyHint") is True

        result = await client.call_tool("get_user", {"screen_name": "jack"})
        assert json.loads(result.content[0].text) == {"data": {"ok": True}}
        url = urlparse(str(seen[-1].url))
        assert url.path == urlparse(X_USER).path
        params = dict(parse_qsl(url.query))
        assert json.loads(params["variables"]) == {"withSafetyModeUserFields": True, "screen_name": "jack"}
        assert params["features"] == json.dumps(FEATURES)  # Sent exactly as recorded

        await client.call_tool("like_post", {"tweet_id": "42"})
        assert str(seen[-1].url) == X_LIKE
        assert json.loads(seen[-1].content) == {
            "queryId": "lI07N6Otwv1PhnEgXILM7A", "variables": {"tweet_id": "42"},
        }


# ---------------------------------------------------------------- running tools

async def _serve(spec_kwargs_list):
    from apps.cli.main import _generate
    from core.runtime.server import create_server

    session = RecordingSession(url="https://app.example.com/", browser_context_state={
        "cookie_jar": [{"name": "sid", "value": "abc", "domain": "app.example.com", "path": "/"}],
    })
    async with get_db() as db:
        await db.save_session(session)
        for kw in spec_kwargs_list:
            await db.save_tool_spec(ToolSpec(session_id=session.id, approved=True,
                                             auth_strategy="cookies", **kw))
    await _generate(session.id, None)
    return session, await create_server()


def _issues_tool(**kw):
    return dict(
        tool_name="list_issues", purpose="List issues", method="POST", url_template=URL,
        inputs=[ToolInput(name="repo"), ToolInput(name="label", required=False)],
        request_mapping={"operationName": "GetIssues", "query": ISSUES, "variables.first": 20,
                         "variables.repo": "$input.repo"},
        graphql=GraphQLInfo(operation="GetIssues", kind="query"), is_readonly=True, **kw,
    )


async def test_graphql_tools_send_the_operation_and_return_its_data():
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"via": "get"}})
        body = json.loads(request.content)
        if isinstance(body, list):
            return httpx.Response(200, json=[{"data": {"created": body[0]["variables"]}}])
        return httpx.Response(200, json={"data": {"issues": [body["variables"]["repo"]]}})

    executor._transport = httpx.MockTransport(handler)
    _, mcp = await _serve([
        _issues_tool(),
        {"tool_name": "create_issue", "purpose": "Create", "method": "POST", "url_template": URL,
         "inputs": [ToolInput(name="title")],
         "request_mapping": {"operationName": "CreateIssue", "query": CREATE,
                             "variables.input.title": "$input.title"},
         "graphql": GraphQLInfo(operation="CreateIssue", kind="mutation", batched=True)},
        {"tool_name": "get_issues_saved", "purpose": "Saved query", "method": "GET",
         "url_template": URL, "inputs": [ToolInput(name="repo")],
         "request_mapping": {"operationName": "GetIssues",
                             "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "ab"}},
                             "variables.repo": "$input.repo"},
         "graphql": GraphQLInfo(operation="GetIssues")},
    ])

    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        hints = tools["list_issues"].annotations
        assert _field(hints, "read_only_hint", "readOnlyHint") is True

        result = await client.call_tool("list_issues", {"repo": "AutoMCP", "label": "bug"})
        assert json.loads(result.content[0].text) == {"data": {"issues": ["AutoMCP"]}}
        assert json.loads(seen[-1].content) == {
            "operationName": "GetIssues", "query": ISSUES,
            # An input without a mapping (label) still goes in as a variable
            "variables": {"first": 20, "repo": "AutoMCP", "label": "bug"},
        }

        result = await client.call_tool("create_issue", {"title": "Hello"})
        assert json.loads(result.content[0].text) == {"data": {"created": {"input": {"title": "Hello"}}}}
        assert isinstance(json.loads(seen[-1].content), list)  # Sent as a batch, like the page did

        await client.call_tool("get_issues_saved", {"repo": "x"})
        params = dict(parse_qsl(urlparse(str(seen[-1].url)).query))
        assert json.loads(params["variables"]) == {"repo": "x"}
        assert json.loads(params["extensions"])["persistedQuery"]["sha256Hash"] == "ab"
        assert params["operationName"] == "GetIssues"


async def test_graphql_errors_are_reported_as_failures():
    def handler(request):
        if json.loads(request.content)["variables"]["repo"] == "bad":
            # Servers often say what went wrong with HTTP 400
            return httpx.Response(400, json={"errors": [{"message": "Unknown repo 'bad'"}]})
        return httpx.Response(200, json={"data": {"repo": None},
                                         "errors": [{"message": "Some fields failed"}]})

    executor._transport = httpx.MockTransport(handler)
    await _serve([_issues_tool()])
    async with get_db() as db:
        tool = (await db.get_generated_tools())[0]
    list_issues = ToolLoader().load_and_get(Path(tool.file_path), "list_issues")

    with pytest.raises(ToolError, match="Unknown repo 'bad'"):
        await list_issues(repo="bad")
    # Partial data with errors is still an answer
    assert (await list_issues(repo="ok"))["errors"][0]["message"] == "Some fields failed"


async def test_graphql_logged_out_errors_renew_the_login(monkeypatch):
    monkeypatch.setattr(settings, "auto_relogin", True)
    renewals = []

    async def fake_renew(session_id):
        renewals.append(session_id)
        async with get_db() as db:
            await db.update_session_auth(session_id, {"cookie_jar": [
                {"name": "sid", "value": "new", "domain": "app.example.com", "path": "/"}
            ]})
        return True

    def handler(request):
        if "sid=new" in request.headers.get("cookie", ""):
            return httpx.Response(200, json={"data": {"issues": []}})
        return httpx.Response(200, json={"data": None, "errors": [
            {"message": "Please sign in", "extensions": {"code": "UNAUTHENTICATED"}}
        ]})

    executor._renew_login = fake_renew
    executor._transport = httpx.MockTransport(handler)
    session, mcp = await _serve([_issues_tool()])
    async with Client(mcp) as client:
        result = await client.call_tool("list_issues", {"repo": "x"})
    assert json.loads(result.content[0].text) == {"data": {"issues": []}}
    assert renewals == [session.id]
