# Enterprise Application Audit — REPH Agentic Platform (Control Plane)

> **Mode:** Enterprise Architecture Review Board — findings-only audit (read-only, no code changed).
> **Prepared:** 26 Jul 2026.
> **Scope:** Django 5.2 control plane for governed AI agents · ~25,800 LOC Python · 38 models · ~66 API endpoints · 555 tests · single contributor · deployed on Render.
> **Companion docs:** `PRODUCTION_HARDENING_PLAN.md`, `RUNBOOK.md`, `AGENTIC_PLATFORM_ROADMAP_DECK.md`.
>
> This document is the durable record of the audit for **later implementation**. Every finding cites `file:line`.
> Severity reflects exploitability/impact in a production (`DEBUG=False`) deployment with interop surfaces enabled.

---

## How to use this document

- **Findings** tables are the evidence. **Action Log** tables are the work items.
- The **Consolidated Action Register** (end) is the single prioritized backlog — start there for implementation.
- Priorities: **P0** (fix before any internet-facing deploy) · **P1** (production-blocking) · **P2** (hardening) · **P3** (polish).
- Cross-reference: many items align with the team's own `PRODUCTION_HARDENING_PLAN.md` gates.

---

## Phase 1 — Application Inventory

| Category | Finding |
|---|---|
| **Technology stack** | Python 3.12, Django 5.2 (LTS range `>=5.2,<6.0`), Gunicorn, WhiteNoise, Celery 5.4+/Redis |
| **Frontend architecture** | Server-rendered Django templates (no framework); vanilla JS (`platform.js` 838 LOC, `manage.js`, `register.js`) calling `/api/v1/*` JSON; Chart.js 4.4.3 via jsDelivr CDN; hand-written CSS (`platform.css` 2,174 LOC) |
| **Backend architecture** | Single Django app `controlplane`; layered `api/` (HTTP) → `services/` (14+ domains) → `models.py` (persistence); custom SSE agent-runtime with pluggable LLM adapters |
| **Database** | PostgreSQL in prod (via `DATABASE_URL`); **SQLite fallback** when unset (`settings.py:99-108`); pgvector declared but not wired (`embeddings.py:162-168`) |
| **External integrations** | Anthropic, OpenAI/Azure OpenAI, AWS Bedrock, generic HTTP API (Copilot); MCP client + server (JSON-RPC 2.0); A2A agent-to-agent protocol; REST + SQL data connectors; Agentforce/Copilot scanners |
| **Authentication** | Django session auth for UI + `/api/v1` (`@login_required`); bearer tokens for A2A/MCP/metrics surfaces (constant-time compare, `interop_auth.py:22-35`) |
| **Infrastructure** | Render (web + worker + Redis + Postgres, `render.yaml`); Docker multi-stage build; docker-compose for local parity |
| **Deployment model** | Container/PaaS on Render; stateless web + Celery worker; migrations run in build command (`render.yaml:6`, `build.sh:7`) |
| **CI/CD tooling** | GitHub Actions `release-gate.yml`: full test suite, `check --deploy`, maturity gates, `pip-audit --strict`, `bandit`, CycloneDX SBOM |
| **Monitoring tooling** | Prometheus `/api/v1/metrics/` (live ORM computation); JSON structured logs w/ correlation IDs; homegrown `OtelSpan` DB table (no real OTel export) |
| **Third-party dependencies** | `requirements.txt` compatible-floor ranges + `constraints.txt` pins (Django==5.2.15, celery==5.6.3, etc.); **`boto3` + `psycopg2-binary` left unpinned** |

**Note:** `agentic_platform_demo.sqlite3` is **not** committed — `.gitignore:2` (`*.sqlite3`) and `.dockerignore` exclude it, and `.env` is untracked. No secrets are in version control.

---

## Phase 2 — Architecture Review

### Findings

