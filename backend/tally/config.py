# =============================================================================
# config.py: ALL SETTINGS IN ONE PLACE
# -----------------------------------------------------------------------------
# What it is:  One class (Settings) listing every knob the app has: database URL,
#              which LLM to use, API keys, how often to poll sources, and so on.
# How values get in: pydantic-settings reads them from environment variables, or
#              from the file backend/.env. Each field name matches an env var
#              (field `groq_api_key` <- env var GROQ_API_KEY; case doesn't matter).
#              If an env var is missing, the default written here is used.
# Who uses it: almost everything calls get_settings() (bottom of the file).
# Why it exists: the rest of the code never touches os.environ directly. One
#              place to look, and tests can override settings easily.
# =============================================================================
"""Application settings, loaded from environment variables (or a local .env file).

Every tunable lives here so the rest of the code never reads os.environ directly.
That makes behaviour explicit and lets tests override settings in one place.
"""
# Lets us write type hints like `str | None` on older Pythons and avoids
# evaluating hints at import time. You'll see this line at the top of every file.
from __future__ import annotations

from functools import lru_cache      # remembers a function's result (used at the bottom)
from typing import Literal           # "this string must be one of these exact values"

from pydantic import field_validator                         # hook to clean up one field
from pydantic_settings import BaseSettings, SettingsConfigDict  # the env-var reading magic


# Inheriting from BaseSettings is what makes this class read env vars. Each class
# attribute below is (name: type = default). Pydantic also CHECKS the type: if
# LEASE_SECONDS="abc" the app refuses to start instead of crashing later.
class Settings(BaseSettings):
    # Where to read from: a file called ".env" in the current folder (backend/).
    # extra="ignore": unknown variables in .env are ignored instead of raising an error.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- storage -------------------------------------------------------------
    # The Postgres connection string. In production this is the Neon URL.
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/tally"
    # Only used if QUEUE_BACKEND=redis.
    redis_url: str = "redis://localhost:6379/0"

    # --- queue ---------------------------------------------------------------
    # "postgres": jobs table + SELECT ... FOR UPDATE SKIP LOCKED (transactional enqueue)
    # "redis":    Redis Streams consumer group (faster, but enqueue is a dual write)
    queue_backend: Literal["postgres", "redis"] = "postgres"
    lease_seconds: int = 120          # how long a worker owns a job before others may reclaim it
    max_attempts: int = 5             # deliveries before a job is dead-lettered
    worker_concurrency: int = 2       # worker threads per process

    # --- LLM extraction ------------------------------------------------------
    # Which extractor to build (see extraction/extractors.py -> build_extractor).
    # "rules" = no LLM at all, the keyword baseline. Safe default: works with no API key.
    llm_provider: Literal["gemini", "groq", "openrouter", "rules"] = "rules"
    gemini_api_key: str | None = None      # `str | None` = optional; None means "not set"
    gemini_model: str = "gemini-flash-latest"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    openrouter_api_key: str | None = None
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    llm_requests_per_minute: float = 8.0   # free tiers are tight; stay under them
    llm_timeout_seconds: float = 30.0      # give up on one LLM HTTP call after this long
    review_confidence_threshold: float = 0.6   # LLM confidence below this -> human review

    # --- market data & scoring ----------------------------------------------
    # "synthetic" = fake but deterministic prices, for offline development and tests.
    market_data_provider: Literal["yfinance", "synthetic"] = "yfinance"
    default_horizon_days: int = 20    # trading days, used when a post gives no horizon
    normalisation_horizon_days: int = 20   # returns are scaled to "per 20 trading days"

    # --- embedded mode (single free-tier web service runs everything) --------
    # On Render's free plan we only get ONE process. These flags make the web server
    # also start worker threads and a scheduler thread inside itself (see runtime.py).
    embedded_worker: bool = False
    embedded_scheduler: bool = False
    scoring_interval_minutes: int = 360        # scheduler runs scoring every 6 hours
    source_poll_interval_minutes: int = 60     # scheduler polls configured sources hourly

    # --- sources -------------------------------------------------------------
    # Off by default: every polled post that mentions a ticker costs one LLM call, and
    # free tiers allow only tens to hundreds of calls a day.
    # Stored as comma-separated strings because env vars are plain text; the
    # *_list properties below split them into Python lists.
    reddit_subreddits: str = ""      # e.g. "stocks,ValueInvesting" (fetched via Reddit RSS)
    substack_publications: str = ""  # e.g. "stocksunderrocks,stockpicking"
    stocktwits_streams: str = ""     # e.g. "trending,NVDA"
    rss_feeds: str = ""              # e.g. "https://stocksunderrocks.substack.com/feed"

    # --- API / security ------------------------------------------------------
    admin_token: str = "change-me"           # password for admin endpoints (Render generates a real one)
    cors_origins: str = "http://localhost:5173"   # which websites may call this API from a browser
    submit_rate_per_minute: float = 10.0    # per client IP, protects the LLM quota
    max_post_chars: int = 5000              # reject giant posts (cost + abuse protection)

    # A validator runs whenever `database_url` is set and may rewrite the value.
    # @classmethod is required by pydantic for validators (it gets the class, not an instance).
    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        # Render/Neon/Heroku hand out postgres:// URLs; SQLAlchemy needs the driver named.
        # "postgresql+psycopg://" tells SQLAlchemy to use the psycopg (v3) driver library.
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    # @property = call it like a field (settings.cors_origin_list) but it's computed.
    # Each one turns "a, b,,c" into ["a", "b", "c"]: split on commas, trim spaces,
    # drop empty pieces. (List comprehension: [expr for item in list if condition].)
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


# @lru_cache: the first call builds Settings() (reads .env); every later call returns
# that SAME object instantly. So the whole app shares one settings object, and the
# .env file is read once, not on every request.
@lru_cache
def get_settings() -> Settings:
    return Settings()
