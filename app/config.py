"""Centralized, environment-driven configuration.

All settings are read from the process environment / `.env` exactly once and
shared as a cached singleton. Nothing else in the codebase should read
os.environ directly.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database -----------------------------------------------------------
    postgres_host: str = "db"
    postgres_port: int = 5432
    postgres_db: str = "company_brain"
    postgres_user: str = "brain"
    postgres_password: str = "brain"

    # --- Embeddings ---------------------------------------------------------
    # Fixed at table-create time (pgvector columns are dimensioned). 1536 maps
    # to text-embedding-3-small; 3072 to text-embedding-3-large.
    embedding_dim: int = 1536

    # --- LLM providers -------------------------------------------------------
    # "azure" (default), "anthropic" (Claude extraction; embeddings delegate to
    # Azure if configured, else deterministic offline vectors), or "stub".
    llm_provider: str = "azure"
    anthropic_api_key: str | None = None  # SDK also resolves ANTHROPIC_API_KEY itself
    anthropic_model: str = "claude-opus-4-8"
    anthropic_max_tokens: int = 16000
    azure_openai_endpoint: str | None = None
    azure_openai_api_key: str | None = None
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_embedding_deployment: str | None = None
    azure_openai_chat_deployment: str | None = None

    # --- GitHub connector (Step 4) ------------------------------------------
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    github_repo: str | None = None  # default "owner/name" if not passed explicitly

    # --- Slack connector (Step 4) -------------------------------------------
    slack_bot_token: str | None = None
    slack_api_url: str = "https://slack.com/api"
    slack_channel_id: str | None = None  # default channel if not passed explicitly

    # --- HTTP resilience (shared by connectors) -----------------------------
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 5
    http_backoff_base_seconds: float = 1.0
    http_backoff_max_seconds: float = 60.0

    # --- Ingestion / extraction limits --------------------------------------
    ingest_max_items: int = 200  # safety cap on items pulled per run
    extraction_max_chars: int = 12000  # chars of a document sent to the LLM

    # --- Security / RBAC (Track 1) ------------------------------------------
    # Default OFF -> Principal.anonymous() is superuser; behavior is unchanged.
    rbac_enabled: bool = False
    # If set, POST /ingest requires this value in the X-API-Key header.
    ingest_api_key: str | None = None
    oidc_jwks_url: str | None = None
    oidc_audience: str | None = None
    oidc_issuer: str | None = None

    # --- Performance (Track 3) ----------------------------------------------
    embedding_batch_size: int = 256
    traverse_statement_timeout_ms: int = 2000
    traverse_max_depth: int = 4
    cache_backend: str = "memory"  # "memory" | "redis"
    cache_ttl_seconds: int = 60
    redis_url: str = "redis://redis:6379/0"

    sync_sources: str | None = None
    sync_interval_seconds: int = 3600

    # --- Proposals / self-maintenance ----------------------------------------
    # Proposals at/above this confidence apply without human review (still
    # audited + rollback-able). None (the default) disables autonomy entirely:
    # every proposal waits in the review queue.
    proposal_auto_apply_threshold: float | None = None
    # Dedup agent candidate bands — deliberately looser than the resolver's
    # insert-time auto-merge thresholds (0.55 / 0.15): the gray band goes to a
    # human instead of being merged silently.
    dedup_name_threshold: float = 0.45
    dedup_vec_threshold: float = 0.25
    dedup_scan_limit: int = 100  # max candidate pairs per run
    # LLM second opinion on each NEW candidate pair (same/different/unsure).
    # Safe default: the stub provider answers "unsure", which changes nothing.
    dedup_llm_judge: bool = True
    # A "different" verdict at/above this confidence keeps the pair out of the
    # queue entirely; below it, the pair is filed with its confidence lowered.
    dedup_llm_skip_threshold: float = 0.8
    # Staleness agent: flag nodes whose newest evidence is older than this.
    stale_after_days: int = 180
    stale_scan_limit: int = 200
    # Bootstrap: how many recent documents to sample for missing-type discovery.
    bootstrap_sample_size: int = 25

    # --- Connectors (Track 4) -----------------------------------------------
    local_root: str | None = None
    local_scan_concurrency: int = 16
    notion_token: str | None = None
    notion_database_id: str | None = None
    notion_api_url: str = "https://api.notion.com/v1"
    notion_version: str = "2022-06-28"
    teams_tenant_id: str | None = None
    teams_client_id: str | None = None
    teams_client_secret: str | None = None
    teams_team_id: str | None = None
    graph_api_url: str = "https://graph.microsoft.com/v1.0"
    zoom_account_id: str | None = None
    zoom_client_id: str | None = None
    zoom_client_secret: str | None = None
    zoom_api_url: str = "https://api.zoom.us/v2"
    gmeet_credentials_json: str | None = None
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_project: str | None = None

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL using the psycopg3 driver."""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
