import json
from types import SimpleNamespace

import pytest

from core.analyzer.endpoint_clusterer import EndpointClusterer, _normalise_path
from core.analyzer.event_classifier import EventClassifier
from core.analyzer.tool_spec_builder import (
    SYSTEM_PROMPT,
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
        _req("https://x.com/login", method="POST", resource_type="document"),
        _req("https://x.com/app.js", resource_type="script"),
        _req("https://x.com/api/me"),
    ]
    assert [r.url for r in EventClassifier().classify(reqs)] == [
        "https://x.com/login", "https://x.com/api/me",
    ]
    with_pages = EventClassifier(include_pages=True).classify(reqs)
    assert "https://x.com/dashboard" in [r.url for r in with_pages]


# ---------------------------------------------------------------- redaction

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
