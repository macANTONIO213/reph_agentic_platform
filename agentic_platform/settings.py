import os
import sys
import warnings
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-agentic-platform-change-me")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# OpenAI / Azure AI Foundry
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
AZURE_OPENAI_KEY = os.environ.get("AZURE_OPENAI_KEY", "")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

# AWS Bedrock
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Generic HTTP API adapter
HTTP_API_BEARER_TOKEN = os.environ.get("HTTP_API_BEARER_TOKEN", "")
_debug_default = "True" if "test" in sys.argv else "False"
DEBUG = os.environ.get("DJANGO_DEBUG", _debug_default).lower() in ("true", "1", "yes")

if SECRET_KEY == "dev-agentic-platform-change-me":
    if DEBUG:
        warnings.warn(
            "DJANGO_SECRET_KEY is using the insecure default. "
            "Set the DJANGO_SECRET_KEY environment variable before any non-local deployment.",
            stacklevel=1,
        )
    else:
        # Refuse to start a production (DEBUG=False) process with the default key.
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is unset/insecure while DEBUG=False. "
            "Set a strong DJANGO_SECRET_KEY environment variable before deploying."
        )

_allowed_hosts_env = os.environ.get("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = (
    [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()]
    if _allowed_hosts_env
    else ["127.0.0.1", "localhost", "testserver"]
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "controlplane",
]

MIDDLEWARE = [
    # First in, last out: stamp the correlation id before anything else logs.
    "controlplane.observability.CorrelationIdMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "controlplane.middleware.ApiKeyAuthMiddleware",
    "controlplane.middleware.ApiGlobalRateLimitMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "controlplane.middleware.ApiVersionHeadersMiddleware",
]

ROOT_URLCONF = "agentic_platform.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "agentic_platform.wsgi.application"

import dj_database_url as _dj_db_url

_database_url = os.environ.get("DATABASE_URL", "")
if _database_url:
    DATABASES = {"default": _dj_db_url.config(default=_database_url, conn_max_age=600)}
else:
    # SQLite is a demo/dev convenience only: no pgvector, poor write concurrency.
    # A production (DEBUG=False) boot must point DATABASE_URL at Postgres, or
    # explicitly opt in to SQLite (e.g. single-node evaluation) via env.
    if not DEBUG and os.environ.get("ALLOW_SQLITE_IN_PROD", "").lower() not in ("true", "1", "yes"):
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "DATABASE_URL is unset while DEBUG=False. SQLite is demo-only; "
            "set DATABASE_URL to a PostgreSQL DSN (or ALLOW_SQLITE_IN_PROD=true to override)."
        )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "agentic_platform_demo.sqlite3",
        }
    }

