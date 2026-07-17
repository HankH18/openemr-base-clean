"""Runtime configuration.

Every value comes from the environment (or a `.env` file for local dev).
Secrets never live in the repo. See RUNLOG.md's operator-action queue for
the credentials the operator must inject before the service can do its job
against a real backend.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide config, loaded once and cached."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="COPILOT_",
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def _blank_env_means_unset(cls, data: Any) -> Any:
        """An empty env var means "unset" for a non-``str`` field, not "invalid".

        ``${COPILOT_OCR_DPI:-}`` is THE standard compose idiom for passing an
        optional knob through, and it hands the container an EMPTY STRING when the
        operator has not set it. Pydantic then parses ``""`` into ``int``/``bool``
        and raises — so the app does not start. Measured: 8 of the 9 knobs an
        operator would most want to tune (ocr_dpi, the raster caps, tls_verify,
        the session timeouts, chat retention) hard-brick the boot that way, with
        nothing to warn them. The idiom is not exotic; it is what the compose docs
        teach, and it means "I am not setting this".

        So an empty value for a typed field is dropped here and the field's default
        applies — which is what the operator asked for.

        Deliberately scoped to non-``str`` fields. For a plain ``str`` setting, ""
        is a MEANINGFUL value the code already reads (an empty ``anthropic_api_key``
        selects the keyless stub; an empty ``fhir_patient_id_template`` means "no
        mapping configured" and makes the write client refuse). Dropping those would
        silently swap a deliberate "off" for a default, which is the opposite of the
        bug being fixed.
        """
        if not isinstance(data, dict):
            return data
        cleaned: dict[str, Any] = {}
        for key, value in data.items():
            if value == "":
                field = cls.model_fields.get(key)
                if field is not None and field.annotation is not str:
                    continue  # non-str field, empty env var -> use the default
            cleaned[key] = value
        return cleaned

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
    public_base_url: str = Field(
        default="",
        description=(
            "Public origin the browser reaches the app at, e.g. "
            "'https://agentforge.example.com'. Single source of truth for the "
            "SMART redirect_uri (${public_base_url}/v1/auth/callback) and the "
            "post-login redirect. Empty ⇒ SMART login cannot be enabled (a "
            "startup check refuses auth_mode=smart without an https origin)."
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

    # --- Auth / session (per-physician SMART login) -----------------------
    #
    # See agent/research/PRODUCTION_GRADE_PLAN.md §1. The whole SMART login
    # re-architecture is staged behind auth_mode. Default "disabled" keeps the
    # current no-login demo byte-for-byte unchanged: identity comes from the
    # request (body/query clinician_id) exactly as today. "smart" resolves the
    # clinician from an opaque server-side session established by the SMART
    # authorization_code flow, and interactive reads/writes ride the logged-in
    # physician's delegated token. The poller ALWAYS keeps the system token.

    auth_mode: Literal["disabled", "smart"] = Field(
        default="disabled",
        description=(
            "Identity model. 'disabled' (default) ⇒ clinician_id comes from the "
            "request, no login — the current demo. 'smart' ⇒ per-physician SMART "
            "login; identity from a server-side session. Refuses to enable "
            "without an https public_base_url."
        ),
    )
    smart_app_client_secret: str = Field(
        default="",
        description=(
            "Secret for the CONFIDENTIAL SMART authorization_code (login) client. "
            "Secrets-manager only; never logged. Paired with smart_app_client_id."
        ),
    )
    smart_scopes: str = Field(
        default=(
            "openid fhirUser offline_access "
            "user/Patient.read user/Observation.read user/MedicationRequest.read "
            "user/Condition.read "
            "user/AllergyIntolerance.read user/Encounter.read "
            "user/DiagnosticReport.read "
            "api:oemr user/vital.crus user/encounter.crus user/medication.cruds"
        ),
        # NB: MedicationStatement is intentionally omitted — OpenEMR's FHIR
        # CapabilityStatement does not list it, so requesting user/
        # MedicationStatement.read makes both client registration AND the
        # authorize call fail with invalid_scope. Meds are read via
        # MedicationRequest. Keep this set a subset of OpenEMR's supported
        # resources (see /apis/default/fhir/metadata).
        description=(
            "Space-separated scopes requested at authorize time for the "
            "per-physician login. One authorization_code token carries both the "
            "user/*.read interactive reads and the api:oemr user/*.crus(d) write "
            "surface, so it serves reads AND writes (retiring the password grant)."
        ),
    )
    session_enc_key: str = Field(
        default="",
        description=(
            "Fernet key (32-byte urlsafe base64) that encrypts the physician "
            "OAuth tokens at rest in physician_session. Secrets-manager only. "
            "Required when auth_mode=smart."
        ),
    )
    session_cookie_name: str = Field(
        default="af_session",
        description="Name of the opaque, httpOnly session cookie.",
    )
    session_idle_seconds: int = Field(
        default=1800,
        description=(
            "Idle timeout for a physician session (automatic logoff, "
            "§164.312(a)(2)(iii)). Sliding — refreshed on activity."
        ),
    )
    session_absolute_seconds: int = Field(
        default=43200,
        description="Absolute session lifetime regardless of activity (12h default).",
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
    anthropic_model_vision: str = Field(
        default="claude-sonnet-5",
        description=(
            "Vision-capable model for structured extraction from document page "
            "images (scanned PDFs). Must have an explicit row in "
            "observability/pricing.py so extraction cost accounting resolves a "
            "real, nonzero rate — never the unknown-model fallback."
        ),
    )

    # --- Document ingestion & guideline RAG (Week 2) -----------------------

    voyage_api_key: str = Field(
        default="",
        description=(
            "Voyage AI API key for guideline-corpus embeddings (voyage-3.5). "
            "Empty (default) ⇒ the deterministic keyless embedding stub; no "
            "outbound calls, CI-safe. Secrets-manager only; never logged."
        ),
    )
    cohere_api_key: str = Field(
        default="",
        description=(
            "Cohere API key for retrieval reranking (rerank-v3.5). Empty "
            "(default) ⇒ the deterministic keyless rerank stub; no outbound "
            "calls, CI-safe. Secrets-manager only; never logged."
        ),
    )
    voyage_embedding_model: str = Field(
        default="voyage-3.5",
        description=(
            "Voyage embedding model id for the guideline RAG index. Must have "
            "an explicit row in observability/pricing.py."
        ),
    )
    cohere_rerank_model: str = Field(
        default="rerank-v3.5",
        description=(
            "Cohere rerank model id for guideline retrieval. Must have an "
            "explicit row in observability/pricing.py."
        ),
    )
    ocr_language: str = Field(
        default="eng",
        description=(
            "Tesseract language(s) for local OCR word-box extraction, e.g. "
            "'eng' or 'eng+spa'. OCR runs in-container so PHI never leaves "
            "the deployment for bounding boxes."
        ),
    )
    ocr_dpi: int = Field(
        default=200,
        description=(
            "Render DPI when rasterizing PDF pages for OCR and vision "
            "extraction. Higher ⇒ better OCR on small print, larger page "
            "images (stored + sent to the vision model)."
        ),
    )
    raster_max_page_pixels: int = Field(
        default=50_000_000,
        description=(
            "Per-page ceiling on RENDERED pixel area (width*height at ocr_dpi) "
            "when rasterizing a PDF. A page's MediaBox is uploader-controlled "
            "and costs bytes nothing while the render is quadratic in it: a "
            "544-byte PDF declaring a 60x60in page renders to 144 MP (~1.1 GB) "
            "at ocr_dpi=200, which OOM-kills a 2 GB box. The default (50 MP) "
            "clears every page a clinic plausibly scans — tabloid 11x17in at "
            "200 DPI is 7.5 MP, large-format ANSI D film is 29.9 MP — and "
            "costs ~200 MB of BGRA at the cap. Raise it on a larger box."
        ),
    )
    raster_max_pages: int = Field(
        default=1000,
        description=(
            "Ceiling on a document's page count when rasterizing. Page count is "
            "otherwise unbounded and a 2000-page bomb is a 244 KB upload. The "
            "default (1000) leaves 3.3x headroom over an ordinary 300-page "
            "discharge summary and 10x over a typical long one, so it rejects "
            "no real clinical document. Raise it on a larger box."
        ),
    )
    vision_max_pages_per_call: int = Field(
        default=20,
        ge=1,
        description=(
            "Maximum page images carried by a SINGLE vision model call. A "
            "document longer than this is extracted in batches of this size "
            "and the facts merged — the document is not refused (see "
            "documents/vision.py for why, and for how page_no stays true "
            "across batches). This bounds ONE CALL, not the document: it is "
            "not a spend cap, because every page is still sent exactly once. "
            "20 is measured against the API's real limits at the default "
            "ocr_dpi=200, where a US-Letter page renders to 1700x2200px: a "
            "request carrying MORE than 20 images is held to a stricter "
            "2000px per-image dimension limit, which a 2200px-tall page "
            "exceeds — so a 21-image call is rejected outright. It also keeps "
            "a call near ~13MB (limit 32MB) and ~96k visual tokens. RAISE "
            "ONLY WITH ocr_dpi <= 181, below which pages fall under 2000px."
        ),
    )
    doc_extraction_confidence_threshold: float = Field(
        default=0.7,
        description=(
            "Minimum OCR-reconciliation match confidence for an extracted "
            "value to count as document-grounded (bbox-anchored). Values below "
            "it are flagged low-confidence/unsupported — never silently "
            "trusted. Conservative by default; tune via the eval rubrics."
        ),
    )
    document_ingestion_enabled: bool = Field(
        default=True,
        description=(
            "Kill switch for the Week-2 document-ingestion HTTP surface: when "
            "false, POST /v1/documents returns 503 and no document is accepted. "
            "Gates the upload surface only — the pipeline service stays directly "
            "invocable (tests, CLI, background jobs), and already-ingested "
            "documents remain readable. Defaults TRUE because ingestion is a core "
            "Week-2 capability and is already live; the flag exists so an operator "
            "can actually disable intake (e.g. pending an incident). "
            "NOTE: this previously defaulted to false and was read NOWHERE — a "
            "phantom switch that made operators believe intake was off or "
            "disableable when it was neither. It is now genuinely enforced in "
            "api/routes/documents.py; the default preserves today's behavior."
        ),
    )
    chat_graph_enabled: bool = Field(
        default=False,
        description=(
            "Route serve-time chat turns through the hand-rolled multi-agent "
            "graph (supervisor/worker/critic) instead of the inline "
            "agent+verify path. Defaults OFF so a deployed app is byte-for-byte "
            "unchanged until an operator opts in; the fail-closed reply "
            "invariant is identical in both modes."
        ),
    )

    # --- Observability ----------------------------------------------------

    langfuse_host: str = Field(
        default="", description="Langfuse endpoint. Empty ⇒ observability no-op."
    )
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")
    observability_pseudonym_key: str = Field(
        default="",
        description=(
            "HMAC key that pseudonymizes patient_id before it egresses to the "
            "trace backend (Langfuse is a third-party SaaS; a bare PID is a "
            "HIPAA identifier). Any high-entropy string; keep it STABLE across "
            "deploys or traces stop correlating to a patient, and keep it out of "
            "Langfuse's reach. Empty (default) ⇒ patient_id is OMITTED from trace "
            "metadata rather than sent raw — safe, but traces can then only be "
            "grouped by correlation_id, not by patient. Secrets-manager only; "
            "never logged. See copilot/observability/pseudonymize.py."
        ),
    )

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

    # --- Retention (§164.312(b)) ------------------------------------------

    audit_retention_years: int = Field(
        default=6,
        description=(
            "Minimum retention for audit_log rows. The retention sweep REFUSES "
            "to delete any audit row younger than this (HIPAA §164.312(b) floor "
            "is 6 years). Never set below 6 for a compliant deployment."
        ),
    )
    chat_retention_days: int = Field(
        default=0,
        description=(
            "Optional retention for conversation/message PHI. 0 (default) ⇒ the "
            "sweep never purges chat history; set a positive day count to enable "
            "clinical-conversation purging separately from the audit floor."
        ),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton."""
    return Settings()
