import json
from types import SimpleNamespace

import pytest

from core.analyzer.endpoint_clusterer import EndpointClusterer, _normalise_path
from core.analyzer.event_classifier import EventClassifier, site_of
from core.analyzer.tool_spec_builder import (
    SYSTEM_PROMPT,
    TOOL_SPECS_SCHEMA,
    LLMProvider,
    ToolSpecBuilder,
    redact_body,
    redact_headers,
)
from core.config import settings
from core.storage.models import CapturedRequest


def _req(url, method="GET", status=200, resource_type="fetch", **kw):
    return CapturedRequest(
        session_id="s", method=method, url=url, response_status=status,
        resource_type=resource_type, **kw,
    )


# ---------------------------------------------------------------- clustering

def test_normalise_path_names_placeholders_after_resource():
    assert _normalise_path("/api/boards/5f9c1a2b3c4d5e6f7a8b9c0d") == "/api/boards/{board_id}"
    assert _normalise_path("/v2/addresses/7/items/9") == "/v2/addresses/{address_id}/items/{item_id}"
    assert _normalise_path("/1/categories/42") == "/1/categories/{category_id}"


def test_normalise_path_consecutive_ids_make_valid_placeholders():
    # Used to produce "/api/boards/{board_id}/{{board_id}_id}"
    assert _normalise_path("/api/boards/123/456") == "/api/boards/{board_id}/{id}"


def test_normalise_path_keeps_words_and_dedupes_names():
    assert _normalise_path("/api/notificationsettings") == "/api/notificationsettings"
    assert _normalise_path("/a/1/a/2") == "/a/{a_id}/a/{a_id_2}"


def test_cluster_groups_by_template():
    clusters = EndpointClusterer().cluster([
        _req("https://x.com/api/boards/111"),
        _req("https://x.com/api/boards/222"),
        _req("https://x.com/api/cards", method="POST"),
    ])
    templates = {(c.method, c.url_template): len(c.request_ids) for c in clusters}
    assert templates == {
        ("POST", "https://x.com/api/cards"): 1,
        ("GET", "https://x.com/api/boards/{board_id}"): 2,
    }


# ---------------------------------------------------------------- filtering

def test_classifier_matches_whole_segments_only():
    kept = EventClassifier().classify([
        _req("https://x.com/api/collections"),
        _req("https://x.com/api/collect"),
        _req("https://x.com/tracks/42"),
        _req("https://x.com/v1/track"),
    ])
    assert [r.url for r in kept] == ["https://x.com/api/collections", "https://x.com/tracks/42"]


def test_classifier_blocks_tracker_domains_by_suffix():
    kept = EventClassifier().classify([
        _req("https://o123.ingest.sentry.io/api/1/envelope/"),
        _req("https://notsentry.io/api/data"),
        _req("https://d111.cloudfront.net/api/items"),
    ])
    assert [r.url for r in kept] == ["https://notsentry.io/api/data", "https://d111.cloudfront.net/api/items"]


def test_classifier_drops_page_loads_unless_asked():
    reqs = [
        _req("https://x.com/dashboard", resource_type="document"),
        _req("https://x.com/cards/new", method="POST", resource_type="document"),
        _req("https://x.com/app.js", resource_type="script"),
        _req("https://x.com/api/me"),
    ]
    assert [r.url for r in EventClassifier().classify(reqs)] == [
        "https://x.com/cards/new", "https://x.com/api/me",
    ]
    with_pages = EventClassifier(include_pages=True).classify(reqs)
    assert "https://x.com/dashboard" in [r.url for r in with_pages]


# ---------------------------------------------------------------- redaction

def test_site_of_groups_subdomains_but_not_shared_hosting():
    assert site_of("api.example.com") == "example.com"
    assert site_of("www.shop.example.co.uk") == "example.co.uk"
    assert site_of("alice.github.io") == "alice.github.io"
    assert site_of("127.0.0.1") == "127.0.0.1"
    assert site_of("localhost") == "localhost"