| ID | Severity | Component | Observation | Business Impact |
|----|----------|-----------|-------------|-----------------|
| A-01 | High | `api/views.py` | God-module: **2,518 LOC**, ~66 routes, 7 serializers, rate-limit + tenant logic in one file | Merge contention, slow onboarding, blocks service extraction |
| A-02 | High | services layer | Circular-dependency cluster masked by function-level imports: `orchestrator → agent_runtime → agent_tasks → orchestrator` (`orchestrator.py:418`, `agent_tasks.py:27/55`, `workflow_queue.py:22/75`); 2nd cluster `factory ↔ package_ingestor ↔ tools.bindings` | Runtime-deferred import failures, fragile refactors, hidden coupling |
| A-03 | High | project structure | Single Django app holds all 14+ domains, 38 models, 66 endpoints (`settings.py:53-61`) | No enforced module boundaries; blocks independent ownership/deploy |
| A-04 | Medium | views | Business logic + ORM inline in controllers (`api/views.py:1869-1943`; 104 ORM calls in `api/views.py`, 50 in `views.py`) | View logic untestable/unreusable by tasks/commands; duplicated tenant rules |
| A-05 | Medium | `models.py` | God-model file: **1,804 LOC**, 38 models across all domains | Every domain change touches one file; blocks app-split |
| A-06 | Medium | error handling | 82 broad `except Exception` (excl. tests); anti-pattern `except (BusinessUnit.DoesNotExist, Exception): pass` (`api/views.py:1913`); 22 broad `json.loads` guards catching `Exception` not `JSONDecodeError` | Hidden defects, harder incident diagnosis, real bugs masked as "bad input" 400s |
| A-07 | Medium | adapters | Adapter "port" couples to ORM: base class imports `Agent/AgentRun/AgentToolCall` and writes `AgentToolCall.objects.create` (`adapters/base.py:14,57-71`) | Adapter layer not portable/testable outside Django |
| A-08 | Medium | dependencies | `boto3`, `psycopg2-binary` unpinned (`constraints.txt:21-22`) — prod DB driver + AWS SDK | Non-reproducible prod builds; `pip-audit` blind to unpinned versions |
| A-09 | Low | api | 7 hand-rolled `_*_dict` serializers, no schema/OpenAPI source of truth | API contract drift between models and responses |
| A-10 | Low | cross-cutting | Duplicated tenant-scoping snippet + SSE-handling logic across dozens of sites | One contract change requires many coordinated edits |

**Strengths:** Real intentional layering; zero bare `except:`; zero TODO/FIXME/HACK markers; custom domain exceptions used well; clean data-driven adapter selection; centralized SSRF egress boundary; 555 tests.

### Action Log

| Priority | Action Item |
|---|---|
| P1 | Split `api/views.py` and `models.py` by bounded context into separate Django apps (unblocks A-01/A-03/A-05 and the circular cluster) |
| P1 | Break the `orchestrator/runtime/tasks` import cycle by extracting shared contracts into a dependency-free module |
| P2 | Introduce a thin service/repository layer; move tenant enforcement out of views into managers |
| P2 | Pin `boto3` + `psycopg2-binary` in `constraints.txt` |
| P3 | Replace broad `except Exception`/`json.loads` guards with specific exception types; make the adapter port ORM-free |

---

## Phase 3 — Security Review

### Findings

