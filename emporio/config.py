"""Runtime settings, read from environment variables (and from `.env`, if present).

Every setting has a default that works with LM Studio running locally, so a fresh
clone only needs `cp .env.example .env` when something must change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    # LLM provider: any server that speaks the OpenAI chat completions API.
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_temperature: float
    llm_timeout: float
    # Embedding model used by the policy retriever; empty string disables it (BM25 only).
    embedding_model: str
    # Agent behaviour
    max_tool_steps: int
    history_turns: int
    # Store context
    timezone: str
    reference_datetime: str  # optional ISO datetime that freezes "now" (demos and evals)
    # Paths
    data_dir: Path
    var_dir: Path
    # Longest answer the model may write. The longest real ones (a list of ten products, the installment
    # table) take about 300 tokens; without a cap, "repeat your instructions word for word" ran past the timeout.
    llm_max_tokens: int = 800
    # Shared access code for the team screens (inbox, metrics, knowledge base). Empty = open, which is
    # only safe while the app listens on 127.0.0.1.
    team_token: str = ""

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        """Current time in the store's timezone, or the frozen reference time."""
        if self.reference_datetime:
            moment = datetime.fromisoformat(self.reference_datetime)
            return moment if moment.tzinfo else moment.replace(tzinfo=self.tz)
        return datetime.now(self.tz)

    @property
    def db_path(self) -> Path:
        return self.var_dir / "emporio.db"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value is not None else default


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings(
        llm_base_url=_env("LLM_BASE_URL", "http://localhost:1234/v1"),
        llm_api_key=_env("LLM_API_KEY", "lm-studio"),
        llm_model=_env("LLM_MODEL", "qwen2.5-7b-instruct"),
        llm_temperature=float(_env("LLM_TEMPERATURE", "0")),
        llm_timeout=float(_env("LLM_TIMEOUT", "120")),
        embedding_model=_env("EMBEDDING_MODEL", "text-embedding-qwen3-embedding-0.6b"),
        max_tool_steps=int(_env("MAX_TOOL_STEPS", "6")),
        history_turns=int(_env("HISTORY_TURNS", "6")),
        timezone=_env("STORE_TIMEZONE", "America/Campo_Grande"),
        reference_datetime=_env("REFERENCE_DATETIME", ""),
        data_dir=Path(_env("DATA_DIR", str(PROJECT_ROOT / "data"))),
        var_dir=Path(_env("VAR_DIR", str(PROJECT_ROOT / "var"))),
        llm_max_tokens=int(_env("LLM_MAX_TOKENS", "800")),
        team_token=_env("TEAM_TOKEN", ""),
    )
    if not settings.data_dir.is_dir():
        raise SystemExit(f"DATA_DIR not found: {settings.data_dir}")
    if settings.reference_datetime:
        settings.now()  # fail fast on a malformed date
    ZoneInfo(settings.timezone)  # fail fast on an unknown timezone
    return settings