def test_classifier_drops_other_sites_but_keeps_hosted_backends():
    page = _req("https://app.example.com/", resource_type="document",
                request_headers={"sec-fetch-dest": "document"})
    widget_frame = _req("https://js.stripe.com/v3/m-outer.html", resource_type="document",
                        request_headers={"sec-fetch-dest": "iframe"})
    reqs = [
        page, widget_frame,
        _req("https://api.example.com/v1/boards"),
        _req("https://fonts.example-cdn.net/list"),
        _req("https://xyz.supabase.co/rest/v1/todos", request_headers={"authorization": "Bearer t"}),
        _req("https://api.stripe.com/v1/payment_methods", request_headers={"authorization": "Bearer pk"}),
        _req("https://partner.io/api/data"),
    ]
    classifier = EventClassifier(site_url="https://app.example.com/", allowed_hosts=["partner.io"])
    kept = [r.url for r in classifier.classify(reqs)]
    assert kept == [
        "https://api.example.com/v1/boards",
        "https://xyz.supabase.co/rest/v1/todos",
        "https://partner.io/api/data",
    ]
    assert classifier.get_rejection_reason(reqs[3]) == [
        "Third-party site fonts.example-cdn.net (use --allow-host to keep)"
    ]


def test_classifier_counts_sites_the_user_navigated_to_as_first_party():
    reqs = [
        _req("https://www.google.com/", resource_type="document",
             request_headers={"sec-fetch-dest": "document"}),
        _req("https://app.other.com/home", resource_type="document",
             request_headers={"sec-fetch-dest": "document"}),
        _req("https://api.other.com/items"),
    ]
    kept = EventClassifier(site_url="https://www.google.com/").classify(reqs)
    assert [r.url for r in kept] == ["https://api.other.com/items"]


def test_classifier_drops_login_steps_and_token_endpoints():
    kept = EventClassifier().classify([
        _req("https://github.com/sessions/two-factor/mobile_poll", method="POST"),
        _req("https://github.com/u2f/login_fragment"),
        _req("https://x.com/api/auth/login", method="POST"),
        _req("https://github.com/copilot/agent-sessions/token", method="POST"),
        _req("https://github.com/copilot/agent-sessions/cmc-api-token", method="POST"),
        _req("https://x.com/api/csrf_token"),
        _req("https://x.com/api/sessions/42"),       # A resource, not a login step
        _req("https://x.com/api/token-usage/stats"),  # Not a token endpoint
    ])
    assert [r.url for r in kept] == [
        "https://x.com/api/sessions/42", "https://x.com/api/token-usage/stats",
    ]


def test_classifier_drops_site_internal_files():
    kept = EventClassifier().classify([
        _req("https://x.com/cdn-cgi/rum", method="POST"),
        _req("https://x.com/_next/data/abc123/boards.json"),
        _req("https://x.com/page-data/app/page-data.json"),
        _req("https://x.com/docs/routeInfo.json"),
        _req("https://x.com/api/boards"),
    ])
    assert [r.url for r in kept] == ["https://x.com/api/boards"]


def test_redact_body_hides_secrets_and_keeps_shape():
    out = json.loads(redact_body(json.dumps(
        {"username": "a", "password": "hunter2", "author": "Al", "nested": {"access_token": "t"}}
    )))
    assert out == {"username": "a", "password": "<redacted>", "author": "Al",
                   "nested": {"access_token": "<redacted>"}}
    assert "hunter2" not in redact_body("user=a&password=hunter2")


def test_redact_headers_keeps_names_not_values():
    out = redact_headers({
        "Cookie": "sid=secret1; theme=dark",
        "Authorization": "Bearer eyJsecret",
        "x-csrf-token": "secret2",
        "user-agent": "UA",
    })
    flat = json.dumps(out)
    assert "secret" not in flat
    assert "sid" in out["cookie"] and out["authorization"] == "Bearer <redacted>"
    assert "user-agent" not in out


# ---------------------------------------------------------------- spec building

class FakeAnthropic:
    """Stands in for anthropic.AsyncAnthropic; answers from a callback."""

    def __init__(self, respond):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self._respond = respond

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        clusters = json.loads(prompt.split("Clusters:\n", 1)[1].rsplit("\n\nReturn", 1)[0])
        text, stop = self._respond(clusters)
        return SimpleNamespace(
            stop_reason=stop, content=[SimpleNamespace(type="text", text=text)]
        )


