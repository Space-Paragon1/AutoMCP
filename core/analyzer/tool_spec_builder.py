from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any

import anthropic
from pydantic import ValidationError

from core.config import settings
from core.storage.models import (
    CapturedRequest,
    EndpointCluster,
    QualityScore,
    ToolInput,
    ToolSpec,
)

SYSTEM_PROMPT = """You are an expert API analyst and MCP (Model Context Protocol) tool designer.

Your task is to analyze HTTP request/response data captured from a browser session and generate
structured ToolSpec JSON objects that describe reusable MCP tools.

For each endpoint cluster provided, generate a ToolSpec JSON object with these exact fields:
- tool_name: snake_case name (e.g. "create_card", "search_users")
- purpose: clear one-sentence description of what this tool does
- method: HTTP method (GET, POST, PUT, PATCH, DELETE)
- url_template: URL with {param} placeholders for path parameters
- auth_strategy: one of "cookies", "bearer", "api_key", "none"
- csrf_strategy: null, or the CSRF header/field name if detected
- inputs: array of input parameter objects with {name, type, required, description}
- request_mapping: dict mapping request body/query keys to $input.<param_name>
- response_type: "json", "text", or "binary"
- confidence: float 0.0-1.0 indicating how confident you are this is a useful tool

Input types must be one of: string, integer, number, boolean, array, object

Rules:
1. tool_name must be unique, snake_case, descriptive
2. Extract path parameters as inputs (e.g. /boards/{board_id} -> board_id input)
3. Extract all request body fields as inputs
4. Extract relevant query parameters as inputs
5. If the request has auth cookies, set auth_strategy to "cookies"
6. If Authorization header is present with Bearer token, set auth_strategy to "bearer"
7. If an API key header is present, set auth_strategy to "api_key"
8. Set confidence based on: clarity of purpose, completeness of request data, action labels present
9. Return ONLY a valid JSON array of ToolSpec objects, no markdown, no explanation

JSON Schema for ToolInput:
{"name": "string", "type": "string|integer|number|boolean|array|object", "required": true/false, "description": "string"}

JSON Schema for ToolSpec:
{
  "tool_name": "string",
  "purpose": "string",
  "method": "string",
  "url_template": "string",
  "auth_strategy": "cookies|bearer|api_key|none",
  "csrf_strategy": "string|null",
  "inputs": [...],
  "request_mapping": {"body_key": "$input.param_name"},
  "response_type": "json|text|binary",
  "confidence": 0.0-1.0
}"""


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


def _build_cluster_prompt(
    cluster: EndpointCluster,
    representative: CapturedRequest,
) -> dict[str, Any]:
    """Build a dict summarising a cluster for the LLM prompt."""
    # Truncate large bodies for the prompt
    req_body = representative.request_body
    if req_body and len(req_body) > 2000:
        req_body = req_body[:2000] + "...[truncated]"

    resp_body = representative.response_body
    if resp_body and len(resp_body) > 2000:
        resp_body = resp_body[:2000] + "...[truncated]"

    # Filter headers to the interesting ones
    interesting_headers = {
        k: v
        for k, v in representative.request_headers.items()
        if k.lower() in (
            "content-type", "accept", "authorization", "x-api-key",
            "x-auth-token", "cookie", "x-csrftoken", "x-xsrf-token"
        )
    }
    # Truncate cookie header
    if "cookie" in interesting_headers:
        interesting_headers["cookie"] = interesting_headers["cookie"][:200] + "...[truncated]"

    return {
        "method": cluster.method,
        "url_template": cluster.url_template,
        "action_labels": cluster.action_labels,
        "request_count": len(cluster.request_ids),
        "body_schema": cluster.body_schema,
        "representative_request": {
            "url": representative.url,
            "headers": interesting_headers,
            "body": req_body,
            "response_status": representative.response_status,
            "response_body_sample": resp_body,
        },
    }


def _score_quality(spec: ToolSpec) -> QualityScore:
    """Assign a quality score based on heuristics."""
    method = spec.method.upper()

    # Usefulness
    if spec.inputs and any(
        label for label in []
    ):
        usefulness = 1.0
    elif spec.quality_score and spec.quality_score.usefulness > 0:
        usefulness = spec.quality_score.usefulness
    elif method in ("POST", "PUT", "PATCH", "DELETE"):
        usefulness = 0.7
    elif method == "GET" and spec.inputs:
        usefulness = 0.5
    else:
        usefulness = 0.3

    # Boost if there are meaningful action labels (session context)
    if "{" not in spec.url_template:
        template_for_check = spec.url_template
    else:
        template_for_check = spec.url_template

    # Stability
    id_count = template_for_check.count("{")
    if id_count == 0:
        stability = 0.9
    elif id_count == 1:
        stability = 0.7
    else:
        stability = 0.5

    # Side-effect risk
    if method == "DELETE":
        side_effect_risk = 0.9
    elif method in ("POST", "PUT", "PATCH"):
        side_effect_risk = 0.7
    else:
        side_effect_risk = 0.1

    return QualityScore(
        usefulness=usefulness,
        stability=stability,
        side_effect_risk=side_effect_risk,
    )