| ID | Severity | Risk Area | Observation |
|----|----------|-----------|-------------|
| S-01 | **Critical** | SSRF | All outbound clients use `urllib.request.urlopen`, which **follows 3xx redirects**, but `validate_destination()` runs only on the initial URL. A destination returning `302 Location: http://169.254.169.254/...` or an internal host bypasses the guard. Affects `rest_connector.py:92`, `mcp_client.py:179`, `a2a_client.py:41`, `http_api.py:94`, `scanners/agentforce.py:85`, `scanners/copilot.py:83` |
| S-02 | High | SSRF policy | `NET_GUARD_RESOLVE_DNS` and `NET_GUARD_BLOCK_PRIVATE` both **default False** (`settings.py:225-227`) and are absent from the `if not DEBUG` hardening block — so hostname→internal SSRF and literal RFC1918 targets are allowed out of the box |
| S-03 | High | Authorization / LLM tools | Agent-less tool context defaults `agent_tier` to **4 (highest)**, so the `spec.risk_tier > agent_tier` gate can never trip (`tools/registry.py:108,164`). The MCP server dispatches external calls with exactly this context (`mcp_server_views.py:137`) — adding any higher-risk tool to the exposed set silently grants Tier-4 execution to external callers |
| S-04 | High | SQL injection | LLM-generated SQL passed to `conn.execute(text(sql))` guarded only by a **keyword denylist** + `SELECT`-prefix check (`sql_connector.py:30-103`). Bypassable via CTEs/`UNION`/`information_schema`/`pg_sleep`; driven by a prompt-injectable agent |
| S-05 | Medium | Secrets management | `DataConnector.config` is plaintext `JSONField`; docstring claims "encrypted-at-rest in production" (`models.py:629`) but **no encryption exists**. Runtime reads literal `auth_header` / DB URL with embedded creds (`rest_connector.py:82-83`, `sql_connector.py:51-56`) |
| S-06 | Medium | Secrets in logs | `AuditLog` stores `url_preview` = first 200 chars of outbound URL (`rest_connector.py:135-153`); credentials/tokens in base URL or query string land in the audit table |
| S-07 | Medium | Authorization / IDOR | A2A `tasks/get` scopes by agent only, not caller (`a2a_views.py:218-223`). All external consumers share one `a2a:external` identity, so one consumer can poll another's task output by UUID |
| S-08 | Medium | Prompt injection | Guardrails are a static English regex denylist (`guardrails.py:57-125`), evadable via encoding/translation/homoglyphs; performs no tool-invocation authorization; output scan non-blocking |
| S-09 | Medium | SSRF (TOCTOU) | Even with DNS resolution on, `getaddrinfo` validate then `urlopen` re-resolve is a rebinding TOCTOU window (`net_guard.py:64-73`) |
| S-10 | Low | Rate limiting | Per-endpoint limiter uses non-atomic get-then-set (`api/views.py:79-85`) vs the atomic `add/incr` in middleware; undercounts under concurrency |
| S-11 | Low | Authorization | `registry_detail` GET not visibility/tenant-scoped (`api/views.py:1213-1224`) — any authenticated user reads any entry's `card_json` by id |
| S-12 | Low | Dependencies | Lower-bound `>=` ranges, no hash-pinned lockfile — supply-chain drift risk |

**Verified sound:** `@login_required` coverage complete; constant-time bearer compares; CSRF re-checked for session callers on `csrf_exempt` interop views; tenant IDOR checks consistent (`get_object_or_404` + `_can_access_*`); mass-assignment guarded by editable allowlists; Django auto-escaping + client-side `esc()`; full production security-header block (HSTS, secure cookies, nosniff, `X-Frame-Options: DENY`); refuses to boot prod with default `SECRET_KEY`.

### Action Log

| Priority | Action Item | Risk Reduction |
|---|---|---|
| P0 | Disable redirect-following (or re-validate every hop) across all `urllib` clients | Closes active SSRF-to-metadata/internal path |
| P1 | Enable `NET_GUARD_RESOLVE_DNS` + `NET_GUARD_BLOCK_PRIVATE` in the `if not DEBUG` block | Removes default-permissive SSRF posture |
| P1 | Default agent-less tool context to the **lowest** risk tier | Prevents privilege escalation via MCP exposure |
| P1 | Replace denylist SQL with parameterization / real allowlist parser | Eliminates injection & exfiltration path |
| P2 | Implement field-level encryption for `DataConnector.config` or move to secret store; fix misleading docstring; redact `url_preview` | Protects connector credentials |
| P2 | Scope A2A `tasks/get` to submitting caller | Closes cross-consumer IDOR |

---

## Phase 4 — DevOps Review

### Findings