def _spec_for(cluster):
    return {
        "cluster_index": cluster["cluster_index"],
        "tool_name": f"tool{cluster['cluster_index']}",
        "purpose": "p", "method": cluster["method"], "url_template": cluster["url_template"],
        "auth_strategy": "cookies", "inputs": [], "request_mapping": {}, "confidence": 0.9,
    }


def _clusters(n):
    reqs = [
        _req(f"https://x.com/api/thing{i}", response_body=json.dumps({"n": i}))
        for i in range(n)
    ]
    return EndpointClusterer().cluster(reqs), {r.id: r for r in reqs}


async def test_build_specs_batches_and_maps_back_by_index():
    clusters, reqs = _clusters(5)
    fake = FakeAnthropic(lambda cs: (json.dumps([_spec_for(c) for c in reversed(cs)]), "end_turn"))
    specs = await ToolSpecBuilder(client=fake, batch_size=2).build_specs(clusters, reqs, "sess")

    assert len(fake.calls) == 3  # 5 clusters in batches of 2
    assert len(specs) == 5
    for spec in specs:
        index = int(spec.tool_name.removeprefix("tool"))
        assert spec.sample_url == reqs[clusters[index].representative_request_id].url
        assert spec.response_schema == {"type": "object", "properties": {"n": {"type": "integer"}}}
        assert spec.session_id == "sess"


async def test_build_specs_splits_batch_when_output_is_truncated():
    clusters, reqs = _clusters(4)

    def respond(cs):
        if len(cs) > 1:
            return "[{\"cluster_index\": 0, \"tool_na", "max_tokens"
        return json.dumps([_spec_for(c) for c in cs]), "end_turn"

    specs = await ToolSpecBuilder(client=FakeAnthropic(respond), batch_size=4).build_specs(
        clusters, reqs, "sess"
    )
    assert len(specs) == 4


async def test_build_specs_never_sends_secrets_to_llm():
    req = _req(
        "https://x.com/api/login", method="POST",
        request_headers={"cookie": "sid=topsecret", "authorization": "Bearer topsecret"},
        request_body=json.dumps({"user": "a", "password": "topsecret"}),
    )
    clusters = EndpointClusterer().cluster([req])
    fake = FakeAnthropic(lambda cs: ("[]", "end_turn"))
    await ToolSpecBuilder(client=fake).build_specs(clusters, {req.id: req}, "sess")
    assert "topsecret" not in json.dumps(fake.calls)


async def test_build_specs_avoids_taken_names():
    clusters, reqs = _clusters(1)
    fake = FakeAnthropic(lambda cs: (json.dumps([_spec_for(c) for c in cs]), "end_turn"))
    specs = await ToolSpecBuilder(client=fake).build_specs(clusters, reqs, "s", {"tool0"})
    assert specs[0].tool_name == "tool0_2"


async def test_build_specs_uses_structured_output_and_keeps_recorded_types():
    req = _req(
        "https://x.com/api/boards/123/cards", method="POST",
        request_headers={"content-type": "application/json"},
        request_body=json.dumps({"name": "Milk", "pos": "bottom", "limit": 50, "closed": False}),
    )
    clusters = EndpointClusterer().cluster([req])

    def respond(cs):
        return json.dumps({"specs": [{
            **_spec_for(cs[0]),
            "inputs": [{"name": "title", "type": "string", "required": True, "description": ""}],
            "csrf_strategy": "",
            "request_mapping": [
                {"field": "name", "value": "$input.title"},
                {"field": "pos", "value": "bottom"},
                {"field": "limit", "value": "50"},
                {"field": "closed", "value": "false"},
            ],
        }]}), "end_turn"

    fake = FakeAnthropic(respond)
    specs = await ToolSpecBuilder(client=fake).build_specs(clusters, {req.id: req}, "s")

    assert fake.calls[0]["output_config"] == {
        "format": {"type": "json_schema", "schema": TOOL_SPECS_SCHEMA}
    }
    assert specs[0].request_mapping == {
        "name": "$input.title", "pos": "bottom", "limit": 50, "closed": False,
    }
    assert specs[0].csrf_strategy is None


# ---------------------------------------------------------------- gemini

class FakeGemini:
    """Stands in for google.genai.Client; answers from a callback."""

    def __init__(self, respond):
        self.calls = []
        self._respond = respond
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        prompt = contents[0].parts[0].text
        clusters = json.loads(prompt.split("Clusters:\n", 1)[1].rsplit("\n\nReturn", 1)[0])
        text, finish = self._respond(clusters)
        return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=finish)])


