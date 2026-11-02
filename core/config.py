import os
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# All relative paths (database, .env, generated files, vault) resolve against
# AUTOMCP_HOME, falling back to the current directory. Setting AUTOMCP_HOME lets
# MCP clients like Claude Desktop launch `automcp serve` from any working directory.
HOME = Path(os.environ.get("AUTOMCP_HOME", ".")).expanduser().resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(HOME / ".env"),
        env_file_encoding="utf-8",
        env_prefix="AUTOMCP_",
        extra="ignore",
    )

    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    openai_api_key: str = Field(default="", description="OpenAI API key")
    gemini_api_key: str = Field(default="", description="Google Gemini API key")
    db_path: Path = Field(default=Path("automcp.db"), description="SQLite database path")
    generated_specs_dir: Path = Field(default=Path("generated/specs"))
    generated_tools_dir: Path = Field(default=Path("generated/tools"))
    # Default for `automcp analyze --provider`: anthropic, openai or gemini
    llm_provider: str = Field(default="anthropic")
    llm_model: str = Field(default="claude-opus-5-5")
    # Endpoint clusters sent to the LLM per request. Smaller batches keep responses
    # well under the output token limit.
    analyze_batch_size: int = Field(default=12)
    max_concurrent_requests: int = Field(default=10)
    rate_limit_per_minute: int = Field(default=60)
    # When a tool gets HTTP 401/403, renew the login in a hidden browser and retry once
    auto_relogin: bool = Field(default=True)
    log_level: str = Field(default="INFO")
    server_host: str = Field(default="127.0.0.1")
    server_port: int = Field(default=8000)
    min_confidence_threshold: float = Field(default=0.5)

    # Domains to drop during analysis (analytics, tracking, feature-flag noise).
    # Matched against the request host and its parent domains.
    blocked_domains: list[str] = Field(default=[
        "google-analytics.com", "googletagmanager.com", "doubleclick.net",
        "facebook.com", "facebook.net", "hotjar.com", "mixpanel.com", "segment.io",
        "segment.com", "amplitude.com", "datadog-browser-agent.com", "browser-intake-datadoghq.com",
        "sentry.io", "bugsnag.com", "clarity.ms", "nr-data.net", "newrelic.com",
        "fullstory.com", "heapanalytics.com", "logrocket.io", "launchdarkly.com",
        "intercom.io", "intercomcdn.com",
    ])

    @model_validator(mode="after")
    def _resolve_paths(self) -> "Settings":
        for name in ("db_path", "generated_specs_dir", "generated_tools_dir"):
            value: Path = getattr(self, name)
            if not value.is_absolute():
                setattr(self, name, HOME / value)
        return self


settings = Settings()