| ID | Severity | Area | Observation |
|----|----------|------|-------------|
| DO-01 | High | Tracing | **Aspirational, not real.** No `opentelemetry` library imported anywhere; `OtelSpan` is a homegrown DB table whose docstring references an `export_spans` command that **does not exist** (`models.py:875-921`). RUNBOOK lists "OTel spans" as a golden signal that cannot be emitted |
| DO-02 | High | Alerting | **Aspirational.** RUNBOOK.md:107 specifies pager alerts, but there are **no alert evaluators, Alertmanager rules, or webhooks** in code — only threshold settings with nothing wired to them |
| DO-03 | High | Dependency pinning | Prod Postgres driver `psycopg2-binary` + `boto3` explicitly unpinned (`constraints.txt:21-22`) |
| DO-04 | Medium | CI maturity | Strong gates (tests, `check --deploy`, pip-audit, bandit, SBOM) but **missing**: linter/formatter, type check, coverage threshold, secret scanning, container image build+scan, IaC scan. No deploy job validates the actual artifact |
| DO-05 | Medium | CI gating | 3 jobs run in parallel with no `needs:` — SBOM/security not gated on tests passing |
| DO-06 | Medium | Metrics scalability | `/api/v1/metrics/` computes all metrics live from ORM on each scrape over **unindexed** `AgentRun` columns (`metrics.py:113-158`) — table scans per Prometheus poll, no caching |
| DO-07 | Medium | Image hygiene | Base image `python:3.12-slim` tag-pinned, **not digest-pinned**; apt packages unpinned (`Dockerfile:5,8,13`) |
| DO-08 | Medium | Migrations in deploy | `migrate` runs in build command (`render.yaml:6`, `build.sh:7`) — concurrent-instance risk, no advisory lock / dedicated release phase |
| DO-09 | Medium | Env/cache parity | SQLite fallback + pgvector "gracefully skipped on SQLite" = **correctness drift** dev vs prod; default LocMemCache makes rate-limit/breaker counters per-process if `REDIS_URL` forgotten |
| DO-10 | Medium | IaC coverage | `render.yaml` + compose codify services, but **no Terraform/Pulumi**; backups, monitoring, alerting, DNS rely on Render defaults; key env vars `sync:false` (manual) |
| DO-11 | Low | Config | `TIME_ZONE = "Asia/Manila"` for a Reed Elsevier platform affects retention/report boundaries (`settings.py:188`) |

### Action Log

| Priority | Action Item |
|---|---|
| P1 | Implement real OTel export (or stop advertising it); wire alert evaluators to the existing thresholds |
| P1 | Pin `psycopg2-binary` + `boto3`; digest-pin the base image |
| P2 | Add coverage gate, secret scanning, container/IaC scan; add `needs:` ordering; move `migrate` to a release phase with advisory lock |
| P2 | Cache metrics computation or precompute; add `AgentRun` indexes (see Phase 5) |

---

## Phase 5 — Data Review

### Findings

