"""Application settings, loaded from environment variables (or a local .env file).

Every tunable lives here so the rest of the code never reads os.environ directly.
That makes behaviour explicit and lets tests override settings in one place.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- storage -------------------------------------------------------------
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/tally"
    redis_url: str = "redis://localhost:6379/0"

    # --- queue ---------------------------------------------------------------
    # "postgres": jobs table + SELECT ... FOR UPDATE SKIP LOCKED (transactional enqueue)
    # "redis":    Redis Streams consumer group (faster, but enqueue is a dual write)
    queue_backend: Literal["postgres", "redis"] = "postgres"
    lease_seconds: int = 120          # how long a worker owns a job before others may reclaim it
    max_attempts: int = 5             # deliveries before a job is dead-lettered
    worker_concurrency: int = 2       # worker threads per process

    # --- LLM extraction ------------------------------------------------------
    llm_provider: Literal["gemini", "groq", "openrouter", "rules"] = "rules"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-flash-latest"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    openrouter_api_key: str | None = None
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    llm_requests_per_minute: float = 8.0   # free tiers are tight; stay under them
    llm_timeout_seconds: float = 30.0
    review_confidence_threshold: float = 0.6

    # --- market data & scoring ----------------------------------------------
    market_data_provider: Literal["yfinance", "synthetic"] = "yfinance"
    default_horizon_days: int = 20    # trading days, used when a post gives no horizon
    normalisation_horizon_days: int = 20

    # --- embedded mode (single free-tier web service runs everything) --------
    embedded_worker: bool = False
    embedded_scheduler: bool = False
    scoring_interval_minutes: int = 360
    source_poll_interval_minutes: int = 60

    # --- sources -------------------------------------------------------------
    # Off by default: every polled post that mentions a ticker costs one LLM call, and
    # free tiers allow only tens to hundreds of calls a day.
    reddit_subreddits: str = ""      # e.g. "stocks,ValueInvesting" (fetched via Reddit RSS)
    substack_publications: str = ""  # e.g. "stocksunderrocks,stockpicking"
    stocktwits_streams: str = ""     # e.g. "trending,NVDA"
    rss_feeds: str = ""              # e.g. "https://stocksunderrocks.substack.com/feed"

    # --- API / security ------------------------------------------------------
    admin_token: str = "change-me"
    cors_origins: str = "http://localhost:5173"
    submit_rate_per_minute: float = 10.0    # per client IP, protects the LLM quota
    max_post_chars: int = 5000

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        # Render/Neon/Heroku hand out postgres:// URLs; SQLAlchemy needs the driver named.
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def subreddit_list(self) -> list[str]:
        return [s.strip() for s in self.reddit_subreddits.split(",") if s.strip()]

    @property
    def substack_list(self) -> list[str]:
        return [s.strip() for s in self.substack_publications.split(",") if s.strip()]

    @property
    def stocktwits_list(self) -> list[str]:
        return [s.strip() for s in self.stocktwits_streams.split(",") if s.strip()]

    @property
    def rss_feed_list(self) -> list[str]:
        return [s.strip() for s in self.rss_feeds.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
