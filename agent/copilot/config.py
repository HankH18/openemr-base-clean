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
    tls_verify: bool = Field(
        default=True,
        description=(
            "Verify TLS on outbound OpenEMR calls. Keep True in prod (real cert "
            "behind the proxy); set False only for a local self-signed dev stack."
        ),
    )
    fhir_patient_id_template: str = Field(
        default="",
        description=(
            "Optional template mapping the agent's integer patient id to the "
            "OpenEMR FHIR Patient UUID in search queries, e.g. "
            "'a1000000-0000-0000-0000-{pid:012d}'. Empty ⇒ use the id verbatim "
            "(the acceptance fake + tests key by integer id)."
        ),
    )
    cors_allow_origins: str = Field(
        default="",
        description=(
            "Comma-separated allowed CORS origins for a split-origin UI (or a "
            "local browser demo). Empty ⇒ no CORS middleware; the same-origin "
            "proxy deploy needs none."
        ),
    )

    # These are IDs, not secrets — the client secret / JWKs live in a
    # secrets manager (see RUNLOG operator queue).
    smart_app_client_id: str = Field(default="", description="SMART App Launch client_id.")
    backend_services_client_id: str = Field(default="", description="Backend Services client_id.")
    backend_services_private_key_path: str = Field(
        default="",
        description=(
            "Path to the Backend Services client private key (PEM). With a "
            "client_id, this activates the real client_credentials + "
            "private_key_jwt flow. Empty ⇒ stub bearer (offline/tests)."
        ),
    )
    backend_services_scopes: str = Field(
        default="",
        description="Space-separated system/*.read scopes for the poller client. Empty ⇒ a default read set.",
    )
    oauth_audience: str = Field(
        default="",
        description="Audience claim for the JWT assertion. Empty ⇒ the token URL.",
    )

    # --- Write-back (physician direct-edit) -------------------------------

    writeback_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for physician write-back. Defaults OFF so the live "
            "app is byte-for-byte unchanged until an operator opts in. The write "
            "client/token provider refuse to be built while this is false."
        ),
    )
    write_client_id: str = Field(
        default="",
        description=(
            "client_id of the DEDICATED write client (password grant) — never "
            "the read poller's client. An ID, not a secret."
        ),
    )
    write_client_secret: str = Field(
        default="",
        description="Secret for the confidential write client. Secrets-manager only; never logged.",
    )
    write_username: str = Field(
        default="",
        description="Dedicated OpenEMR write user (e.g. 'copilot_writer') for the password grant.",
    )
    write_password: str = Field(
        default="",
        description="Password for the dedicated write user. Secrets-manager only; never logged.",
    )
    write_scopes: str = Field(
        default="",
        description=(
            "Space-separated user/* scopes for the write client, e.g. "
            "'openid offline_access api:oemr user/vital.crus user/encounter.crus "
            "user/medication.cruds'. Empty ⇒ the token endpoint's default."
        ),
    )
    write_api_base_url: str = Field(
        default="",
        description=(
            "Standard REST API base (…/apis/default/api). Empty ⇒ derived from "
            "fhir_base_url by swapping the trailing '/fhir' for '/api'."
        ),
    )

    # --- LLM --------------------------------------------------------------

    anthropic_api_key: str = Field(
        default="", description="Set via env; without it, LLM calls raise a documented error."
    )
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com",
        description=(
            "Base URL for the Anthropic API. Overridable to point at a gateway/"
            "proxy — or at a test double so readiness probes can verify "
            "reachability without hitting the real provider."
        ),
    )
    anthropic_model_synthesis: str = Field(
        default="claude-sonnet-5", description="Model for synthesis and chat."
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

    poller_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the background poller. Defaults OFF so the "
            "service boots identically in every environment that has not "
            "opted in; set COPILOT_POLLER_ENABLED=true to run the change-gated "
            "poll loop in the app lifespan."
        ),
    )
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