| ID | Severity | Area | Observation |
|----|----------|------|-------------|
| DA-01 | High | PII protection | Prompts/responses stored **plaintext**: `AgentRun.input_text/output_text` (`models.py:269-270`), `ConversationSession.messages` (:379), `AsyncAgentTask.input/output` (:1718-1719), `KnowledgeDocument.raw_text` (:582). No field encryption, redaction, PII classification, or per-subject erasure |
| DA-02 | High | Retention enforcement | `enforce_retention.py` exists and writes AuditLog, but is **scheduled nowhere** — no cron, no Celery beat (0 grep hits), no `render.yaml` job. GDPR/retention windows not actually enforced in prod despite RUNBOOK claiming "nightly" |
| DA-03 | High | Backup/recovery | **Not codified** — no backup provision in `render.yaml`; PITR/WAL/RPO/RTO all documented in RUNBOOK but none automated or verified in-repo; no backup-verification job |
| DA-04 | High | Missing indexes | `AgentRun` (system-of-record) has only `ordering`, **no index** on `agent`/`status`/`started_at` — the exact columns every metrics/cost/latency query filters. Same for `TelemetryEvent`, `AgentToolCall`, `AgentFeedback`. Newer models do add indexes → inconsistent |
| DA-05 | Medium | FK cascade | `AgentRun.agent` is `CASCADE` (`models.py:266`): deleting an Agent destroys all run history, tool calls, feedback, spans. Contrast `TelemetryEvent`/`OtelSpan` correctly `SET_NULL` |
| DA-06 | Medium | Tenant scoping | `Agent` carries both legacy text `business_unit` and FK `org_unit` (`models.py:131,143`) with reconciliation fallback — dual source of truth. Nullable `SET_NULL` tenant FKs silently convert scoped rows to "platform-wide" on BU deletion |
| DA-07 | Medium | Audit immutability | `AuditLog` blocks update/delete in `save`/`delete` overrides + admin (`models.py:1797-1804`) — but **Python-layer only**; raw SQL bypasses it. No DB triggers, append-only grants, or hash-chaining |
| DA-08 | Medium | Schema design | Pervasive JSONField blobs (AgentBlueprint ~10, AgentFactoryPackage 13) — unvalidated, unqueryable, unindexable |
| DA-09 | Low | Migrations hygiene | Healthy: 20 migrations, one reversible data migration, `0020` (DLQ/idempotency) purely additive & forward-compatible |
| DA-10 | Low | Compliance evidence | `export_compliance_evidence` emits aggregate SOC2/ISO snapshot (no PII) — appropriate, but unscheduled (same gap as DA-02) |

### Action Log

| Priority | Action Item |
|---|---|
| P1 | Schedule `enforce_retention` + `export_compliance_evidence` (Render cron / Celery beat) |
| P1 | Field-encrypt or redact stored prompts/responses; add per-subject erasure |
| P1 | Add composite index `(agent, status, started_at)` on `AgentRun`; codify + verify backups |
| P2 | Reconsider `CASCADE` on run history; consolidate dual tenant fields; add DB-level audit immutability |

---

## Phase 6 — Performance Review

### Findings

| ID | Severity | Area | Observation |
|----|----------|------|-------------|
| PF-01 | High | Worker starvation | Interactive agent runs execute the LLM **synchronously inside the HTTP request** (`views.py:195-197` → `agent_runtime.py:137`). With **4 sync gunicorn workers** (`render.yaml:7`), only 4 concurrent conversations before every worker is pinned for the full LLM duration. Single biggest scale ceiling |
| PF-02 | High | Timeout alignment | `LLM_CLIENT_TIMEOUT_SECONDS=120` and gunicorn `--timeout 120` are **identical** — a slow-but-valid response races a worker SIGKILL mid-stream instead of a clean error |
| PF-03 | High | Vector search | `embeddings._python_search` loads **every** embedding row and computes unvectorised cosine in pure Python per row (`embeddings.py:170-198`); `_pg_search` is a stub that falls back to Python **even on Postgres** — pgvector ANN never used. O(N·1536) per search |
| PF-04 | Medium | N+1 | `org_tree` re-filters inside prefetch loop (`.filter()` bypasses cache, `api/views.py:367-369`); `eval_suites` breaks prefetch with `.order_by().first()` + per-suite `.count()` (`:642-646`) |
| PF-05 | Medium | Unbounded querysets | `dashboard` does `Agent.objects.all().prefetch_related("runs")` — eagerly loads every run, unused (`views.py:25-27`); `agents_list`/`manage_panel` have no pagination |
| PF-06 | Medium | Redundant aggregation | No query/result caching — every dashboard load re-runs full aggregations; `agent_catalog_telemetry` fires 4 full-table passes per call incl. a discarded query (`aggregations.py:253-303`) |
| PF-07 | Low | Multi-process counters | Rate-limiter/breaker correctness depends on Redis; silent loss of enforcement if `CACHE_URL` forgotten (documented, wired in render.yaml) |
| PF-08 | Low | DB-queue polling | Legacy `process_workflow_runs` is a fixed-interval serial DB poller (dev-only; prod uses Celery, which is well-configured) |