def _gemini_builder(monkeypatch, respond, **kwargs):
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_model", "claude-opus-5-5")
    builder = ToolSpecBuilder(provider=LLMProvider.GEMINI, **kwargs)
    fake = FakeGemini(respond)
    builder._gemini_client = fake
    return builder, fake


async def test_gemini_builds_specs_and_splits_truncated_batches(monkeypatch):
    types = pytest.importorskip("google.genai.types")
    clusters, reqs = _clusters(4)

    def respond(cs):
        if len(cs) > 1:
            return '[{"cluster_index": 0, "tool_na', types.FinishReason.MAX_TOKENS
        return json.dumps([_spec_for(c) for c in cs]), types.FinishReason.STOP

    builder, fake = _gemini_builder(monkeypatch, respond, batch_size=4)
    specs = await builder.build_specs(clusters, reqs, "sess")

    assert len(specs) == 4
    # A Claude model name in AUTOMCP_LLM_MODEL must not be sent to Google
    assert {c["model"] for c in fake.calls} == {"gemini-flash-latest"}
    assert fake.calls[0]["config"].system_instruction == SYSTEM_PROMPT
    assert fake.calls[0]["config"].response_json_schema == TOOL_SPECS_SCHEMA


async def test_gemini_retry_sends_bad_answer_back_as_model_turn(monkeypatch):
    types = pytest.importorskip("google.genai.types")
    clusters, reqs = _clusters(1)
    answers = iter(["not json", None])

    def respond(cs):
        return next(answers) or json.dumps([_spec_for(c) for c in cs]), types.FinishReason.STOP

    builder, fake = _gemini_builder(monkeypatch, respond)
    specs = await builder.build_specs(clusters, reqs, "sess")

    assert [s.tool_name for s in specs] == ["tool0"]
    retry = fake.calls[1]["contents"]
    assert [c.role for c in retry] == ["user", "model", "user"]
    assert retry[1].parts[0].text == "not json"


# ---------------------------------------------------------------- real-site fixes (GitHub)

def test_cluster_turns_varying_names_into_placeholders():
    reqs = [
        *(_req(f"https://github.com/me/{repo}/star", method="POST")
          for repo in ("AutoMCP", "Resume-Analyzer")),
        *(_req(f"https://github.com/me/{repo}/graphs/participation")
          for repo in ("server", "cudf", "recipes", "notchprompt")),
        _req("https://github.com/me/AutoMCP/issues"),
        _req("https://github.com/me/AutoMCP/pulls"),       # Last segment: separate endpoints
        _req("https://github.com/me/AutoMCP/branch-infobar/main"),
        _req("https://github.com/me/AutoMCP/overview-files/main"),  # Route words, not names
        _req("https://x.com/api/v1/users"),
        _req("https://x.com/api/v2/users"),                 # API versions stay apart
        _req("https://api.x.com/api/v1/users"),             # Other host: other endpoint
    ]
    templates = sorted((c.method, c.url_template) for c in EndpointClusterer().cluster(reqs))
    assert templates == sorted([
        ("POST", "https://github.com/me/{name}/star"),
        ("GET", "https://github.com/me/{name}/graphs/participation"),
        ("GET", "https://github.com/me/AutoMCP/issues"),
        ("GET", "https://github.com/me/AutoMCP/pulls"),
        ("GET", "https://github.com/me/AutoMCP/branch-infobar/main"),
        ("GET", "https://github.com/me/AutoMCP/overview-files/main"),
        ("GET", "https://x.com/api/v1/users"),
        ("GET", "https://x.com/api/v2/users"),
        ("GET", "https://api.x.com/api/v1/users"),
    ])


def test_classifier_keeps_form_posts_that_redirect_but_never_logins():
    kept = EventClassifier().classify([
        _req("https://github.com/me/repo/pull/create", method="POST", status=302,
             request_body="authenticity_token=x&pull_request%5Btitle%5D=Hi"),
        _req("https://github.com/session", method="POST", status=302,
             request_body="login=me&password=secret"),
        _req("https://x.com/old-page", status=301),
        _req("https://x.com/api/boards/9", status=404),
        _req("https://x.com/assets/opt-in.specrule-0a0e9d38bd18.json"),
    ])
    assert [r.url for r in kept] == ["https://github.com/me/repo/pull/create"]


