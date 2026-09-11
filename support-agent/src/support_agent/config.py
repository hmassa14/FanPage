"""Runtime configuration. Everything comes from env vars / .env; nothing is hardcoded.

Model policy: Claude Opus 5 for every stage by default. Per-stage overrides exist so you
can measure a cheaper model on one stage at a time rather than guessing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class StageModelConfig(BaseModel):
    model: str = "claude-opus-5"
    effort: Effort = "high"
    max_tokens: int = 16000


# USD per 1M tokens: (input, output). Cache reads are 0.1x input, cache writes 1.25x input.
MODEL_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="SA_", env_nested_delimiter="__", extra="ignore"
    )

    # --- provider -----------------------------------------------------------
    llm_provider: Literal["anthropic", "fake"] = "fake"
    anthropic_fallbacks: bool = Field(
        default=True,
        description="Send fallbacks='default' so policy refusals are re-run server-side.",
    )
    triage: StageModelConfig = StageModelConfig(effort="low", max_tokens=4000)
    research: StageModelConfig = StageModelConfig(effort="high", max_tokens=16000)
    draft: StageModelConfig = StageModelConfig(effort="high", max_tokens=8000)
    judge: StageModelConfig = StageModelConfig(effort="medium", max_tokens=4000)
    research_max_tool_rounds: int = 8

    # --- company persona ----------------------------------------------------
    company_name: str = "Northwind Outfitters"
    support_address: str = "support@northwind-outfitters.example"
    agent_signature: str = "Northwind Outfitters Support"

    # --- storage / data -----------------------------------------------------
    db_path: Path = PROJECT_ROOT / "data" / "support_agent.sqlite3"
    kb_dir: Path = PROJECT_ROOT / "data" / "kb"
    crm_dir: Path = PROJECT_ROOT / "data" / "crm"
    inbox_dir: Path = PROJECT_ROOT / "data" / "inbox"
    policy_file: Path = PROJECT_ROOT / "data" / "policy.yaml"

    # --- delivery -----------------------------------------------------------
    sender: Literal["console", "smtp", "file"] = "console"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = True
    outbox_dir: Path = PROJECT_ROOT / "data" / "outbox"
    send_max_attempts: int = 5

    # --- ingestion (IMAP is optional; file inbox is the default) ------------
    imap_host: str | None = None
    imap_username: str | None = None
    imap_password: str | None = None
    imap_folder: str = "INBOX"
    worker_poll_seconds: float = 5.0

    # --- api ----------------------------------------------------------------
    admin_token: str = Field(
        default="change-me",
        description="Bearer token for the approval API. Replace in production.",
    )
    log_json: bool = True
    log_level: str = "INFO"

    # --- tracing ------------------------------------------------------------
    otel_exporter: Literal["none", "console", "otlp"] = "none"
    otel_endpoint: str = "http://localhost:4318"
    otel_service_name: str = "support-agent"

    def price(self, model: str) -> tuple[float, float]:
        return MODEL_PRICES_PER_MTOK.get(model, (0.0, 0.0))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