### Action Log

| Priority | Action Item |
|---|---|
| P1 | Move interactive runs to async worker class or the existing Celery/AsyncAgentTask path |
| P1 | Lower LLM client timeout below gunicorn timeout (e.g. 90s vs 120s) |
| P1 | Wire real pgvector ANN; add indexes (Phase 5 DA-04) |
| P2 | Fix N+1s; paginate list endpoints; cache dashboard aggregations; add a load/soak test |

---

## Phase 7 — UX Review

### Findings

| ID | Severity | Area | Observation |
|----|----------|------|-------------|
| UX-01 | Medium | Navigation consistency | **No shared base template** (`TEMPLATES.DIRS=[]`, no `{% extends %}`); each of 3 pages is standalone; `login.html` inlines its own stylesheet with a different visual system; duplicated `<head>`/topbar across pages |
| UX-02 | Medium | Maintainability | Primary app is one **2,102-line `dashboard.html`** with ~838 LOC inline JS; all tabs' markup ships on every load; no component reuse |
| UX-03 | Medium | Supply chain / offline | Chart.js loaded from jsDelivr CDN with **no SRI hash, no local fallback** (`dashboard.html:9`) — enterprise CSP or offline network breaks all charts |
| UX-04 | Low-Med | Accessibility | Good baseline (29 aria/role usages, sr-only label, polite toast, proper form labels). Gaps: status conveyed by **color-only** CSS classes (`tone-green/orange/blue`); streaming chat log has **no `aria-live`/`aria-busy`**, so screen readers aren't notified as tokens stream |
| UX-05 | Low | Responsiveness | Viewport meta present but only 5 `@media` queries for a 2,100-line dashboard with fixed multi-column grid + wide tables — cramped/horizontal-scroll on tablet/mobile |
| UX-06 | Low | Onboarding | Dashboard hardcodes a demo agent slug as the "live agent" (`views.py:30`); a fresh tenant with no agents renders blank hero fields — no empty-fleet call-to-action or guided first-run |

**Strength:** Empty/loading/error states are well covered across async tables; SSE token streaming masks LLM latency with live feedback.

### Action Log

| Priority | Action Item |
|---|---|
| P2 | Introduce a shared base template; self-host Chart.js (already have WhiteNoise) or add SRI |
| P2 | Add text/icon redundancy to color-coded status; add `aria-live`/`aria-busy` to the streaming log |
| P3 | Improve tablet/mobile responsive CSS; add empty-fleet onboarding flow |

---

## Phase 8 — Supportability Review

### Findings

| ID | Severity | Area | Observation |
|----|----------|------|-------------|
| SP-01 | High | Runbook vs reality | RUNBOOK.md is **excellent on paper** (topology, triage, rollback, backup, DR, dashboard spec) but several procedures reference capabilities not implemented: alerting (DO-02), OTel export (DO-01), scheduled retention/evidence (DA-02) — operators would follow steps that cannot execute |
| SP-02 | High | Bus factor | **Single contributor** across all 32 commits (`git shortlog`); no README; no CODEOWNERS; knowledge concentrated in one person — critical L3 support risk |
| SP-03 | Medium | DR verification | DR drill is documented (quarterly restore, RPO≤5m/RTO≤1hr) but **never executed/evidenced in-repo**; backups not codified (DA-03) |
| SP-04 | Medium | Support model | No L1/L2/L3 tiering, escalation matrix, or on-call rotation defined; incident procedures exist but no ownership assignment |
| SP-05 | Low | Documentation depth | Rich strategy/roadmap/spec docs and a RUNBOOK, but **no README, no architecture diagram, no API reference** (compounded by no OpenAPI, A-09) |

### Action Log

| Priority | Action Item |
|---|---|
| P1 | Reconcile RUNBOOK with implemented reality — implement the missing controls or mark them roadmap |
| P1 | Add a second maintainer / CODEOWNERS; write a README with setup + architecture overview |
| P2 | Execute and evidence a DR restore drill; define L1/L2/L3 + escalation |