class ToolSpecBuilder:
    """Uses an LLM to generate ToolSpec objects from endpoint clusters."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        provider: LLMProvider = LLMProvider.ANTHROPIC,
    ) -> None:
        self._provider = provider
        if provider == LLMProvider.ANTHROPIC:
            self._client = client or anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        elif provider == LLMProvider.OPENAI:
            try:
                import openai
                self._openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
            except ImportError:
                raise ImportError("Install openai: pip install openai")
        elif provider == LLMProvider.GEMINI:
            try:
                import google.generativeai as genai
                genai.configure(api_key=settings.gemini_api_key)
                self._gemini_model = genai.GenerativeModel("gemini-1.5-pro")
            except ImportError:
                raise ImportError("Install google-generativeai: pip install google-generativeai")

    async def build_specs(
        self,
        clusters: list[EndpointCluster],
        requests_map: dict[str, CapturedRequest],
        session_id: str,
    ) -> list[ToolSpec]:
        """
        Generate ToolSpec objects for all clusters using the LLM.
        Processes clusters in batches to avoid token limits.
        """
        if not clusters:
            return []

        # Build prompt content
        cluster_summaries = []
        for cluster in clusters:
            rep = requests_map.get(cluster.representative_request_id)
            if rep is None:
                continue
            cluster_summaries.append(_build_cluster_prompt(cluster, rep))

        user_content = (
            "Analyze these API endpoint clusters captured from a browser session "
            "and generate ToolSpec JSON objects.\n\n"
            "Clusters:\n"
            + json.dumps(cluster_summaries, indent=2)
            + "\n\nReturn ONLY a JSON array of ToolSpec objects."
        )

        specs = await self._call_llm(user_content, retry_context=None)

        # Score quality and attach session_id
        result: list[ToolSpec] = []
        for spec in specs:
            spec.session_id = session_id
            spec.quality_score = _score_quality(spec)
            result.append(spec)

        # Infer response schemas from captured response bodies
        from core.analyzer.schema_inferrer import SchemaInferrer
        inferrer = SchemaInferrer()
        for spec in result:
            cluster = next(
                (c for c in clusters if c.url_template == spec.url_template and c.method == spec.method),
                None,
            )
            if cluster:
                bodies = [requests_map[rid].response_body for rid in cluster.request_ids if rid in requests_map]
                schema = inferrer.infer_from_responses(bodies)
                if schema:
                    spec.response_schema = schema

        return result

    async def _call_llm(
        self,
        user_content: str,
        retry_context: str | None,
    ) -> list[ToolSpec]:
        """Route to the appropriate LLM provider."""
        if self._provider == LLMProvider.ANTHROPIC:
            return await self._call_anthropic(user_content, retry_context)
        elif self._provider == LLMProvider.OPENAI:
            return await self._call_openai(user_content, retry_context)
        elif self._provider == LLMProvider.GEMINI:
            return await self._call_gemini(user_content, retry_context)
        return []

    async def _call_anthropic(
        self,
        user_content: str,
        retry_context: str | None,
    ) -> list[ToolSpec]:
        """Call the LLM and parse the response into ToolSpec objects."""
        messages: list[dict[str, str]] = [{"role": "user", "content": user_content}]
        if retry_context:
            messages.append({"role": "assistant", "content": retry_context})
            messages.append({
                "role": "user",
                "content": (
                    "The previous response had validation errors. "
                    "Please fix them and return valid JSON only:\n" + retry_context
                ),
            })

        response = await self._client.messages.create(
            model=settings.llm_model,
            max_tokens=8192,
            system=SYSTEM_PROMPT,
            messages=messages,
        )

        raw_text = response.content[0].text.strip()

        # Extract JSON array from the response (handle markdown code blocks)
        json_text = _extract_json(raw_text)

        try:
            data = json.loads(json_text)
            if not isinstance(data, list):
                data = [data]
            return [ToolSpec.model_validate(item) for item in data]
        except (json.JSONDecodeError, ValidationError) as e:
            if retry_context is None:
                # Retry once with error feedback
                error_msg = f"Validation errors: {e}\n\nYour response was:\n{raw_text}"
                return await self._call_anthropic(user_content, retry_context=error_msg)
            # Give up and return empty list
            return []

    async def _call_openai(self, user_content: str, retry_context: str | None) -> list[ToolSpec]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        if retry_context:
            messages.append({"role": "assistant", "content": retry_context})
            messages.append({"role": "user", "content": "Fix the validation errors above and return valid JSON only."})

        response = await self._openai_client.chat.completions.create(
            model=settings.llm_model if "gpt" in settings.llm_model else "gpt-4o",
            messages=messages,
            max_tokens=8192,
        )
        raw_text = response.choices[0].message.content.strip()
        json_text = _extract_json(raw_text)
        try:
            data = json.loads(json_text)
            if not isinstance(data, list):
                data = [data]
            return [ToolSpec.model_validate(item) for item in data]
        except Exception as e:
            if retry_context is None:
                return await self._call_openai(user_content, retry_context=f"Errors: {e}\nYour response: {raw_text}")
            return []

    async def _call_gemini(self, user_content: str, retry_context: str | None) -> list[ToolSpec]:
        import asyncio
        prompt = f"{SYSTEM_PROMPT}\n\n{user_content}"
        if retry_context:
            prompt += f"\n\nFix these errors: {retry_context}"
        response = await asyncio.get_event_loop().run_in_executor(
            None, self._gemini_model.generate_content, prompt
        )
        raw_text = response.text.strip()
        json_text = _extract_json(raw_text)
        try:
            data = json.loads(json_text)
            if not isinstance(data, list):
                data = [data]
            return [ToolSpec.model_validate(item) for item in data]
        except Exception as e:
            if retry_context is None:
                return await self._call_gemini(user_content, retry_context=f"Errors: {e}\nResponse: {raw_text}")
            return []


def _extract_json(text: str) -> str:
    """Extract JSON content from a string, handling markdown code blocks."""
    # Try to find a JSON array in a code block
    code_block = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if code_block:
        return code_block.group(1)

    # Try to find a bare JSON array
    array_match = re.search(r'\[.*\]', text, re.DOTALL)
    if array_match:
        return array_match.group(0)

    return text
