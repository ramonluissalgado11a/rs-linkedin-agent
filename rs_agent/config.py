"""Centralized configuration loaded from environment variables.

Nothing here is hardcoded that could go stale: the LinkedIn API version and
Claude model IDs are configuration, with defaults that were current when this
file was written (LinkedIn versioning docs listed 202605 as latest).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_decimal(name: str, default: str) -> Decimal:
    raw = _env(name, default)
    assert raw is not None
    try:
        return Decimal(raw)
    except Exception as exc:  # noqa: BLE001 - decimal raises several types
        raise ConfigError(f"{name} must be a number") from exc


# Hard ceilings: configuration can lower these, never raise them.
HARD_CEILINGS: dict[str, tuple[int, int]] = {
    # action_type: (max per hour, max per day)
    "CREATE_POST": (1, 2),
    "CREATE_ORG_POST": (1, 2),
}


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: Decimal
    output_per_mtok: Decimal


@dataclass(frozen=True)
class Settings:
    env: str
    database_url: str
    admin_token: str
    session_secret: str
    token_encryption_key: str | None
    public_base_url: str

    linkedin_client_id: str | None
    linkedin_client_secret: str | None
    linkedin_redirect_uri: str | None
    linkedin_api_version: str
    linkedin_scopes: tuple[str, ...]
    linkedin_auto_publish: bool

    anthropic_api_key: str | None
    claude_model_complex: str
    claude_model_simple: str
    claude_timeout_s: int
    model_prices: dict[str, ModelPrice] = field(default_factory=dict)

    daily_budget_usd: Decimal = Decimal("2.00")
    monthly_budget_usd: Decimal = Decimal("40.00")

    posts_per_day: int = 1
    job_max_attempts: int = 5
    job_timeout_s: int = 60
    worker_poll_s: int = 5

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def linkedin_configured(self) -> bool:
        return bool(self.linkedin_client_id and self.linkedin_client_secret and self.linkedin_redirect_uri)

    @property
    def claude_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    def rate_limit(self, action_type: str) -> tuple[int, int]:
        """Per-hour and per-day limits, never above the hard ceilings."""
        hour_cap, day_cap = HARD_CEILINGS.get(action_type, (0, 0))
        if action_type in ("CREATE_POST", "CREATE_ORG_POST"):
            day_cap = min(day_cap, max(self.posts_per_day, 0))
        return hour_cap, day_cap


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Load settings; `environ` lets tests inject values without touching os.environ."""
    if environ is not None:
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(environ)
    try:
        prices = {
            # USD per million tokens. Verify against current pricing and override via env.
            _env("CLAUDE_MODEL_COMPLEX", "claude-opus-5-5") or "": ModelPrice(
                _env_decimal("CLAUDE_COMPLEX_INPUT_PER_MTOK", "15"),
                _env_decimal("CLAUDE_COMPLEX_OUTPUT_PER_MTOK", "75"),
            ),
            _env("CLAUDE_MODEL_SIMPLE", "claude-haiku-5-5") or "": ModelPrice(
                _env_decimal("CLAUDE_SIMPLE_INPUT_PER_MTOK", "1"),
                _env_decimal("CLAUDE_SIMPLE_OUTPUT_PER_MTOK", "5"),
            ),
        }
        scopes_raw = _env("LINKEDIN_SCOPES", "openid profile w_member_social") or ""
        settings = Settings(
            env=_env("APP_ENV", "development") or "development",
            database_url=_env("DATABASE_URL", "sqlite:///./rs_agent.db") or "",
            admin_token=_env("ADMIN_TOKEN", "") or "",
            session_secret=_env("SESSION_SECRET", "") or "",
            token_encryption_key=_env("TOKEN_ENCRYPTION_KEY"),
            public_base_url=(_env("PUBLIC_BASE_URL", "http://localhost:8000") or "").rstrip("/"),
            linkedin_client_id=_env("LINKEDIN_CLIENT_ID"),
            linkedin_client_secret=_env("LINKEDIN_CLIENT_SECRET"),
            linkedin_redirect_uri=_env("LINKEDIN_REDIRECT_URI"),
            linkedin_api_version=_env("LINKEDIN_API_VERSION", "202605") or "202605",
            linkedin_scopes=tuple(s for s in scopes_raw.replace(",", " ").split() if s),
            linkedin_auto_publish=_env_bool("LINKEDIN_AUTO_PUBLISH", False),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            claude_model_complex=_env("CLAUDE_MODEL_COMPLEX", "claude-opus-5-5") or "",
            claude_model_simple=_env("CLAUDE_MODEL_SIMPLE", "claude-haiku-5-5") or "",
            claude_timeout_s=_env_int("CLAUDE_TIMEOUT_S", 60),
            model_prices=prices,
            daily_budget_usd=_env_decimal("DAILY_BUDGET_USD", "2.00"),
            monthly_budget_usd=_env_decimal("MONTHLY_BUDGET_USD", "40.00"),
            posts_per_day=_env_int("POSTS_PER_DAY", 1),
            job_max_attempts=_env_int("JOB_MAX_ATTEMPTS", 5),
            job_timeout_s=_env_int("JOB_TIMEOUT_S", 60),
            worker_poll_s=_env_int("WORKER_POLL_S", 5),
        )
    finally:
        if environ is not None:
            os.environ.clear()
            os.environ.update(saved)
    return settings


def validate_settings(settings: Settings) -> list[str]:
    """Return a list of problems. In production these block startup."""
    problems: list[str] = []
    if len(settings.admin_token) < 32:
        problems.append("ADMIN_TOKEN must be at least 32 characters")
    if len(settings.session_secret) < 32:
        problems.append("SESSION_SECRET must be at least 32 characters")
    if not settings.linkedin_api_version.isdigit() or len(settings.linkedin_api_version) != 6:
        problems.append("LINKEDIN_API_VERSION must be YYYYMM")
    if settings.linkedin_configured and not settings.token_encryption_key:
        problems.append("TOKEN_ENCRYPTION_KEY is required when LinkedIn OAuth is configured")
    if settings.is_production and settings.database_url.startswith("sqlite"):
        problems.append("DATABASE_URL must be PostgreSQL in production (hosted filesystems are ephemeral; SQLite data would be lost)")
    if settings.is_production and settings.public_base_url.startswith("http://"):
        problems.append("PUBLIC_BASE_URL must use https in production")
    if settings.linkedin_redirect_uri and settings.is_production and not settings.linkedin_redirect_uri.startswith("https://"):
        problems.append("LINKEDIN_REDIRECT_URI must use https in production")
    if settings.daily_budget_usd <= 0 or settings.monthly_budget_usd <= 0:
        problems.append("Budgets must be positive")
    if settings.job_max_attempts < 1 or settings.job_max_attempts > 10:
        problems.append("JOB_MAX_ATTEMPTS must be between 1 and 10")
    return problems