---

## Phase 9 — Governance Review

### Findings

| ID | Severity | Area | Observation |
|----|----------|------|-------------|
| GV-01 | Medium | Secure-by-Design | Strong intent (SSRF guard, governed runtime, 3-gate promotion, risk tiering, immutable audit) undermined by **secure controls defaulting off** (S-02 SSRF flags, S-03 tier default) — "secure by default" not fully honored |
| GV-02 | Medium | Auditability | `AuditLog` is app-layer immutable and broadly written, but coverage is **voluntary at call sites** — not centrally enforced for all admin/API mutations; no tamper-evidence (DA-07) |
| GV-03 | Medium | Change management | Good CI gate + expand/contract migration discipline + feature-flag defaults; but no coverage gate, single reviewer (SP-02), and self-attested maturity gates (`platform_maturity_report --fail-on-unready`) risk grading themselves |
| GV-04 | Medium | Risk management | Risk-tiering + eval-gate framework exists (`EVAL_GATE_REQUIRE_SUITE_MIN_TIER`) but **defaults to 0 (off)**; promotion controls present but opt-in |
| GV-05 | Low | Platform ownership | Governance choke-point (`GovernanceService`) is architecturally sound; ownership/operational model undocumented (SP-04) |
| GV-06 | Low | Standards alignment | Compliance-evidence export targets SOC2/ISO controls — good direction; not yet scheduled or externally validated |

### Action Log

| Priority | Action Item |
|---|---|
| P1 | Flip secure defaults on (SSRF, lowest-tier, eval-gate) so posture is safe out of the box |
| P2 | Centrally enforce audit-write on all mutations; add tamper-evidence |
| P2 | Add coverage gate + independent review; formalize ownership/operating model |

---

## Executive Summary

**Enterprise Readiness Score: 68 / 100**

An unusually **well-engineered platform for its stage** — genuine layering, a clean LLM adapter/interop design, a centralized SSRF boundary, dense test coverage (555 tests), mature CI gating (pip-audit, bandit, SBOM, deploy checks), and a professional RUNBOOK. It is held back from production readiness by a consistent theme: **secure/operational controls that are built but default-off or documented-but-not-wired**, one active SSRF vulnerability, unprotected PII at rest, and structural monolith debt.

### Category Scores

| Category | Score | Rationale |
|---|---:|---|
| Architecture | 70 | Clean layering & adapters; god-modules, circular cluster, single-app monolith |
| Security | 60 | Strong fundamentals; 1 Critical (SSRF redirect) + 3 High undercut posture |
| DevOps | 65 | Excellent CI; tracing/alerting aspirational, unpinned prod driver |
| Data | 58 | Good reliability model; PII plaintext + retention unscheduled + backups uncodified |
| Performance | 62 | Sound Celery config; sync LLM in 4 workers + brute-force vector search cap scale |
| UX | 72 | Solid states & a11y baseline; no base template, color-only status, CDN dependency |
| Operations | 66 | Great runbook; contradicts reality in places, single maintainer |
| Governance | 70 | Real governance spine; secure defaults off, self-attested gates |

### Top Critical Risks

- **S-01 — SSRF guard bypassed by HTTP redirects** on every outbound client (`urllib` follows 3xx; guard checks only the first URL). Reachable via external MCP/A2A/REST registration; can hit cloud metadata / internal services.

### Top High Risks

