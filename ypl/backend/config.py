import json
import os
import secrets
import warnings
from functools import cached_property
from typing import Literal, Self

import pydantic
import sqlalchemy
from pydantic import (
    computed_field,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TRANSFORMERS_NO_FRAMEWORK_WARNING", "1")

DEFAULT_UNSAFE_PASSWORD = "changethis"

EnvironmentType = Literal["production", "staging", "test", "local", "selfhosted"]

# Environments that run without GCP (no Secret Manager, no Cloud SQL proxy,
# no yupp-llms DB topology). "local" = dev laptops + CI; "test" = pytest;
# "selfhosted" = the AHS monolith running on a single VM with .env secrets
# and local Postgres. Extend when adding another GCP-free deployment mode.
GCP_FREE_ENVIRONMENTS: frozenset[EnvironmentType] = frozenset(("local", "test", "selfhosted"))


def is_gcp_free_environment(env: str) -> bool:
    """True when ``env`` runs without GCP (no Secret Manager / Cloud SQL proxy)."""
    return env in GCP_FREE_ENVIRONMENTS


DbName = Literal["yuppdb", "agentdb"]


class PostgresConnection(pydantic.BaseModel):
    """A single Postgres connection target parsed from a JSON env var."""

    user: str = "test"
    password: str = "test"
    host: str = "localhost:5432"
    host_non_pooling: str = "localhost:5432"
    database: str = "postgres"
    cloud_sql_proxy_socket: str = ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("DOTENV_PATH", ".env"), env_ignore_empty=True, extra="ignore"
    )
    API_PREFIX: str = "/api"
    SECRET_KEY: str = secrets.token_urlsafe(32)
    X_API_KEY: str = ""
    # This holds the alternative API key, used for rotation.
    # X_API_KEY and X_API_KEY_SECONDARY both will have the same value in a steady-state, but during
    # rotations, X_API_KEY and X_API_KEY_SECONDARY will alternate between the old and new values.
    X_API_KEY_SECONDARY: str = ""
    X_ADMIN_API_KEY: str = ""
    X_AUTOMATION_KEY: str = ""
    OPENAPI_USERNAME: str = ""
    OPENAPI_PASSWORD: str = ""

    AWS_REGION: str = ""
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    OPENAI_API_KEY: str = ""
    ALIBABA_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    ANYSCALE_API_KEY: str = ""
    AZURE_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""
    FIREWORKS_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""
    HUGGINGFACE_API_KEY: str = ""
    MISTRAL_API_KEY: str = ""
    NVIDIA_API_KEY: str = ""
    OPENROUTER_API_KEY: str = ""
    PARALLEL_API_KEY: str = ""
    EXA_API_KEY: str = ""
    PERPLEXITY_API_KEY: str = ""
    TOGETHERAI_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    CEREBRAS_API_KEY: str = ""
    SAMBANOVA_API_KEY: str = ""
    SEARCHAPI_API_KEY: str = ""
    # BSL AI Private HF Datasets/Models token
    BSL_AI_HF_AUTH_TOKEN: str = ""

    DOMAIN: str = "localhost"
    ENVIRONMENT: EnvironmentType = "local"
    # Which database get_async_session() uses by default. Set to "agentdb" for AHS/SAG services.
    DEFAULT_DB: DbName = "yuppdb"
    PROJECT_NAME: str = ""
    PRIMARY_CLOUD_PROVIDER: str = "google_cloud_run"

    # Database connections are configured via JSON env vars. Each contains:
    # {"user", "password", "host", "host_non_pooling", "database", "cloud_sql_proxy_socket"(optional)}
    # yuppdb = the shared Yupp database, agentdb = the agent-specific database.
    POSTGRES_CONNECTION_YUPPDB: str = ""
    POSTGRES_CONNECTION_YUPPDB_REPLICA: str = ""
    POSTGRES_CONNECTION_AGENTDB: str = ""
    POSTGRES_CONNECTION_AGENTDB_REPLICA: str = ""
    # DDL / Alembic connection. Higher-privilege role (e.g. schema_manager)
    # that can CREATE / ALTER / DROP. If empty, falls back to AGENTDB.
    # Set in the monolith setup by the install.sh-generated .pg-creds file,
    # and in CI via GitHub Actions secrets.
    POSTGRES_CONNECTION_AGENTDB_ADMIN: str = ""

    CACHE_DIR: str = ".cache"
    USE_GOOGLE_CLOUD_LOGGING: bool = True
    DISABLE_WRITE_GOOGLE_CLOUD_METRICS: bool = False

    ENABLE_SQL_QUERY_LOGGING_ON_NON_PROD: bool = True

    ROUTING_LOCAL_DEBUG: bool = os.getenv("ROUTING_LOCAL_DEBUG", "false").lower() == "true"
    ROUTING_LOCAL_DEBUG_DETAILED: bool = os.getenv("ROUTING_LOCAL_DEBUG_DETAILED", "false").lower() == "true"

    # Enables some print() logging in some places to see logs locally even when gcp logging is enabled.
    STREAMING_LOCAL_DEBUG: bool = os.getenv("STREAMING_LOCAL_DEBUG", "false").lower() == "true"
    STREAMING_LOCAL_DEBUG_RAW: bool = os.getenv("STREAMING_LOCAL_DEBUG_RAW", "false").lower() == "true"
    STREAMING_LOCAL_DEBUG_INPUT: bool = os.getenv("STREAMING_LOCAL_DEBUG_INPUT", "false").lower() == "true"
    STREAMING_LOCAL_DEBUG_OUTPUT: bool = os.getenv("STREAMING_LOCAL_DEBUG_OUTPUT", "false").lower() == "true"
    STREAMING_LOCAL_DEBUG_USAGE_METADATA: bool = False

    ROUTING_GOOD_MODELS_RANK_THRESHOLD: int = 3  # the rank cutoff for what is considered a "good" model for routing
    ROUTING_GOOD_MODELS_ALWAYS: bool = False  # if true, a good model will always be included in the selected models
    ROUTING_DO_LOGGING: bool = True  # if true, logging will be done

    ROUTING_ERROR_FILTER_SOFT_THRESHOLD: float | None = None
    ROUTING_ERROR_FILTER_HARD_THRESHOLD: float | None = None
    ROUTING_ERROR_FILTER_SOFT_REJECT_PROB: float | None = None

    # The timeout for the prompt categorizer in routing
    ROUTING_TIMEOUT_SECS: float = 1.25

    ROUTING_REPUTABLE_PROVIDERS: list[str] = ["openai", "google", "anthropic", "azure", "microsoft", "meta"]
    OPENAI_API_KEY_ROUTING: str = ""

    # The GCP storage path to the prompt categorizer model.
    CATEGORIZER_MODEL_PATH: str = "gs://yupp-models/online-classifier-base.zip"

    # The cloud run project ID and region
    GCP_PROJECT_NUMBER: str = ""  # a numeric name for the project (in string form)
    GCP_PROJECT_ID: str = ""  # a string name, mutually exchangeable with GCP_PROJECT_NUMBER in identifying resources
    GCP_REGION: str = "us-east4"
    GCP_REGION_GEMINI_2: str = "us-central1"
    GCP_REGION_CLAUDE: str = "global"

    # Gmail service account credentials
    GMAIL_SERVICE_ACCOUNT_EMAIL: str = ""
    GMAIL_SERVICE_ACCOUNT_KEY_JSON: str = ""
    STS_GMAIL_OAUTH_TOKEN: str = ""

    CDP_API_KEY_ID: str = ""
    CDP_API_KEY_SECRET: str = ""
    CDP_RPC_ENDPOINT: str = ""
    CDP_WALLET_SECRET: str = ""
    CDP_WALLET_NAME: str = ""
    ETHERSCAN_API_KEY: str = ""
    ETHERSCAN_API_URL: str = ""

    CRYPTO_EXCHANGE_PRICE_API_URL_COINBASE: str = os.getenv(
        "CRYPTO_EXCHANGE_PRICE_API_URL_COINBASE", "https://api.coinbase.com/v2/prices/{}-{}/spot"
    )
    CRYPTO_EXCHANGE_PRICE_API_URL_COINGECKO: str = os.getenv(
        "CRYPTO_EXCHANGE_PRICE_API_URL_COINGECKO",
        "https://api.coingecko.com/api/v3/simple/price?ids={}&vs_currencies=usd",
    )

    SLACK_WEBHOOK_ABUSE: str = os.getenv("SLACK_WEBHOOK_ABUSE", "")
    SLACK_WEBHOOK_ABUSE_EXPERIMENTAL: str = os.getenv("SLACK_WEBHOOK_ABUSE_EXPERIMENTAL", "")
    SLACK_WEBHOOK_CASHOUT: str = os.getenv("SLACK_WEBHOOK_CASHOUT", "")
    SLACK_WEBHOOK_GUEST_MANAGEMENT: str = os.getenv("SLACK_WEBHOOK_GUEST_MANAGEMENT", "")
    SLACK_WEBHOOK_ONBOARDING: str = os.getenv("SLACK_WEBHOOK_ONBOARDING", "")
    SLACK_WEBHOOK_URL_REWARDS_AND_PAYMENTS: str = ""
    SLACK_WEBHOOK_PRIORITY_SUPPORT_TICKETS: str = os.getenv("SLACK_WEBHOOK_PRIORITY_SUPPORT_TICKETS", "")
    SLACK_WEBHOOK_SUPPORT_TICKETS: str = os.getenv("SLACK_WEBHOOK_SUPPORT_TICKETS", "")
    SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")
    SLACK_GROWTH_ALERTS_CHANNEL_ID: str = "C08GNLF1H5F"  # Defaults to #alert-test-only
    SLACK_SUPPORT_SQUAD_CHANNEL_ID: str = "C08GNLF1H5F"  # Defaults to #alert-test-only
    SLACK_INTERESTING_PROMPTS_FEED_CHANNEL_ID: str = "C0826FU9JMB"  # Defaults to test channel
    SLACK_INTERESTING_PROMPTS_PRIVATE_TURNS_CHANNEL_ID: str = SLACK_INTERESTING_PROMPTS_FEED_CHANNEL_ID

    # Slack Agent Gateway settings
    # Registry of agents lives in the ``slack_agents`` DB table. Per-agent
    # secrets are sourced via ``fetch_agent_secret``: env vars named
    # ``SLACK_AGENT_GATEWAY_<NAME>_BOT_TOKEN`` / ``_SIGNING_SECRET`` in
    # GCP-free environments, GCP Secret Manager elsewhere.

    # Agcouch MCP — the default/first-class remote MCP this repo ships.
    # Hosted in-process when running ``mono_server``; can also run as a
    # standalone service in the future (just point the URL elsewhere). The
    # AHS connects to it by name and reads its tools during session startup.
    #
    # TODO(phase-9): support additional 3rd-party MCP servers via a
    # DB-backed registry + Streamlit admin UI. For now, only this one
    # first-class server is wired up.
    AGCOUCH_MCP_SERVER_NAME: str = "agcouch-mcp-server"
    AGCOUCH_MCP_SERVER_URL: str = "http://localhost:8090/mcp/agcouch"
    AGCOUCH_MCP_TOKEN: str = ""
    AGCOUCH_MCP_ENABLED: bool = True

    # Agent Harness Service (AHS) base URL (SAG calls AHS here)
    AGENT_HARNESS_SERVICE_BASE_URL: str = ""
    # API key shared between SAG and AHS for mutual authentication
    AGENT_HARNESS_SERVICE_API_KEY: str = ""
    # Agent orchestrator settings (CODE_MODALITIES_MULTI_FILE_OUTPUT modalities)
    AGENT_ORCHESTRATOR_URL: str = ""
    AGENT_ORCHESTRATOR_API_KEY: str = ""
    # SAG base URL that AHS calls back to for posting replies to Slack
    GATEWAY_BASE_URL: str = ""
    # Per-agent settings (add more as needed)
    SLACK_AGENT_GATEWAY_GILADOVSKI_APP_ID: str = ""
    SLACK_AGENT_GATEWAY_GILADOVSKI_BOT_TOKEN: str = ""
    SLACK_AGENT_GATEWAY_GILADOVSKI_SIGNING_SECRET: str = ""
    SLACK_AGENT_GATEWAY_GILADOVSKI_DISPLAY_NAME: str = "Giladovski"
    SLACK_AGENT_GATEWAY_LINGFENGOVICH_APP_ID: str = ""
    SLACK_AGENT_GATEWAY_LINGFENGOVICH_BOT_TOKEN: str = ""
    SLACK_AGENT_GATEWAY_LINGFENGOVICH_SIGNING_SECRET: str = ""
    SLACK_AGENT_GATEWAY_LINGFENGOVICH_DISPLAY_NAME: str = "Lingfengovich"
    SLACK_AGENT_GATEWAY_TIANFUCIUS_APP_ID: str = ""
    SLACK_AGENT_GATEWAY_TIANFUCIUS_BOT_TOKEN: str = ""
    SLACK_AGENT_GATEWAY_TIANFUCIUS_SIGNING_SECRET: str = ""
    SLACK_AGENT_GATEWAY_TIANFUCIUS_DISPLAY_NAME: str = "Tianfucius"
    SLACK_AGENT_GATEWAY_AXANDWICH_APP_ID: str = ""
    SLACK_AGENT_GATEWAY_AXANDWICH_BOT_TOKEN: str = ""
    SLACK_AGENT_GATEWAY_AXANDWICH_SIGNING_SECRET: str = ""
    SLACK_AGENT_GATEWAY_AXANDWICH_DISPLAY_NAME: str = "Axandwich"
    SLACK_AGENT_GATEWAY_YUPPCLAW_APP_ID: str = ""
    SLACK_AGENT_GATEWAY_YUPPCLAW_BOT_TOKEN: str = ""
    SLACK_AGENT_GATEWAY_YUPPCLAW_SIGNING_SECRET: str = ""
    SLACK_AGENT_GATEWAY_YUPPCLAW_DISPLAY_NAME: str = "YuppClaw"
    # Enable mock agent for testing (bypasses real agent service)
    SLACK_AGENT_GATEWAY_USE_MOCK_AGENT: bool = False

    # Bot Father — automated Slack bot creation
    SLACK_BOT_FATHER_BOT_TOKEN: str = ""
    SLACK_BOT_FATHER_SIGNING_SECRET: str = ""
    SLACK_BOT_FATHER_APP_CONFIG_REFRESH_TOKEN: str = ""  # Access tokens obtained on-demand via refresh
    # TODO(oss): currently unused at runtime. When BotFather approval gating is
    # re-enabled, move this default out of source — deployers should set the
    # Slack channel ID in their own .env.
    SLACK_BOT_FATHER_APPROVAL_CHANNEL: str = "C0ALTCHNZ99"

    # Slack Agent Gateway encryption key (base64-encoded 32-byte key for Fernet)
    SLACK_AGENT_GW_ENCRYPTION_KEY: str = ""

    # External-MCP per-user grant encryption key (Fernet).  Protects the
    # OAuth access / refresh tokens and per-user API keys stored in
    # ``mcp_user_grants``, plus the OAuth client_secret + M2M_SHARED token
    # on ``mcp_server_secrets``.  Generate with the same recipe as
    # SLACK_AGENT_GW_ENCRYPTION_KEY (deploy/mac/install.sh does this).
    MCP_USER_GRANT_ENCRYPTION_KEY: str = ""

    # Per-deployment display name shown across self-hosted UIs.  Drives the
    # Streamlit hub title ("<name> Hub") and the artifact viewer header
    # ("<name> Artifacts").  Override per box in .env, e.g. "VoltCouch" on a
    # laptop or "AgenticCouch" on the GCP VM.
    DEPLOYMENT_NAME: str = "AgenticCouch"

    # Streamlit hub title shown on the browser tab and the login/landing/
    # access-denied screens.  Leave empty to derive "<DEPLOYMENT_NAME> Hub";
    # set explicitly only when you need a title that isn't "<name> Hub".
    STREAMLIT_HUB_TITLE: str = ""

    # Sentry
    SENTRY_AUTH_TOKEN: str = ""

    DEFAULT_QT_TIMEOUT_SECS: float = 1.5
    RETAKE_QT_TIMEOUT_SECS: float = 5.0

    DEFAULT_REVIEW_TIMEOUT_SECS: float = 120.0
    HALLUCINATION_REVIEW_TIMEOUT_SECS: float = 600.0

    # Timeout for junk prompt quality assessment as part of risk evaluation
    JUNK_PROMPT_QUALITY_TIMEOUT_SECS: float = 15.0

    PARSE_PDF_LOCALLY_FOR_QUICKTAKE: bool = True
    PARSE_PDF_LOCALLY_FOR_REVIEW: bool = True
    MAX_TEXT_TO_EXTRACT_FROM_PDF: int = 16000

    # Whether to create LLM-generated text as a promptbox placeholder. Currently not picked up by the frontend.
    USE_PROMPTBOX_PLACEHOLDER: bool = False

    # Whether to compute and store embeddings for user messages.
    EMBED_USER_MESSAGES: bool = True
    # Whether to compute and store embeddings for assistant messages.
    EMBED_ASSISTANT_MESSAGES: bool = False

    # TODO(gilad): split to staging and production.
    INTERNAL_EMBEDDING_ENDPOINT: str = os.getenv("INTERNAL_EMBEDDING_ENDPOINT", "")

    # Leaderboard tier: "" or "latest" - used for Redis cache key separation.
    # When running dual leaderboard backends, this ensures each tier has its own cache namespace.
    LEADERBOARD_TIER: str = ""

    # Leaderboard instance role: "" for stable instances, "cron" for cron instance.
    # Unlike LEADERBOARD_TIER, this does not affect caching - all roles share the same cache.
    # Used to differentiate instance behavior (e.g., enable/disable certain features on cron).
    LEADERBOARD_INSTANCE_ROLE: str = ""

    # GCS bucket names:
    # see: https://console.cloud.google.com/storage/browser?project=yupp-llms
    ATTACHMENT_BUCKET: str = "gs://yupp-attachments/staging"
    ATTACHMENT_BUCKET_GEN_IMAGES: str = "gs://yupp-generated-images/staging"
    ATTACHMENT_BUCKET_PUBLIC: str = "gs://yupp-public-attachments/staging"
    ATTACHMENT_BUCKET_PUBLIC_GEN_IMAGES: str = "gs://yupp-public-generated-images/staging"

    # GCS bucket names for app feedback:
    APP_FEEDBACK_BUCKET: str = "gs://app-feedback-attachments/staging"

    # Whether to start celery workers along with the main app.
    # TODO(arawind): remove this once we separate the celery workers into a separate instance.
    CELERY_SPAWN_WORKERS: bool = True

    # The URL of the Redis instance to use for celery workers.
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"

    TASKIQ_ADMIN_URL: str = ""
    TASKIQ_ADMIN_API_TOKEN: str = ""

    # The URL of the Redis instance to use for the application.
    # For process-local state (e.g. rate limiting) use get_redis_client, which uses this URL.
    # For state shared with an external frontend service (e.g. stop-streaming signals) use
    # get_upstash_redis_client_for_stop_streaming_check, which reads UPSTASH_REDIS_* env vars.
    REDIS_URL: str = "redis://localhost:6379/1"

    # Whether to process events via Kafka.
    ENABLE_KAFKA_EVENT_PROCESSING: bool = False

    # Kafka related settings
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    KAFKA_SCHEMA_REGISTRY_URL: str = "http://localhost:8081"

    # Secrets in GCP Secret Manager, loaded as environment variables:
    # see: https://console.cloud.google.com/secrets/create?project=yupp-llms
    # If the secret needs to be accessed on local development, add it to the `.env` file in 1password.
    # If it can't be added there (as it's JSON or like a file), only then use the pattern used for AXIS_UPI_CONFIG.
    # If you're adding a new secret, make sure to update the
    # `.github/workflows/scripts/generate_gcloud_deploy_secrets_string.py` script.
    AMPLITUDE_API_KEY: str = ""
    AMPLITUDE_API_SECRET: str = ""
    AMPLITUDE_EXPERIMENTS_DEPLOYMENT_API_KEY: str = ""
    EMBED_X_API_KEY: str = ""
    # Bearer token for the X / Twitter v2 API (used by the ``search_twitter``
    # MCP tool). Obtain from https://developer.x.com/. Empty disables the tool.
    X_API_BEARER_TOKEN: str = ""
    GUEST_MANAGEMENT_SLACK_WEBHOOK_URL: str = ""
    IPINFO_API_KEY: str = ""
    IPQUALITYSCORE_SECRET: str = os.getenv("IPQUALITYSCORE_SECRET", "")
    PARTNER_PAYMENTS_API_URL: str = ""
    PAYPAL_WEBHOOK_ID: str = ""
    RESEND_API_KEY: str = ""
    RESEND_BATCH_EMAIL_URL: str = "https://api.resend.com/emails/batch"
    UPSTASH_REDIS_URL: str = ""
    UPSTASH_REDIS_TOKEN: str = ""
    VALIDATE_DESTINATION_IDENTIFIER_SECRET_KEY: str = ""
    CASHOUT_VERIFICATION_SECRET_KEY: str = ""
    VPNAPI_API_KEY: str = ""
    PRIME_INTELLECT_API_KEY: str = ""

    # For logging API requests and responses for debugging and development.
    API_LOGGING_ENABLED: bool = False  # This logs request and responses for all the API requests, for debugging.
    API_METRICS_ENABLED: bool = True  # This logs API calls metrics to GCP
    API_LOGGING_REGEX: str = ".*"  # If provided, only log request when the URL matches the regex.

    # To support separting leaderboard and admin APIs as a separate server
    BACKEND_OPERATING_MODE: Literal["main", "leaderboard", "admin"] = "main"

    # Setting this to above 0 enables periodic logging of active async tasks including stack traces.
    TASK_STATUS_LOGGING_INTERVAL_SECS: int = 0
    # Setting this to above 0 enables periodic logging of resource information, useful for tracking resource leaks.
    RESOURCE_LOGGING_INTERVAL_SECS: int = 0

    # Overridden in the deploy scripts.
    ENABLE_CLOUDSQL_PROXY: bool = False

    # When True, uses the cloud-sql-python-connector library for async DB connections
    # instead of the Cloud SQL Proxy sidecar. Connects via port 3307 through the
    # managed connection pooler.
    ENABLE_CLOUD_SQL_CONNECTOR: bool = False

    # Setting this to above 0 enables periodic logging of active async tasks including stack traces.
    SUBPROCESS_TRACE_COLLECT_TIME_SEC: int = 0

    PYINSTRUMENT_PROFILING_ENABLED: bool = False

    PROFILE_APP_INIT: bool = False
    APP_INIT_PROFILE_BUCKET: str = "yupp-attachments"
    APP_INIT_PROFILE_FOLDER_PATH: str = "app_init_profiles"

    LINEAR_API_KEY: str = os.getenv("LINEAR_API_KEY", "")
    READ_COMMIT_HISTORY_GITHUB_TOKEN: str = os.getenv("READ_COMMIT_HISTORY_GITHUB_TOKEN", "")

    GCS_BUCKET_NAME: str = os.getenv("GCS_BUCKET_NAME", "yupp-data")

    # Blob store — pluggable storage backend used by the artifact system
    # (``ypl/agent_harness_service/artifact_store.py``). ``local`` writes
    # under the configured directory; ``gcs`` uses ``GCS_BUCKET_NAME``.
    #
    # Logical paths passed to the store are scoped by UUID shard, e.g.
    # ``ab/<uuid>/<uuid>.md``. The store-level prefix (``artifacts/``) is
    # omitted — ``BLOB_STORE_LOCAL_DIR`` already ends in ``artifacts/`` and
    # operators who share a GCS bucket with other data can scope via a
    # dedicated bucket name.
    BLOB_STORE_ENGINE: str = "local"
    BLOB_STORE_LOCAL_DIR: str = "/data/ahs/artifacts"
    BLOB_STORE_LOCAL_BASE_URL: str = ""

    # Public base URL of the artifact viewer (apps/artifact-viewer). Used by
    # ``create_artifact`` to populate ``agent_artifacts.url`` so callers /
    # Slack messages / agents get a shareable, human-readable link instead
    # of the raw AHS REST path. No trailing slash.
    # If empty, ``agent_artifacts.url`` falls back to the raw
    # ``/ahs/artifacts/{uuid}`` path.
    VIEWER_BASE_URL: str = "https://artifacts.agcouch.com"

    # Slack channel ID to receive a notification every time an artifact is
    # created or updated. Posted as OpsBot in production / staging; skipped
    # entirely on local + test environments to avoid spamming the channel
    # during dev. Set to the empty string to disable globally.
    # Default: ``#agent-artifacts`` (C0B0M10JR8E).
    ARTIFACT_NOTIFICATIONS_CHANNEL: str = "C0B0M10JR8E"

    # Data Takeout settings
    DATA_TAKEOUT_BUCKET: str = "yupp-data-takeouts-staging"  # GCS bucket for takeout ZIPs
    DATA_TAKEOUT_OUTPUT_DIR: str = "/tmp/data-takeouts"  # Local/FUSE path for writing ZIPs
    DATA_TAKEOUT_EXPIRY_DAYS: int = 7  # How long takeouts remain downloadable
    DATA_TAKEOUT_RETRY_DAYS: int = 4  # How long to retry stuck processing before giving up

    # The base64 encoded 256 bit key to use while hashing a deleted user's attribute.
    # We will use this key to hash the attribute value, and store the hash in the database.
    DELETED_USER_ATTRIBUTE_HMAC_KEY: str = ""

    # Sardine API configuration
    SARDINE_BASE_URL: str = ""
    SARDINE_CLIENT_ID: str = ""
    SARDINE_CLIENT_SECRET: str = ""

    # Twilio settings
    TWILIO_SID: str = ""
    TWILIO_SECRET: str = ""
    TWILIO_SERVICE_SID: str = ""

    # The base64 encoded 256 bit key to use while encrypting user verification values.
    # Created by running ypl.backend.user.verifications.cryptography_utils.generate_encryption_key().
    USER_VERIFICATION_ENCRYPTION_KEY: str = ""
    # The base64 encoded 256 bit key to use while calculating the HMAC of user verification values.
    # Created by running ypl.backend.user.verifications.cryptography_utils.generate_hmac_key().
    USER_VERIFICATION_HMAC_KEY: str = ""

    # Solana settings
    SOLANA_EXPLORER_BASE_URL: str = "https://explorer.solana.com"
    SOLANA_EXPLORER_CLUSTER: str = "devnet"

    # Dynamic app settings local test model
    DISABLE_REDIS_FOR_DYNAMIC_APP_SETTINGS: bool = False

    # For local development only
    USE_LOCAL_MEMORY_DATA_SOURCE: bool = False

    # MaxMind GeoIP settings
    MAXMIND_API_KEY: str = ""
    MAXMIND_ACCOUNT_ID: str = ""

    # Allowed email domains for MCP server token creation. Access control on
    # AHS itself goes through the ``users`` table (who's allowed to access
    # anything else is gated by whether there's a row for them); the MCP
    # OAuth flow needs its own list because tokens can be minted for users
    # who aren't yet in the DB. Empty = no gate.
    ALLOWED_MCP_EMAIL_DOMAINS: list[str] = []

    # System "robot" account emails used by internal operations. Required when
    # running mono_server setup/manage (creates the row) or github webhooks
    # (looks it up to attribute webhook-triggered sessions).
    SYSTEM_USER_EMAIL: str = ""
    SYSTEM_GITHUB_WEBHOOK_USER_EMAIL: str = ""

    # Synthetic email domain used to mint identities for programmatic agent
    # users (e.g. ``agent-<uuid>@<AGENT_USER_EMAIL_DOMAIN>``). Kept distinct
    # from human-user domains so agent identities stay outside human flows.
    AGENT_USER_EMAIL_DOMAIN: str = ""

    # Public base URL for the MCP server, used in emails and docs.
    MCP_SERVER_BASE_URL: str = "http://localhost:8080"

    # MCP OAuth settings for Google OAuth provider
    MCP_OAUTH_GOOGLE_CLIENT_ID: str = ""
    MCP_OAUTH_GOOGLE_CLIENT_SECRET: str = ""
    # JWT signing key for OAuth token management
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
    MCP_OAUTH_JWT_SIGNING_KEY: str = ""
    # Fernet encryption key for OAuth token storage
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    MCP_OAUTH_STORAGE_ENCRYPTION_KEY: str = ""

    # MCP Server authentication mode: "DEV_TOKEN" or "OAUTH"
    # DEV_TOKEN: Uses Bearer tokens created via CLI (yupp_dev_* format)
    # OAUTH: Uses Google OAuth via FastMCP's GoogleProvider
    MCP_SERVER_MODE: str = "DEV_TOKEN"

    # AHS CORS configuration. The baseline loopback origins for local dev are
    # always added by server.py; these two settings let deployers add their
    # production frontends. Format: JSON list for explicit origins, a regex
    # string for wildcard matching (e.g. ``https://.*\\.example\\.com``).
    AHS_CORS_ALLOW_ORIGINS: list[str] = []
    AHS_CORS_ALLOW_ORIGIN_REGEX: str = ""

    # Agent Harness Service (AHS) GitHub token encryption key
    # Fernet encryption key for storing user GitHub tokens in Redis
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    AHS_GITHUB_TOKEN_ENCRYPTION_KEY: str = ""

    # GitHub App client secret for refreshing user access tokens
    # Required when "User-to-server token expiration" is enabled on the GitHub App
    AHS_GITHUB_APP_CLIENT_SECRET: str = ""

    # Shared secret for verifying incoming GitHub App webhook signatures (X-Hub-Signature-256).
    # Set this to the webhook secret configured on the GitHub App.
    # Generate with: python -c "import secrets; print(secrets.token_hex(32))"
    AHS_GITHUB_WEBHOOK_SECRET: str = ""

    def _parse_address_list(self, value: str) -> list[str]:
        """Parse a string containing addresses in JSON array or comma-separated format."""
        if not value:
            return []
        if value.strip().startswith("["):
            try:
                parsed = json.loads(value)
                return [str(addr) for addr in parsed]
            except Exception:
                return []
        return [addr.strip() for addr in value.split(",") if addr.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def TRUSTED_ETH_SMART_CONTRACT_ADDRESSES(self) -> list[str]:
        env_var = os.getenv("TRUSTED_ETH_SMART_CONTRACT_ADDRESSES", "")
        return self._parse_address_list(env_var)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def server_host(self) -> str:
        # Use HTTPS for anything other than local development
        if self.ENVIRONMENT == "local":
            return f"http://{self.DOMAIN}"
        return f"https://{self.DOMAIN}"

    def _parse_pg_connection(self, raw: str) -> PostgresConnection:
        """Parse a JSON string into a PostgresConnection, falling back to defaults."""
        if not raw:
            return PostgresConnection()
        return PostgresConnection.model_validate_json(raw)

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def yuppdb(self) -> PostgresConnection:
        return self._parse_pg_connection(self.POSTGRES_CONNECTION_YUPPDB)

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def yuppdb_replica(self) -> PostgresConnection:
        return self._parse_pg_connection(self.POSTGRES_CONNECTION_YUPPDB_REPLICA)

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def agentdb(self) -> PostgresConnection:
        return self._parse_pg_connection(self.POSTGRES_CONNECTION_AGENTDB)

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def agentdb_replica(self) -> PostgresConnection:
        return self._parse_pg_connection(self.POSTGRES_CONNECTION_AGENTDB_REPLICA)

    @computed_field  # type: ignore[prop-decorator]
    @cached_property
    def agentdb_admin(self) -> PostgresConnection:
        """Admin / DDL connection (e.g. schema_manager). Falls back to agentdb
        if POSTGRES_CONNECTION_AGENTDB_ADMIN is not set, so existing deployments
        that only have one role keep working."""
        raw = self.POSTGRES_CONNECTION_AGENTDB_ADMIN or self.POSTGRES_CONNECTION_AGENTDB
        return self._parse_pg_connection(raw)

    def agentdb_admin_url(self, *, async_mode: bool = False) -> str:
        """Build a SQLAlchemy URL for the agentdb admin connection — used by
        Alembic to run DDL. If no admin creds are configured, returns the same
        URL as the regular agentdb connection."""
        return self._build_db_url(self.agentdb_admin, async_mode=async_mode)

    def get_pg_connection(self, db: DbName = "yuppdb", *, replica: bool = False) -> PostgresConnection:
        """Return the PostgresConnection for the given database and replica flag."""
        if db == "yuppdb":
            return self.yuppdb_replica if replica else self.yuppdb
        return self.agentdb_replica if replica else self.agentdb

    def _use_proxy_socket(self, conn: PostgresConnection, async_mode: bool) -> bool:
        """Whether to connect via the Cloud SQL Auth Proxy unix socket."""
        if is_gcp_free_environment(self.ENVIRONMENT) or not self.ENABLE_CLOUDSQL_PROXY:
            return False
        if not conn.cloud_sql_proxy_socket:
            return False
        if async_mode and self.ENABLE_CLOUD_SQL_CONNECTOR:
            return False
        return True

    def _build_db_url(self, conn: PostgresConnection, async_mode: bool) -> str:
        scheme = "postgresql" + ("+asyncpg" if async_mode else "")
        if self._use_proxy_socket(conn, async_mode):
            return sqlalchemy.engine.url.URL.create(
                drivername=scheme,
                username=conn.user,
                password=conn.password,
                database=conn.database,
                query={"host": f"{conn.cloud_sql_proxy_socket}" + ("/.s.PGSQL.5432" if async_mode else "")},
            ).render_as_string(hide_password=False)
        host, _, port_str = conn.host.rpartition(":")
        if not host:
            host, port_str = port_str, ""
        return sqlalchemy.engine.url.URL.create(
            drivername=scheme,
            username=conn.user,
            password=conn.password,
            host=host,
            port=int(port_str) if port_str else 5432,
            database=conn.database,
        ).render_as_string(hide_password=False)

    def db_url_for(self, db: DbName = "yuppdb", *, replica: bool = False, async_mode: bool = False) -> str:
        """Build a SQLAlchemy database URL for the given database."""
        return self._build_db_url(self.get_pg_connection(db, replica=replica), async_mode=async_mode)

    def cloud_sql_instance_for(self, db: DbName = "yuppdb", *, replica: bool = False) -> str:
        """Instance connection name (e.g. 'yupp-llms:us-east4:sarai-chat-prod')."""
        conn = self.get_pg_connection(db, replica=replica)
        return conn.cloud_sql_proxy_socket.removeprefix("/cloudsql/")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_ssl_mode(self) -> str:
        if is_gcp_free_environment(self.ENVIRONMENT) or self.ENABLE_CLOUDSQL_PROXY:
            return "disable"
        return "require"

    # ---- Convenience aliases (yuppdb, the default) for backward compat ----
    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_url(self) -> str:
        return self.db_url_for("yuppdb", async_mode=False)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_url_async(self) -> str:
        return self.db_url_for("yuppdb", async_mode=True)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_url_read_replica(self) -> str:
        return self.db_url_for("yuppdb", replica=True, async_mode=False)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def db_url_async_read_replica(self) -> str:
        return self.db_url_for("yuppdb", replica=True, async_mode=True)

    def _check_default_secret(self, var_name: str, value: str | None) -> None:
        if value == DEFAULT_UNSAFE_PASSWORD:
            message = (
                f'The value of {var_name} is "{DEFAULT_UNSAFE_PASSWORD}", '
                "for security, please change it, at least for deployments."
            )
            if self.ENVIRONMENT == "local":
                warnings.warn(message, stacklevel=1)
            else:
                raise ValueError(message)

    @model_validator(mode="after")
    def _enforce_non_default_secrets(self) -> Self:
        self._check_default_secret("OPENAI_API_KEY", self.SECRET_KEY)
        self._check_default_secret("AWS_ACCESS_KEY_ID", self.AWS_ACCESS_KEY_ID)
        self._check_default_secret("AWS_SECRET_ACCESS_KEY", self.AWS_SECRET_ACCESS_KEY)
        self._check_default_secret("ALIBABA_API_KEY", self.ALIBABA_API_KEY)
        self._check_default_secret("ANTHROPIC_API_KEY", self.ANTHROPIC_API_KEY)
        self._check_default_secret("ANYSCALE_API_KEY", self.ANYSCALE_API_KEY)
        self._check_default_secret("AZURE_API_KEY", self.AZURE_API_KEY)
        self._check_default_secret("DEEPSEEK_API_KEY", self.DEEPSEEK_API_KEY)
        self._check_default_secret("FIREWORKS_API_KEY", self.FIREWORKS_API_KEY)
        self._check_default_secret("GOOGLE_API_KEY", self.GOOGLE_API_KEY)
        self._check_default_secret("HUGGINGFACE_API_KEY", self.HUGGINGFACE_API_KEY)
        self._check_default_secret("MISTRAL_API_KEY", self.MISTRAL_API_KEY)
        self._check_default_secret("NVIDIA_API_KEY", self.NVIDIA_API_KEY)
        self._check_default_secret("OPENROUTER_API_KEY", self.OPENROUTER_API_KEY)
        self._check_default_secret("PERPLEXITY_API_KEY", self.PERPLEXITY_API_KEY)
        self._check_default_secret("TOGETHERAI_API_KEY", self.TOGETHERAI_API_KEY)

        return self

    @model_validator(mode="after")
    def validate_db_config(self) -> Self:
        if self.ENVIRONMENT in ["production", "staging"]:
            # Only validate during actual runtime, not during tests
            # Skip when running under pytest, or in local e2e test mode (both signals required)
            if os.getenv("PYTEST_CURRENT_TEST") is None and not (
                os.getenv("IN_AHS_E2E_TEST") and self.ENABLE_CLOUDSQL_PROXY
            ):
                # Skip validation when yuppdb is not configured (e.g. SAG only uses agentdb)
                if self.POSTGRES_CONNECTION_YUPPDB:
                    conn = self.yuppdb
                    test_values = ["test", "postgres", "localhost:5432"]
                    if (
                        conn.user in test_values
                        or conn.password == "test"
                        or conn.host in test_values
                        or conn.database in test_values
                    ):
                        raise ValueError(
                            f"Database configuration using test values in {self.ENVIRONMENT} environment. "
                            "Please set proper database credentials."
                        )
        return self


settings = Settings()