def test_multipart_bodies_are_redacted_and_replayed_as_multipart():
    body = (
        "------B\r\nContent-Disposition: form-data; name=\"authenticity_token\"\r\n\r\nsecret123\r\n"
        "------B\r\nContent-Disposition: form-data; name=\"context\"\r\n\r\nrepository\r\n------B--\r\n"
    )
    assert json.loads(redact_body(body)) == {
        "multipart_form_fields": {"authenticity_token": "<redacted>", "context": "repository"}
    }


async def test_specs_keep_endpoint_headers_and_drop_duplicates():
    def post(repo):
        return _req(
            f"https://github.com/me/{repo}/star", method="POST",
            request_headers={
                "content-type": "multipart/form-data; boundary=x", "origin": "https://github.com",
                "github-verified-fetch": "true", "x-fetch-nonce": "n1", "cookie": "s=1",
                "x-csrf-token": "t", "sec-fetch-mode": "cors", ":path": "/x",
            },
        )
    # Two clusters (different hosts would never merge) that the model describes identically
    reqs = [post("AutoMCP"), _req("https://github.com/me/AutoMCP/star2", method="POST",
                                   request_headers={"x-fetch-nonce": "n1"})]
    # Ordinary page calls, so only x-fetch-nonce is common to most calls to the host
    others = [_req(f"https://github.com/me/page{i}", request_headers={"x-fetch-nonce": "n1"})
              for i in range(3)]
    clusters = EndpointClusterer().cluster(reqs)

    def respond(cs):
        return json.dumps({"specs": [
            {**_spec_for(c), "tool_name": "star_repo",
             "confidence": 0.9 if c["url_template"].endswith("/star") else 0.6,
             "url_template": "https://github.com/me/{repo}/star"} for c in cs
        ]}), "end_turn"

    specs = await ToolSpecBuilder(client=FakeAnthropic(respond)).build_specs(
        clusters, {r.id: r for r in reqs + others}, "s"
    )
    assert [s.tool_name for s in specs] == ["star_repo"]  # One spec, not star_repo + star_repo_2
    assert specs[0].sample_url == reqs[0].url  # The more confident duplicate wins
    assert specs[0].body_encoding == "multipart"
    # Host-wide (x-fetch-nonce), auth, cookie, CSRF and browser headers are left out
    assert specs[0].headers == {"origin": "https://github.com", "github-verified-fetch": "true"}


def test_placeholders_are_named_after_plural_collections():
    reqs = [_req(f"https://x.com/repos/{r}/star", method="POST") for r in ("Alpha1", "Beta2")]
    assert [c.url_template for c in EndpointClusterer().cluster(reqs)] == ["https://x.com/repos/{repo}/star"]


async def test_gemini_daily_quota_is_not_retried_but_minute_limits_are(monkeypatch):
    errors = pytest.importorskip("google.genai.errors")
    types = pytest.importorskip("google.genai.types")
    import core.analyzer.tool_spec_builder as tsb

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(tsb.asyncio, "sleep", no_wait)
    clusters, reqs = _clusters(1)

    def quota_error(kind):
        return errors.ClientError(429, {"error": {
            "code": 429, "status": "RESOURCE_EXHAUSTED",
            "message": f"Quota exceeded for metric: generate_content_free_tier_requests{kind}",
        }})

    answers = iter([quota_error("PerMinute"), None])

    def per_minute(cs):
        answer = next(answers)
        if answer:
            raise answer
        return json.dumps({"specs": [_spec_for(c) for c in cs]}), types.FinishReason.STOP

    builder, fake = _gemini_builder(monkeypatch, per_minute)
    assert len(await builder.build_specs(clusters, reqs, "s")) == 1
    assert len(fake.calls) == 2  # Waited and tried again

    def per_day(cs):
        raise quota_error("PerDay")

    builder, fake = _gemini_builder(monkeypatch, per_day)
    with pytest.raises(errors.ClientError):
        await builder.build_specs(clusters, reqs, "s")
    assert len(fake.calls) == 1  # No retries that would use up more of the quota