- **S-02** SSRF policy permissive by default (DNS re-resolution + private-range blocking both off)
- **S-03** Agent-less tool context defaults to Tier-4 (max) privilege; MCP server uses it
- **S-04** LLM-generated raw SQL validated only by a bypassable keyword denylist
- **A-01/A-02/A-03** God-module `api/views.py` (2,518 LOC), circular import cluster, single-app monolith
- **DO-01/DO-02** Tracing and alerting are documented as operational but not implemented
- **DO-03** Prod Postgres driver + `boto3` unpinned
- **DA-01** Prompts/responses (PII) stored plaintext, no encryption/redaction/erasure
- **DA-02** Retention job never scheduled — GDPR/retention windows unenforced in prod
- **DA-03** Backups documented but not codified or verified
- **DA-04** `AgentRun` (system-of-record) missing indexes on its hottest query columns
- **PF-01/PF-02/PF-03** Synchronous LLM in a 4-worker pool, coincident timeouts, brute-force vector search
- **SP-01/SP-02** Runbook contradicts implemented reality; single-contributor bus factor

### Consolidated Action Register

| Priority | Area | Action Item | Severity |
|---|---|---|---|
| P0 | Security | Disable/re-validate redirects across all `urllib` clients (S-01) | Critical |
| P1 | Security | Enable SSRF DNS + private-block by default (S-02) | High |
| P1 | Security | Default agent-less tool context to lowest tier (S-03) | High |
| P1 | Security | Parameterize / allowlist-parse connector SQL (S-04) | High |
| P1 | Data | Schedule `enforce_retention` + evidence export (DA-02) | High |
| P1 | Data | Encrypt/redact stored prompts & responses; add erasure (DA-01) | High |
| P1 | Data | Codify + verify DB backups (DA-03); add `AgentRun` indexes (DA-04) | High |
| P1 | Performance | Async/offload interactive LLM runs; fix timeout alignment (PF-01/02) | High |
| P1 | DevOps | Implement or de-advertise OTel export & alerting (DO-01/02); pin prod deps (DO-03) | High |
| P1 | Ops | Reconcile RUNBOOK with reality; add 2nd maintainer + README (SP-01/02) | High |
| P2 | Architecture | Split `api/views.py` + `models.py` by domain; break import cycle (A-01/02/03) | High/Med |
| P2 | Performance | Wire pgvector ANN; fix N+1s; paginate; cache aggregations (PF-03/04/05/06) | Medium |
| P2 | Governance | Flip secure defaults on; enforce audit-writes; add coverage gate (GV-01/02/04) | Medium |
| P2 | Data/Sec | Encrypt `DataConnector.config`; fix misleading docstring; scope A2A IDOR (S-05/07) | Medium |
| P3 | UX | Base template; self-host/SRI Chart.js; a11y color+aria fixes; onboarding (UX-01/03/04) | Low-Med |

### Go / No-Go Assessment

**🟡 GO WITH CONDITIONS**

The platform is architecturally sound and demonstrably more mature than typical at this stage — the engineering foundations (governance spine, adapter design, test breadth, CI gating, DR planning) are real and strong. It is **not** a NO-GO: there are no unrecoverable design flaws, and every gap has a scoped, low-risk remediation, several already flagged in the team's own hardening plan.

It is **not** an unconditional GO because of four production-blocking gaps that expose data or systems in a live deployment:

1. **The SSRF redirect bypass (S-01)** is actively exploitable and reachable through externally-registerable destinations — must be fixed before any internet-facing deploy.
2. **PII plaintext + unscheduled retention (DA-01/DA-02)** means sensitive prompts accumulate indefinitely, unencrypted — a data-protection/GDPR exposure.
3. **Backups are documented but not codified or verified (DA-03)** — the DR plan is untested paper.
4. **Tracing and alerting are advertised but not implemented (DO-01/DO-02)** — operators cannot actually detect or diagnose the incidents the RUNBOOK describes.

**Recommended gate to full GO:** clear the P0 + P1 register above (roughly 1–2 focused sprints given the controls are largely scaffolded), execute one evidenced DR restore drill, and flip the secure-by-default flags. On completion the readiness score moves into the mid-80s, meeting the platform's own `PLATFORM_ENTERPRISE_MIN_SCORE=85` bar.

---

*Audit conducted read-only. No files were modified, no code created, no fixes applied, no configuration changed. Severity reflects exploitability/impact in a production `DEBUG=False` deployment with interop surfaces enabled.*
