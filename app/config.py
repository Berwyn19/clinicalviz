"""App settings, read from environment variables (and .env at the project root)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# override=True: the project's .env wins over variables already exported in the shell
# (e.g. a global OPENAI_API_KEY for another account).
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    guardrail_model: str = os.getenv("GUARDRAIL_MODEL", "gpt-4.1-mini")
    planner_model: str = os.getenv("PLANNER_MODEL", "gpt-4.1")
    planner_max_attempts: int = int(os.getenv("PLANNER_MAX_ATTEMPTS", "3"))
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))

    # ClinicalTrials.gov API
    ctgov_requests_per_minute: int = int(os.getenv("CTGOV_REQUESTS_PER_MINUTE", "50"))
    ctgov_max_retries: int = int(os.getenv("CTGOV_MAX_RETRIES", "4"))
    ctgov_timeout_seconds: float = float(os.getenv("CTGOV_TIMEOUT_SECONDS", "30"))
    ctgov_cache_enabled: bool = os.getenv("CTGOV_CACHE_ENABLED", "true").lower() == "true"
    ctgov_cache_dir: str = os.getenv("CTGOV_CACHE_DIR", ".cache/ctgov")
    ctgov_cache_ttl_seconds: int = int(os.getenv("CTGOV_CACHE_TTL_SECONDS", "86400"))
    # Max tiny count requests per question when a truncated download is replaced by exact counts.
    ctgov_exact_count_max_requests: int = int(os.getenv("CTGOV_EXACT_COUNT_MAX_REQUESTS", "150"))


settings = Settings()