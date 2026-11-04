"""App settings, read from environment variables (and .env at the project root)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    guardrail_model: str = os.getenv("GUARDRAIL_MODEL", "gpt-4.1-mini")
    planner_model: str = os.getenv("PLANNER_MODEL", "gpt-4.1")
    planner_max_attempts: int = int(os.getenv("PLANNER_MAX_ATTEMPTS", "3"))
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))


settings = Settings()