# ── Cache backend ─────────────────────────────────────────────────────────────
# Rate limiting AND the circuit breakers keep counters in the cache; with the
# default per-process LocMemCache those counters are NOT shared across gunicorn
# workers or Celery processes, so a limit of N effectively becomes N×workers and
# a breaker only trips within one process. Point CACHE_URL (or REDIS_URL) at
# Redis in any multi-process deployment so the controls are cluster-wide.
_cache_url = os.environ.get("CACHE_URL") or os.environ.get("REDIS_URL", "")
if _cache_url:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _cache_url,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "agentic-platform-locmem",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ── Logging ───────────────────────────────────────────────────────────────────
# Structured, correlation-id-carrying logs. Set LOG_FORMAT=json in production for
# one-line JSON suitable for a log aggregator; default "plain" stays readable in
# local dev. LOG_LEVEL controls the app logger threshold.
LOG_FORMAT = os.environ.get("LOG_FORMAT", "plain").lower()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation_id": {
            "()": "controlplane.observability.CorrelationIdFilter",
        },
    },
    "formatters": {
        "plain": {
            "format": "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s",
        },
        "json": {
            "()": "controlplane.observability.JsonLogFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["correlation_id"],
            "formatter": "json" if LOG_FORMAT == "json" else "plain",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "controlplane": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# API stabilization controls
API_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("API_RATE_LIMIT_WINDOW_SECONDS", "60"))
API_RATE_LIMIT_REQUESTS_PER_WINDOW = int(os.environ.get("API_RATE_LIMIT_REQUESTS_PER_WINDOW", "120"))
CONNECTOR_CIRCUIT_BREAKER_FAILURE_THRESHOLD = int(
    os.environ.get("CONNECTOR_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")
)
CONNECTOR_CIRCUIT_BREAKER_COOLDOWN_SECONDS = int(
    os.environ.get("CONNECTOR_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "60")
)
# Per-agent orchestrator circuit breaker (production hardening A3).
AGENT_CIRCUIT_BREAKER_FAILURE_THRESHOLD = int(
    os.environ.get("AGENT_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")
)
AGENT_CIRCUIT_BREAKER_COOLDOWN_SECONDS = int(
    os.environ.get("AGENT_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "60")
)
# Dead-letter: max execution attempts before a run/task is parked (A2).
WORKFLOW_RUN_MAX_ATTEMPTS = int(os.environ.get("WORKFLOW_RUN_MAX_ATTEMPTS", "3"))
ASYNC_TASK_MAX_ATTEMPTS = int(os.environ.get("ASYNC_TASK_MAX_ATTEMPTS", "3"))
# Outbound LLM client timeouts (A5) — a hung upstream must not pin a worker.
# Kept below the gunicorn worker --timeout (120s) so a slow upstream returns a
# clean error instead of racing a mid-stream worker SIGKILL (audit PF-02).
LLM_CLIENT_TIMEOUT_SECONDS = float(os.environ.get("LLM_CLIENT_TIMEOUT_SECONDS", "90"))
LLM_CLIENT_MAX_RETRIES = int(os.environ.get("LLM_CLIENT_MAX_RETRIES", "2"))
BROKER_ROUTER_TIMEOUT_SECONDS = float(os.environ.get("BROKER_ROUTER_TIMEOUT_SECONDS", "20"))
# SSRF egress hardening (C3) — DNS re-checking + private-range blocking.
# RESOLVE_DNS is secure-by-default in production: it re-checks resolved IPs and
# blocks loopback/link-local/metadata (e.g. 169.254.169.254) while still allowing
# ordinary internal hostnames. Local dev keeps it off for latency (audit S-02).
# BLOCK_PRIVATE stays opt-in: RFC1918 is allowed by design for internal connectors,
# so blocking it is a per-environment decision, not a safe global default.
_resolve_dns_default = "False" if DEBUG else "True"
NET_GUARD_RESOLVE_DNS = os.environ.get("NET_GUARD_RESOLVE_DNS", _resolve_dns_default).lower() in ("true", "1", "yes")
NET_GUARD_BLOCK_PRIVATE = os.environ.get("NET_GUARD_BLOCK_PRIVATE", "").lower() in ("true", "1", "yes")
# Bearer tokens a Prometheus/Grafana scraper may present to /api/v1/metrics/
# (session-less machine scraping). Empty ⇒ session+admin only.
METRICS_SCRAPE_TOKENS = [
    t.strip() for t in os.environ.get("METRICS_SCRAPE_TOKENS", "").split(",") if t.strip()
]
# Require an active EvalSuite before promoting agents at/above this risk tier
# (0 disables the requirement; the passing-run gate still applies when a suite exists).
# Default 3: tier-3/4 agents cannot reach production untested (GV-1 hardening).
EVAL_GATE_REQUIRE_SUITE_MIN_TIER = int(os.environ.get("EVAL_GATE_REQUIRE_SUITE_MIN_TIER", "3"))
RETENTION_TELEMETRY_DAYS = int(os.environ.get("RETENTION_TELEMETRY_DAYS", "30"))
RETENTION_SPANS_DAYS = int(os.environ.get("RETENTION_SPANS_DAYS", "30"))
RETENTION_SESSIONS_DAYS = int(os.environ.get("RETENTION_SESSIONS_DAYS", "90"))
RETENTION_RUNS_DAYS = int(os.environ.get("RETENTION_RUNS_DAYS", "90"))
PLATFORM_SLO_SUCCESS_RATE_TARGET = float(os.environ.get("PLATFORM_SLO_SUCCESS_RATE_TARGET", "99.0"))
PLATFORM_SLO_P95_LATENCY_MS_TARGET = int(os.environ.get("PLATFORM_SLO_P95_LATENCY_MS_TARGET", "2000"))
PLATFORM_QUEUE_PENDING_WARN_THRESHOLD = int(
    os.environ.get("PLATFORM_QUEUE_PENDING_WARN_THRESHOLD", "100")
)
PLATFORM_QUEUE_STALE_MINUTES = int(os.environ.get("PLATFORM_QUEUE_STALE_MINUTES", "30"))
PLATFORM_ACTIVE_BUDGET_ALERTS_WARN_THRESHOLD = int(
    os.environ.get("PLATFORM_ACTIVE_BUDGET_ALERTS_WARN_THRESHOLD", "10")
)
PLATFORM_ENTERPRISE_MIN_SCORE = float(os.environ.get("PLATFORM_ENTERPRISE_MIN_SCORE", "85"))

# ── Phase 0: Durable execution backend ────────────────────────────────────────
# Which engine runs WorkflowRun and AsyncAgentTask work off the request thread:
#   "db"     — legacy DB-backed queue drained by the `process_workflow_runs`
#              management command (agent tasks run synchronously in-process).
#   "celery" — dispatch to Celery workers over the Redis broker (durable, async).
# Default: "celery" in production when a Redis broker is configured (durable,
# async — SC-1 hardening); otherwise "db" (no broker required, dev/demo).
_has_broker = bool(os.environ.get("CELERY_BROKER_URL") or os.environ.get("REDIS_URL"))
_backend_default = "celery" if (not DEBUG and _has_broker) else "db"
EXECUTION_BACKEND = os.environ.get("EXECUTION_BACKEND", _backend_default).lower()

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_TASK_DEFAULT_QUEUE = os.environ.get("CELERY_TASK_DEFAULT_QUEUE", "agentic")
# Reliability: ack after completion and prefetch one, so a crashed worker's task
# is redelivered rather than lost, and long agent runs don't starve siblings.
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = int(os.environ.get("CELERY_TASK_TIME_LIMIT", "1800"))       # hard 30m
CELERY_TASK_SOFT_TIME_LIMIT = int(os.environ.get("CELERY_TASK_SOFT_TIME_LIMIT", "1500"))  # soft 25m
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_RESULT_EXPIRES = int(os.environ.get("CELERY_RESULT_EXPIRES", "86400"))        # 1 day
# Run tasks inline (no broker needed) during tests, or when explicitly requested.
CELERY_TASK_ALWAYS_EAGER = (
    os.environ.get("CELERY_TASK_ALWAYS_EAGER", "").lower() in ("true", "1", "yes")
    or "test" in sys.argv
)
CELERY_TASK_EAGER_PROPAGATES = True

# ── Scheduled maintenance (OE-1) ──────────────────────────────────────────────
# Celery beat drains the operational batch jobs that previously required external
# cron. Runs only where a beat process is started (Procfile/render.yaml).
CELERY_BEAT_SCHEDULE = {
    "recover-stale-runs": {"task": "controlplane.maintenance", "schedule": 300.0, "args": ("recover_stale",)},
    "compute-budgets": {"task": "controlplane.maintenance", "schedule": 3600.0, "args": ("compute_budgets",)},
    "compute-baselines": {"task": "controlplane.maintenance", "schedule": 86400.0, "args": ("compute_baselines",)},
    "enforce-retention": {"task": "controlplane.maintenance", "schedule": 86400.0, "args": ("enforce_retention",)},
    "purge-expired-memory": {"task": "controlplane.maintenance", "schedule": 3600.0, "args": ("purge_memory",)},
    "export-spans": {"task": "controlplane.maintenance", "schedule": 60.0, "args": ("export_spans",)},
}

# ── Email / notifications (UX-2) ──────────────────────────────────────────────
# SMTP is configured entirely from env; unset EMAIL_HOST ⇒ console backend (dev).
if os.environ.get("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ["EMAIL_HOST"]
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True").lower() in ("true", "1", "yes")
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "agentic-platform@localhost")

# ── Operational alert delivery (OE-3) ─────────────────────────────────────────
# Budget/quality/dead-letter alerts fan out to these sinks (both optional).
ALERT_EMAIL_RECIPIENTS = [
    e.strip() for e in os.environ.get("ALERT_EMAIL_RECIPIENTS", "").split(",") if e.strip()
]
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")  # e.g. Teams/Slack incoming webhook

# ── OTLP span export (SC-2) ───────────────────────────────────────────────────
# When set, the export_spans job POSTs unexported OtelSpan rows as OTLP/HTTP JSON
# to <endpoint>/v1/traces and marks them exported.
OTEL_EXPORTER_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OTEL_EXPORTER_OTLP_HEADERS = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")  # "k=v,k2=v2"
OTEL_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "agentic-platform")

# ── Connector config encryption at rest (GV-2) ────────────────────────────────
# Fernet key (urlsafe base64, 32 bytes) used to encrypt DataConnector.config
# values. Unset ⇒ configs stored as plaintext JSON (dev/demo only).
CONNECTOR_CONFIG_ENCRYPTION_KEY = os.environ.get("CONNECTOR_CONFIG_ENCRYPTION_KEY", "")

# Max independent workflow tasks the orchestrator runs concurrently per wave
# (1 = sequential — the pre-hardening behaviour).
ORCHESTRATOR_MAX_PARALLEL = int(os.environ.get("ORCHESTRATOR_MAX_PARALLEL", "4"))

# ── Phase 1: A2A server (outbound discoverability) ────────────────────────────
# The external A2A surface (/a2a/) is OFF by default — enable deliberately.
A2A_SERVER_ENABLED = os.environ.get("A2A_SERVER_ENABLED", "").lower() in ("true", "1", "yes")
# Comma-separated bearer tokens accepted from external A2A consumers (per-consumer).
# Session-authenticated internal users are always allowed when the surface is on.
A2A_ACCESS_TOKENS = [
    t.strip() for t in os.environ.get("A2A_ACCESS_TOKENS", "").split(",") if t.strip()
]
# Public base URL advertised in agent cards (falls back to the request host).
A2A_PUBLIC_BASE_URL = os.environ.get("A2A_PUBLIC_BASE_URL", "")

# ── Phase 4: Broker routing ───────────────────────────────────────────────────
# "deterministic" (default; keyword/domain/capability scoring) or "llm" (live,
# quality-aware LLM ranker over the deterministic shortlist — falls back to
# deterministic when no ANTHROPIC_API_KEY or on any LLM error).
BROKER_ROUTER_MODE = os.environ.get("BROKER_ROUTER_MODE", "deterministic").lower()
BROKER_ROUTER_MODEL = os.environ.get("BROKER_ROUTER_MODEL", "claude-sonnet-4-6")

# ── Phase 1 stretch: MCP server (expose our governed tools) ────────────────────
# Exposes an allowlisted set of builtin tools as an MCP server at /a2a/mcp/.
# OFF by default; when on, external callers present a bearer token.
MCP_SERVER_ENABLED = os.environ.get("MCP_SERVER_ENABLED", "").lower() in ("true", "1", "yes")
MCP_SERVER_TOKENS = [
    t.strip() for t in os.environ.get("MCP_SERVER_TOKENS", "").split(",") if t.strip()
]
_mcp_exposed = os.environ.get("MCP_SERVER_EXPOSED_TOOLS", "")
# Only read-only, agent-agnostic builtins should be exposed. Default: registry search.
MCP_SERVER_EXPOSED_TOOLS = (
    [t.strip() for t in _mcp_exposed.split(",") if t.strip()]
    if _mcp_exposed else ["registry_search"]
)

# CSRF trusted origins — must be defined before the Render hostname block appends to it
CSRF_TRUSTED_ORIGINS = [
    "http://127.0.0.1:8765",
    "http://localhost:8765",
]

# Render: auto-add the public hostname to ALLOWED_HOSTS and CSRF origins
_render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
if _render_hostname:
    ALLOWED_HOSTS.append(_render_hostname)
    CSRF_TRUSTED_ORIGINS.append(f"https://{_render_hostname}")

# ── Production security hardening ────────────────────────────────────────────
# Applied only when DEBUG is off, so local development is unaffected.
if not DEBUG:
    # Honour the X-Forwarded-Proto header set by the Render/upstream TLS proxy.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "True").lower() in ("true", "1", "yes")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    # HSTS — 1 year, includes subdomains; opt out via env if needed.
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    X_FRAME_OPTIONS = "DENY"
