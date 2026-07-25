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
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "agentic_platform_demo.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"
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
# Default "db" keeps existing behaviour and requires no broker.
EXECUTION_BACKEND = os.environ.get("EXECUTION_BACKEND", "db").lower()

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
