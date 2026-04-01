from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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
    llm_model: str = Field(default="claude-opus-4-5")
    max_concurrent_requests: int = Field(default=10)
    log_level: str = Field(default="INFO")
    server_host: str = Field(default="127.0.0.1")
    server_port: int = Field(default=8000)
    min_confidence_threshold: float = Field(default=0.5)

    # Domains to block during recording (analytics, CDN noise)
    blocked_domains: list[str] = Field(default=[
        "google-analytics.com", "googletagmanager.com", "facebook.com",
        "hotjar.com", "mixpanel.com", "segment.io", "amplitude.com",
        "datadog-browser-agent.com", "sentry.io", "bugsnag.com",
        "cloudfront.net", "fastly.net",
    ])


settings = Settings()
