"""Configuration. Fails fast at boot on any missing required variable.

Required-ness is per entry point, not global: the CLI sweep needs Places and
Firestore, the audit worker needs the renderer and PageSpeed. Ask for what you
need with `require()` rather than demanding the whole file up front, so day-one
work is not blocked by a Friday variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env")


class ConfigError(RuntimeError):
    """A required configuration value is missing or unusable."""


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).upper()
    if not raw:
        return default
    return raw in {"1", "TRUE", "YES", "ON"}


@dataclass(frozen=True)
class Config:
    # Vertex / model
    use_vertexai: bool = field(default_factory=lambda: _bool("GOOGLE_GENAI_USE_VERTEXAI", True))
    project: str = field(default_factory=lambda: _str("GOOGLE_CLOUD_PROJECT"))
    location: str = field(default_factory=lambda: _str("GOOGLE_CLOUD_LOCATION", "us-central1"))
    # gemini-3.5-flash is served from the global endpoint only. Keep this
    # separate from `location`, which is where Firestore, GCS and Cloud Run live.
    model_location: str = field(default_factory=lambda: _str("VERTEX_MODEL_LOCATION", "global"))
    gemini_model: str = field(default_factory=lambda: _str("GEMINI_MODEL", "gemini-3.5-flash"))
    google_api_key: str = field(default_factory=lambda: _str("GOOGLE_API_KEY"))

    # Firestore
    firestore_database: str = field(default_factory=lambda: _str("FIRESTORE_DATABASE", "(default)"))

    # Services
    renderer_url: str = field(default_factory=lambda: _str("RENDERER_URL"))
    renderer_shared_secret: str = field(default_factory=lambda: _str("RENDERER_SHARED_SECRET"))

    # External APIs
    places_api_key: str = field(default_factory=lambda: _str("GOOGLE_PLACES_API_KEY"))
    pagespeed_api_key: str = field(default_factory=lambda: _str("PAGESPEED_API_KEY"))
    meta_ads_access_token: str = field(default_factory=lambda: _str("META_ADS_ACCESS_TOKEN"))

    # Storage
    gcs_evidence_bucket: str = field(default_factory=lambda: _str("GCS_EVIDENCE_BUCKET"))
    report_ip_salt: str = field(default_factory=lambda: _str("REPORT_IP_SALT"))

    # Crawl conduct. Defaults are the values in the criteria doc, section 8.
    crawl_user_agent: str = field(
        default_factory=lambda: _str(
            "CRAWL_USER_AGENT",
            "RelayAuditBot/1.0 (+https://relayforroofers.com/bot)",
        )
    )
    crawl_max_pages: int = field(default_factory=lambda: _int("CRAWL_MAX_PAGES", 25))
    crawl_requests_per_second: float = field(
        default_factory=lambda: _float("CRAWL_REQUESTS_PER_SECOND", 2.0)
    )
    crawl_concurrency_per_host: int = field(
        default_factory=lambda: _int("CRAWL_CONCURRENCY_PER_HOST", 2)
    )
    crawl_timeout_seconds: float = field(default_factory=lambda: _float("CRAWL_TIMEOUT_SECONDS", 15.0))

    max_concurrent_audits: int = field(default_factory=lambda: _int("MAX_CONCURRENT_AUDITS", 4))

    # Pub/Sub
    pubsub_audit_topic: str = field(
        default_factory=lambda: _str("PUBSUB_AUDIT_TOPIC", "run-audit")
    )
    pubsub_audit_subscription: str = field(
        default_factory=lambda: _str("PUBSUB_AUDIT_SUBSCRIPTION", "run-audit-push")
    )
    pubsub_dead_letter_topic: str = field(
        default_factory=lambda: _str("PUBSUB_DEAD_LETTER_TOPIC", "run-audit-dlq")
    )
    pubsub_job_topic: str = field(
        default_factory=lambda: _str("PUBSUB_JOB_TOPIC", "run-job")
    )
    worker_shared_secret: str = field(default_factory=lambda: _str("WORKER_SHARED_SECRET"))
    # Separate from the Pub/Sub push token on purpose. That one is a machine
    # credential rotated without notice; this one is what a person types, so it
    # should be a password the operator picked, not a generated 40 character
    # token they have to look up in Secret Manager every time.
    console_password: str = field(
        default_factory=lambda: _str("CONSOLE_PASSWORD") or _str("WORKER_SHARED_SECRET")
    )

    def require(self, *names: str) -> None:
        """Raise unless every named attribute is set. Call at entry points."""
        missing = [name for name in names if not getattr(self, name)]
        if missing:
            env_names = ", ".join(_ENV_NAME.get(name, name.upper()) for name in missing)
            raise ConfigError(
                f"Missing required configuration: {env_names}. "
                "Copy .env.example to .env and fill these in."
            )


_ENV_NAME = {
    "project": "GOOGLE_CLOUD_PROJECT",
    "location": "GOOGLE_CLOUD_LOCATION",
    "model_location": "VERTEX_MODEL_LOCATION",
    "gemini_model": "GEMINI_MODEL",
    "google_api_key": "GOOGLE_API_KEY",
    "renderer_url": "RENDERER_URL",
    "renderer_shared_secret": "RENDERER_SHARED_SECRET",
    "places_api_key": "GOOGLE_PLACES_API_KEY",
    "pagespeed_api_key": "PAGESPEED_API_KEY",
    "meta_ads_access_token": "META_ADS_ACCESS_TOKEN",
    "gcs_evidence_bucket": "GCS_EVIDENCE_BUCKET",
    "report_ip_salt": "REPORT_IP_SALT",
    "pubsub_audit_topic": "PUBSUB_AUDIT_TOPIC",
    "worker_shared_secret": "WORKER_SHARED_SECRET",
    "console_password": "CONSOLE_PASSWORD",
}


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
