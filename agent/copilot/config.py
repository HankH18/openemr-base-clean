"""Runtime configuration.

Every value comes from the environment (or a `.env` file for local dev).
Secrets never live in the repo. See RUNLOG.md's operator-action queue for
the credentials the operator must inject before the service can do its job
against a real backend.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide config, loaded once and cached."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="COPILOT_",
        extra="ignore",
    )

    # --- Database ---------------------------------------------------------

    database_url: str = Field(
        default="sqlite+aiosqlite:///:memory:",
        description=(
            "SQLAlchemy async URL. Production: postgresql+psycopg://...  "
            "Tests default to an in-memory aiosqlite DB."
        ),
    )

    # --- OpenEMR / FHIR ---------------------------------------------------

    fhir_base_url: str = Field(
        default="http://openemr/apis/default/fhir",
        description=(
            "Base URL for OpenEMR's FHIR endpoints. Inside compose network "
            "the openemr service is reachable as http://openemr."
        ),
    )
    oauth_token_url: str = Field(
        default="http://openemr/oauth2/default/token",
        description="OpenEMR OAuth2 token endpoint.",
    )
    oauth_authorize_url: str = Field(
        default="http://openemr/oauth2/default/authorize",
        description="OpenEMR OAuth2 authorization endpoint (SMART App Launch).",
    )

    # These are IDs, not secrets — the client secret / JWKs live in a
    # secrets manager (see RUNLOG operator queue).
    smart_app_client_id: str = Field(default="", description="SMART App Launch client_id.")
    backend_services_client_id: str = Field(default="", description="Backend Services client_id.")

    # --- LLM --------------------------------------------------------------

    anthropic_api_key: str = Field(
        default="", description="Set via env; without it, LLM calls raise a documented error."
    )
    anthropic_model_synthesis: str = Field(
        default="claude-sonnet-4-6", description="Model for synthesis and chat."
    )
    anthropic_model_gating: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Cheaper model for classification / entailment.",
    )

    # --- Observability ----------------------------------------------------

    langfuse_host: str = Field(
        default="", description="Langfuse endpoint. Empty ⇒ observability no-op."
    )
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")

    # --- Poller -----------------------------------------------------------

    poll_interval_seconds: int = Field(
        default=300, description="Seconds between poller ticks. ARCHITECTURE calls for 5-15 min."
    )

    # --- Alerting ---------------------------------------------------------

    acuity_alert_threshold: float = Field(
        default=7.0,
        description=(
            "Acuity score at or above which the deterioration-alert feature "
            "raises an alert. Consumed by the alerting path added later."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton."""
    return Settings()
