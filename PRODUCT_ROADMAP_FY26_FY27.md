# Agentic Platform — Product Roadmap (Remaining FY26 → FY27)

> **Status:** Draft for review · **Owner:** Platform Product · **Date:** 2026-07-07
> **Purpose:** Two-track roadmap — (1) Production Hardening and (2) New Functionality — for the
> remaining FY26 and full FY27. This document is the working plan; it will be converted into an
> executive presentation once the priorities and sequencing are agreed.

> **Fiscal calendar assumption:** Fiscal year = calendar year (RELX convention). Therefore
> **Remaining FY26 = H2 2026 (Q3 Jul–Sep, Q4 Oct–Dec)** and **FY27 = full-year 2027 (Q1–Q4)**.
> _Adjust this and the quarter labels if your fiscal calendar differs._

---

## 1. Executive Summary

The Agentic Platform is a **multi-tenant control plane for governed enterprise AI agents**. Over
FY26 we delivered the core capability set — agent lifecycle governance, multi-platform runtime,
guardrails, evaluation gates, observability, cost controls, multi-agent orchestration, a
data/knowledge layer, and an autonomous **Agent Factory** (process insight → blueprint → runnable
agent). The control plane is feature-complete for pilot use.

The next 18 months move the platform from **"feature-complete pilot"** to **"enterprise-grade,
scaled production service."** This requires two parallel tracks:

- **Track 1 — Production Hardening:** the reliability, security, scale, and compliance work needed
  to run business-critical agents at fleet scale with an auditable, certifiable posture.
- **Track 2 — New Functionality:** the capability expansion that grows adoption, self-service, and
  measurable business value.

**Guiding principle:** _Hardening is the gate to scale; new functionality is the engine of adoption._
We resource both concurrently, with hardening front-loaded in H2 FY26.

---

## 2. Where We Are Today (Current-State Snapshot)

| Capability area | What exists today |
|---|---|
| **Agent lifecycle & governance** | Draft→Review→Pilot→Production→Retired state machine; blocking governance review + single-use tier-4 approval tokens; immutable audit log; per-agent risk tiers (1–4). |
| **Multi-tenant model** | 4-level org hierarchy (BusinessUnit→Division→WorkStream→Process); role-based access (viewer/builder/approver/admin); tenant-scoped data isolation. |
| **Multi-platform runtime** | Unified runtime with adapters for Anthropic Claude, Azure OpenAI/GPT-4o, AWS Bedrock, and generic HTTP APIs (Copilot Studio / custom / vendor). |
| **Safety & guardrails** | Pre-LLM content scan (prompt injection, PII, system-prompt override) with off/warn/block enforcement levels. |
| **Evaluation** | Eval suites/cases/runs with pass thresholds; production-promotion gate (model in place). |
| **Observability & cost** | OpenTelemetry spans, Prometheus `/metrics/`, per-agent budget alerts, quality-drift (satisfaction baseline) alerting. |
| **Data & knowledge** | Semantic agent search (embeddings), RAG pipeline (PDF/DOCX/TXT/MD ingestion + chunking), SQL/REST/GraphQL data connectors. |
| **Multi-agent orchestration** | Workflow DAG definition + execution engine, dependency resolution, cross-agent shared memory, retries/timeouts. |
| **Agent Factory** | Process-insight ingestion, opportunity scoring, blueprint generation, approval gate, build compiler, canonical package handoff with authoritative safety boundary; tool-binding lifecycle (proposed→sandbox→live). |
| **Ops & compliance tooling** | Maturity scorecard + enterprise success-criteria gates; retention enforcement; compliance-evidence export; CI release-gate; Render deployment. |

### Known gaps / stubs (inputs to Track 1)
- Workflow execution is **sequential** (no parallel task fan-out).
- Eval gate exists as a model but is **not yet enforced at runtime**.
- Model router decision logic is a **stub** (no live multi-model dispatch).
- Orchestrator resilience is basic (retry only; no circuit breaker / dead-letter / idempotency).
- Connector SSRF protection is basic URL validation.
- Secrets are **env-var references**, not encrypted/managed storage.
- No scheduled/event-driven workflow triggers; no workflow versioning.
- No canary/A-B deployment; cost attribution is aggregate only.
- Single-process workflow worker (management command), not a durable queue.

---

## 3. Current Platform Architecture (logical view)

The platform is a **governed control plane for enterprise AI agents**. Conceptually it separates a
**control plane** (registration, governance, policy, observability, factory) from a **runtime/data
plane** (agent execution, tool calls, orchestration). Everything an agent does flows through the
control plane so it can be governed, audited, costed, and secured.

### 3.1 Layered capability model

```
┌──────────────────────────────────────────────────────────────────────────┐
│  EXPERIENCE LAYER                                                          │
│  Dashboard UI · Registration & builder · Monitoring/analytics · JSON API   │
│  Personas: Viewer · Agent Builder · Agent Approver · Platform Admin        │
├──────────────────────────────────────────────────────────────────────────┤
│  GOVERNANCE & SAFETY LAYER                                                 │
│  Lifecycle state machine · Governance review + approval tokens · RBAC &    │
│  4-level tenant scoping · Guardrails (injection/PII) · Immutable audit log │
│  Eval gates · Risk tiers (1–4)                                             │
├──────────────────────────────────────────────────────────────────────────┤
│  AGENT FACTORY (autonomy)                                                  │
│  Process insight → opportunity scoring → blueprint → approval → build →    │
│  tool-binding lifecycle (proposed→sandbox→live) · canonical package handoff│
├──────────────────────────────────────────────────────────────────────────┤
│  ORCHESTRATION & RUNTIME LAYER                                             │
│  PlatformAgentRuntime · Workflow DAG engine · Shared memory · Model router │
│  Tool registry (builtin + connector tools)                                 │
├──────────────────────────────────────────────────────────────────────────┤
│  INTEGRATION / ADAPTER LAYER                                               │
│  LLM adapters: Anthropic · Azure OpenAI · AWS Bedrock · HTTP API · Echo    │
│  Data connectors: SQL · REST · GraphQL                                     │
├──────────────────────────────────────────────────────────────────────────┤
│  DATA & KNOWLEDGE LAYER                                                    │
│  Embeddings / semantic search · RAG (docs→chunks) · connectors            │
├──────────────────────────────────────────────────────────────────────────┤
│  OBSERVABILITY & OPERATIONS LAYER                                          │
│  OpenTelemetry spans · Prometheus metrics · Budget/quality alerts ·        │
│  Retention · Compliance evidence · Maturity scorecard                      │
└──────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Two governed lifecycles

**Agent lifecycle (governed promotion):**
```
Draft ──▶ Review ──▶ Pilot ──▶ Production ──▶ Retired
                 │                    ▲
        Governance review        gates: approved review
        + Approval token         + valid approval + (eval gate)
```

**Agent Factory lifecycle (autonomous build):**
```
ProcessInsight ─▶ Blueprint (scored) ─▶ Approval gate ─▶ Build (sandbox Agent)
     │                                                        │
 opportunity                                       tool bindings = PROPOSED
   scoring                                     (never live without approval)
                                                             │
                                              promote → sandbox → live (approved)
```
The **safety boundary** on the factory package is authoritative: a package can only ever produce a
**draft/sandbox** agent with **proposed** tool bindings; production and live binding require explicit
human/policy approval — this is enforced at ingestion and cannot be overridden by package content.

### 3.3 Multi-tenancy

Isolation is enforced at every layer via a 4-level hierarchy
(**BusinessUnit → Division → WorkStream → OrgProcess**). Each user has a `UserProfile` with a home
BusinessUnit and a role; non-privileged users are transparently filtered to their tenant on every
list/detail/mutation endpoint. Staff/superuser/platform_admin are cross-tenant by design.

---

## 4. Current Technical Architecture

### 4.1 Technology stack

| Concern | Technology |
|---|---|
| Language / runtime | Python 3.12 |
| Web framework | Django 5.2 (single project `agentic_platform`, single app `controlplane`) |
| App server | Gunicorn (WSGI) |
| Static assets | WhiteNoise (compressed, manifest storage) |
| Database | PostgreSQL (prod) or SQLite (local/demo), via `dj-database-url` (`conn_max_age=600`) |
| Vector search | pgvector on Postgres; Python cosine fallback on SQLite |
| LLM SDKs | `anthropic`, `openai` (Azure/OpenAI), `boto3` (Bedrock) |
| Knowledge/RAG | `tiktoken` (token-aware chunking), `pypdf`, `python-docx` |
| Config | `python-dotenv` + environment variables |
| CI/CD | GitHub Actions release-gate; Render (build → collectstatic → migrate → gunicorn) |
| API style | Plain Django views returning JSON (no DRF); versioned as `v1` with policy headers |

### 4.2 Codebase structure

```
agentic_platform/            # Django project (settings, urls, wsgi)
controlplane/
├── models.py                # All domain models (agents, runs, workflows, factory, governance…)
├── views.py                 # Dashboard + server-rendered views
├── security.py              # Tenant/role helpers (can_access_*, business_unit resolution)
├── middleware.py            # API version headers + global rate limiting
├── api/
│   ├── urls.py              # ~40 JSON endpoints (v1)
│   ├── views.py             # Endpoint handlers (auth, RBAC, tenant scoping, rate limits)
│   └── aggregations.py      # Monitoring/analytics rollups
├── services/                # Domain/business logic (framework-light)
│   ├── governance.py        # Registration, versioning, approvals, transitions, gates
│   ├── agent_runtime.py     # PlatformAgentRuntime — unified execution entrypoint (SSE)
│   ├── orchestrator.py      # Workflow DAG execution engine
│   ├── workflow_compiler.py # Blueprint → Workflow DAG
│   ├── workflow_queue.py    # Queue/worker plumbing for workflow runs
│   ├── factory.py           # Opportunity scoring, blueprint generation, build compiler
│   ├── package_ingestor.py  # Canonical factory-package validation + ingestion
│   ├── guardrails.py        # Prompt-injection / PII / override scanning
│   ├── eval_service.py      # Eval suite execution + gating
│   ├── model_router.py      # Model selection (routing)
│   ├── embeddings.py, rag.py # Semantic search + retrieval
│   ├── metrics.py, telemetry.py, pricing.py, platform_maturity.py, memory.py
│   ├── adapters/            # base · django_runtime(Claude) · openai · bedrock · http_api · echo
│   ├── connectors/          # sql_connector · rest_connector (circuit breakers)
│   └── tools/               # registry · builtins (memory_*) · bindings (proposed→sandbox→live)
└── management/commands/     # Operational batch jobs (see §4.6)
```

### 4.3 Request & middleware flow

```
Client ─▶ SecurityMiddleware ─▶ WhiteNoise ─▶ Session ─▶ Common ─▶ CSRF
      ─▶ Auth ─▶ ApiGlobalRateLimitMiddleware ─▶ Messages ─▶ Clickjacking
      ─▶ ApiVersionHeadersMiddleware ─▶ View
                                          │
      login_required ─▶ role check ─▶ tenant scoping ─▶ handler ─▶ JSON
```
Every protected endpoint enforces authentication, role authorization, and tenant filtering, and
mutations to privileged fields are written to the immutable `AuditLog`.

### 4.4 Agent execution path (runtime/data plane)

```
API/UI ─▶ PlatformAgentRuntime.stream(message, session)
   1. Create AgentRun (status=started)
   2. Guardrails.scan() ─▶ off/warn/block per agent.guardrail_level
   3. Resolve toolset (executable bindings; sandbox vs live mode)
   4. Select adapter via platform map:
        django_runtime→Claude · azure→OpenAI · bedrock→Bedrock
        copilot/custom/vendor→HTTP API · embedded→Echo
   5. Stream tokens as SSE RuntimeEvents (run_started/token/completed/failed)
   6. On completion: price_run() (tokens→cost), telemetry, OTel span,
      update Agent metrics (runs, cost, satisfaction)
```

### 4.5 Multi-agent orchestration

`WorkflowCompiler` turns an approved blueprint into a `Workflow` of `WorkflowTask` nodes with
`depends_on` edges and Jinja2 `input_template`s (`{{inputs.x}}`, `{{outputs.STEP.key}}`). The
`OrchestratorService` executes the DAG via topological readiness walk, substituting upstream outputs,
invoking each node through `PlatformAgentRuntime`, applying per-task retries/timeouts, and persisting
`WorkflowTaskRun` records + aggregated `WorkflowRun.outputs`. Cross-node state passes through
`SharedMemory` (builtin `memory_read/write/list` tools). _Execution is currently sequential — parallel
fan-out and durable queueing are Track-1 hardening items._

### 4.6 Batch / worker plane (management commands)

`process_workflow_runs` (queue worker), `enforce_retention`, `export_compliance_evidence`,
`platform_maturity_report` / `enterprise_success_criteria` (readiness gates, also run in CI),
`compute_baselines` (quality drift), `compute_budgets` (budget alerts), `embed_agents` (embedding
pipeline), plus `seed_demo` / `seed_metrics` for local data.

### 4.7 Security architecture

- **Identity/access:** Django auth + session cookies; `login_required` everywhere; four roles; `UserProfile` tenant home BU.
- **Tenant isolation:** enforced in `security.py` helpers and applied per endpoint; cross-tenant only for staff/admin.
- **API protection:** global per-user/IP rate limiting; versioned API with policy/deprecation headers.
- **Content safety:** guardrail scanning (injection/PII/override) with off/warn/block enforcement.
- **Auditability:** append-only `AuditLog` with save/delete overrides preventing mutation.
- **Connector safety:** circuit breakers on SQL/REST connectors; config holds no raw secrets; tool bindings gated proposed→sandbox→live.
- **Production hardening (DEBUG=off):** refuses to boot with default secret key; forced HTTPS + HSTS (1yr, preload), secure/HttpOnly cookies, nosniff, `X-Frame-Options: DENY`, `SECURE_PROXY_SSL_HEADER` for TLS-terminating proxy.

### 4.8 Deployment topology

```
                 ┌─────────────────────────────────────────────┐
   GitHub  ──────▶  Actions release-gate (maturity + success    │
   (PR/push)      │  criteria + regression test suites)         │
                 └──────────────────────┬──────────────────────┘
                                        ▼ (on pass)
   ┌──────────────┐   HTTPS    ┌────────────────────┐   ┌────────────────────┐
   │   Browser /  │ ─────────▶ │  Render web service │──▶│  PostgreSQL (+pgvec)│
   │   API client │            │  gunicorn + Django  │   │  managed DB         │
   └──────────────┘            │  WhiteNoise statics │   └────────────────────┘
                               └─────────┬──────────┘
             build: pip install ─▶ collectstatic ─▶ migrate ─▶ gunicorn
                                         │
                    External LLM/data providers (Anthropic, Azure OpenAI,
                    AWS Bedrock, HTTP APIs, SQL/REST data sources)
```
_Today this is a single Render web service + managed Postgres with a command-driven worker. The
Track-1 hardening plan evolves this toward a durable queue with autoscaled workers, HA Postgres, a
staging environment, and blue/green deploys._

### 4.9 Architectural constraints feeding the roadmap

| Constraint today | Track-1 target |
|---|---|
| Sequential DAG execution | Parallel fan-out (Track 1A) |
| Command-driven single-process worker | Durable queue + autoscaled workers (Track 1A) |
| Env-var secrets | Managed/encrypted secrets vault (Track 1B) |
| Basic connector URL validation | SSRF/egress hardening (Track 1B) |
| Eval gate modeled, not runtime-enforced | Enforce at promotion (Track 1E) |
| Single service, no staging | Staging + blue/green + HA/DR (Track 1E, 1A) |

---

## 5. Target ("To-Be") Architecture — end of FY27

This is the intended end state once the roadmap is delivered. Items marked **[NEW]** are added by the
roadmap; everything else is a hardened/scaled evolution of what exists today. The core design
principle is unchanged: **a governed control plane in front of every agent action** — the roadmap
makes it durable, secure, certifiable, self-service, and interoperable.

### 5.1 To-Be logical capability model

```
┌──────────────────────────────────────────────────────────────────────────┐
│  EXPERIENCE LAYER                                                          │
│  Dashboard · [NEW] Self-service low-code builder · [NEW] Human-in-the-loop │
│  task inbox · [NEW] Exec ROI/adoption analytics & chargeback ·             │
│  [NEW] Agent & tool Marketplace · Channels: web/API + [NEW] Teams/Slack/voice│
├──────────────────────────────────────────────────────────────────────────┤
│  GOVERNANCE & SAFETY LAYER                                                 │
│  Lifecycle + [NEW] runtime-enforced eval gate · [NEW] SSO/SAML/OIDC + SCIM │
│  · Fine-grained RBAC · [NEW] Guardrails 2.0 (jailbreak/toxicity/           │
│  groundedness/output-schema) · Immutable audit · [NEW] SOC 2 controls ·    │
│  [NEW] data residency & per-tenant retention · [NEW] risk register          │
├──────────────────────────────────────────────────────────────────────────┤
│  AGENT FACTORY (autonomy)                                                  │
│  Continuous insight ingestion · [NEW] telemetry→blueprint feedback loop ·  │
│  [NEW] portfolio/ROI ranking · approval + build · binding lifecycle        │
├──────────────────────────────────────────────────────────────────────────┤
│  ORCHESTRATION & RUNTIME LAYER                                             │
│  [NEW] Parallel DAG fan-out · [NEW] durable queue + autoscaled workers ·   │
│  [NEW] circuit breaker/dead-letter/idempotency · [NEW] scheduled &         │
│  event/webhook triggers · [NEW] workflow versioning + template library ·   │
│  [NEW] live cost/latency/quality model router · [NEW] canary / A-B / rollback│
├──────────────────────────────────────────────────────────────────────────┤
│  INTEGRATION / ADAPTER LAYER                                               │
│  LLM adapters (Claude/Azure/Bedrock/HTTP) · [NEW] MCP tools/servers ·      │
│  [NEW] SaaS connector catalog (ServiceNow/Salesforce/SharePoint/Snowflake) │
│  · [NEW] connector SDK · [NEW] bi-directional Azure AI / Copilot sync      │
├──────────────────────────────────────────────────────────────────────────┤
│  DATA & KNOWLEDGE LAYER                                                    │
│  [NEW] Advanced RAG (hybrid search + reranking, multi-modal) ·             │
│  embeddings/semantic search · [NEW] embedding/RAG + prompt caching         │
├──────────────────────────────────────────────────────────────────────────┤
│  OBSERVABILITY & OPERATIONS LAYER                                          │
│  OTel → [NEW] external APM + golden dashboards · [NEW] alerting + on-call   │
│  runbooks · [NEW] log aggregation + PII scrubbing · [NEW] platform cost     │
│  dashboard · [NEW] automated compliance-evidence pipeline · maturity gates │
├──────────────────────────────────────────────────────────────────────────┤
│  PLATFORM FOUNDATION                                                       │
│  [NEW] Secrets vault (encrypted, rotated) · [NEW] encryption at rest /      │
│  field-level · [NEW] SSRF/egress controls · [NEW] HA/DR + backups/restore  │
└──────────────────────────────────────────────────────────────────────────┘
```

### 5.2 To-Be technical / deployment topology

```
                 ┌──────────────────────────────────────────────────────┐
   GitHub ───────▶  Actions: release-gate + [NEW] contract tests +        │
   (PR/push)      │  migration safety ─▶ [NEW] Staging ─▶ [NEW] blue/green │
                 └───────────────────────────┬──────────────────────────┘
                                             ▼
   ┌────────────┐         ┌────────────────────────────┐   ┌──────────────────────┐
   │ [NEW] IdP  │──SSO───▶│  Web tier (autoscaled       │──▶│ [NEW] HA PostgreSQL  │
   │ SAML/OIDC  │  SCIM   │  gunicorn/Django + WhiteNoise)│  │  +pgvector, backups, │
   └────────────┘         └───────────────┬─────────────┘   │  tested restore (DR) │
                                          │                 └──────────────────────┘
   ┌────────────┐   enqueue    ┌──────────▼──────────┐      ┌──────────────────────┐
   │  Clients / │─────────────▶│ [NEW] Durable queue  │      │ [NEW] Secrets vault  │
   │  events /  │              │  + broker            │◀────▶│  (rotation) +        │
   │  webhooks  │              └──────────┬──────────┘      │  encryption at rest  │
   └────────────┘                         ▼                 └──────────────────────┘
                              ┌────────────────────────┐
                              │ [NEW] Autoscaled agent  │──▶ LLM providers (via [NEW]
                              │  & workflow workers      │    live model router) +
                              │  (parallel DAG fan-out)  │    [NEW] MCP + SaaS connectors
                              └───────────┬─────────────┘         │
                                          ▼                       ▼
                     OTel/logs/metrics ─▶ [NEW] External APM + log aggregation
                                          + alerting/on-call + cost dashboard
```

### 5.3 Current → Target transformation summary

| Dimension | Current (today) | Target (end FY27) |
|---|---|---|
| **Execution** | Sequential DAG, command-driven single worker | Parallel fan-out, durable queue, autoscaled workers, idempotent re-drive |
| **Resilience** | Retry only | Circuit breaker, dead-letter, self-healing, HA/DR with tested restore |
| **Secrets & data** | Env-var refs | Managed vault + rotation, encryption at rest / field-level |
| **Identity** | Django login + roles | Enterprise SSO (SAML/OIDC) + SCIM, fine-grained RBAC |
| **Egress safety** | Basic URL validation | SSRF/egress allowlists, DNS-rebind protection |
| **Quality gate** | Eval model, not enforced | Eval gate enforced at promotion; canary/A-B + auto-rollback |
| **Guardrails** | Injection/PII scan | + jailbreak, toxicity, groundedness, output-schema validation |
| **Triggers** | Manual/API only | + scheduled (cron) and event/webhook-driven |
| **Model use** | Static per-agent | Live cost/latency/quality-aware router + prompt caching |
| **Integration** | SQL/REST/GraphQL + LLM adapters | + MCP, SaaS connector catalog, connector SDK, bi-directional platform sync |
| **Knowledge** | Baseline RAG | Hybrid search + reranking, multi-modal, cached |
| **Observability** | OTel/Prometheus in-app | External APM, golden dashboards, alerting + on-call runbooks, log aggregation |
| **Delivery** | Single service, no staging | Staging + blue/green, contract tests, migration safety |
| **Compliance** | Audit log + evidence export cmd | Automated evidence pipeline, SOC 2 Type II-ready, data residency, risk register |
| **Experience** | Dashboard + registration + JSON API | + self-service builder, HITL inbox, ROI analytics/chargeback, marketplace, more channels |
| **Readiness tier** | Feature-complete pilot | Enterprise-grade, certifiable, scaled production service |

---

## 6. Strategic Themes (the "why" behind the tracks)

1. **Trust & Safety at scale** — make the platform certifiably secure and compliant.
2. **Reliability & Scale** — durable execution, HA/DR, performance under fleet load.
3. **Time-to-value & Self-service** — let business units build, test, and ship agents faster.
4. **Measurable ROI** — cost transparency, value attribution, executive analytics.
5. **Ecosystem & Interoperability** — connectors, MCP, and bi-directional platform sync.

---

## 7. Track 1 — Production Hardening

Organized into six workstreams. Each item lists a target quarter and a definition of done (DoD).

### 1A. Reliability & Durable Execution
| Item | Target | Definition of done |
|---|---|---|
| Durable workflow queue (replace single-process worker with Celery/RQ + broker) | Q3'26 | At-least-once execution, retries, visibility timeouts; worker autoscaling. |
| Parallel task fan-out in orchestrator | Q3'26 | Independent DAG nodes run concurrently; correctness preserved. |
| Orchestrator resilience (circuit breaker, dead-letter, idempotency keys) | Q4'26 | Poison runs quarantined; safe re-drive; no duplicate side-effects. |
| Stale-run recovery & self-healing | Q4'26 | Stuck RUNNING runs auto-detected and re-queued/failed with alert. |
| HA/DR: Postgres HA, automated backups, tested restore, RPO/RTO targets | Q1'27 | Documented DR runbook; quarterly restore drill passes. |

### 1B. Security Hardening
| Item | Target | Definition of done |
|---|---|---|
| Secrets management (managed vault; encrypted at rest; rotation) | Q3'26 | No plaintext secrets in env/DB; connector creds vaulted + rotatable. |
| SSRF / egress hardening on connectors (allowlists, metadata-endpoint block, DNS-rebind protection) | Q3'26 | Connector requests constrained to approved egress; abuse tests pass. |
| Enterprise SSO (SAML/OIDC) + SCIM provisioning | Q4'26 | IdP-federated login; automated user/role provisioning & deprovisioning. |
| Fine-grained RBAC & least-privilege review | Q4'26 | Permissions matrix documented and enforced per endpoint. |
| Third-party penetration test + remediation | Q1'27 | External pentest completed; criticals/highs remediated. |
| Encryption at rest + field-level encryption for sensitive payloads | Q1'27 | Run I/O and PII fields encrypted; key management documented. |

### 1C. Scale & Performance
| Item | Target | Definition of done |
|---|---|---|
| Load & soak testing harness + published SLOs | Q4'26 | Documented throughput/latency envelope; SLO dashboard live. |
| DB connection pooling, query/index tuning, N+1 elimination | Q4'26 | P95 API latency within SLO under target concurrency. |
| Horizontal scale for runtime + workers (autoscaling) | Q1'27 | Linear scale demonstrated to target agent-fleet size. |
| Caching layer (prompt caching, embedding/RAG cache) | Q2'27 | Measurable latency + token-cost reduction on hot paths. |

### 1D. Observability & Operations
| Item | Target | Definition of done |
|---|---|---|
| OTLP exporter wiring to external collector + standard dashboards | Q3'26 | Traces/metrics flow to APM; golden dashboards published. |
| Alerting + on-call runbooks (SLO breach, queue backlog, budget, quality drift) | Q4'26 | PagerDuty/Opsgenie alerts with runbooks; MTTR baseline set. |
| Structured logging + log aggregation + PII scrubbing | Q4'26 | Centralized, searchable logs; no PII leakage in logs. |
| Cost & usage operational dashboard (platform-wide) | Q1'27 | Real-time spend, per-tenant/per-agent breakdown for ops. |

### 1E. Quality Gates & Release Engineering
| Item | Target | Definition of done |
|---|---|---|
| **Enforce eval gate at runtime** (block promotion on failing active suite) | Q3'26 | Promotion API rejects agents whose active eval suite fails. |
| Staging environment + blue/green (or canary) deploys | Q4'26 | Zero-downtime deploys; automated rollback on health regression. |
| Migration safety (backward-compatible, gated, tested) | Q4'26 | Migrations run in CI against prod-shaped data; rollback plan. |
| Expand automated test coverage + contract tests for API v1 | Q1'27 | Coverage target met; API contract regression suite green. |

### 1F. Compliance & Governance
| Item | Target | Definition of done |
|---|---|---|
| Automated compliance evidence pipeline (extend export command) | Q4'26 | Scheduled evidence bundles; tamper-evident audit export. |
| SOC 2 Type II readiness (controls mapped + evidence) | Q2'27 | Control catalog mapped to platform features; audit-ready. |
| Data residency & retention policy per tenant | Q1'27 | Configurable residency/retention enforced and audited. |
| Model/agent risk register + review cadence | Q1'27 | Documented risk register; periodic governance review workflow. |

---

## 8. Track 2 — New Functionality

Grouped by theme. Each item lists a target quarter and the value it unlocks.

### 2A. Agent Factory & Autonomy (grow the funnel)
| Item | Target | Value |
|---|---|---|
| Continuous insight ingestion + auto-blueprint at scale | Q4'26 | Turns process-mining findings into a steady pipeline of agent candidates. |
| Feedback loop: runtime telemetry → blueprint refinement | Q1'27 | Agents self-improve from real usage; closes the discovery→build→learn loop. |
| Blueprint recommendation ranking + portfolio view | Q1'27 | Leaders prioritize the highest-ROI automations. |

### 2B. Deployment Sophistication
| Item | Target | Value |
|---|---|---|
| Canary / A-B testing for agents (traffic splitting) | Q1'27 | Safe, data-driven rollout of new prompts/models/versions. |
| Progressive rollout + automatic rollback on quality/cost regression | Q2'27 | De-risks production changes; protects SLOs and budgets. |
| Agent versioning + one-click rollback (UX) | Q4'26 | Operators recover instantly from bad versions. |

### 2C. Orchestration & Scheduling
| Item | Target | Value |
|---|---|---|
| Scheduled & event-driven workflow triggers (cron + webhooks/events) | Q4'26 | Unattended automations; integration with business events. |
| Workflow versioning + templates library | Q1'27 | Reusable, governed workflow patterns across BUs. |
| Human-in-the-loop task inbox (approvals, exceptions, escalations) | Q1'27 | Operationalizes human oversight for higher-risk automations. |

### 2D. Ecosystem & Interoperability
| Item | Target | Value |
|---|---|---|
| **MCP (Model Context Protocol) support** — consume MCP tools/servers | Q4'26 | Instant access to a growing ecosystem of standardized tools. |
| SaaS connector catalog (ServiceNow, Salesforce, SharePoint, Snowflake, etc.) | Q1'27–Q2'27 | Drops integration time from weeks to minutes. |
| Complete GraphQL connector + connector SDK for custom sources | Q2'27 | Partners/BUs self-serve new integrations. |
| Bi-directional external platform sync (Azure AI Foundry, Copilot Studio) | Q3'27 | Single pane of governance over agents built elsewhere. |

### 2E. Intelligence & Quality
| Item | Target | Value |
|---|---|---|
| Live model router (cost/latency/quality-aware multi-model dispatch) | Q4'26 | Automatic cost/perf optimization per request. |
| Advanced RAG (hybrid search + reranking, multi-modal ingestion) | Q1'27 | Higher answer accuracy; supports images/tables/scanned docs. |
| Guardrails 2.0 (jailbreak, toxicity, hallucination/groundedness, output schema validation) | Q2'27 | Broader, stronger safety envelope for regulated use cases. |
| Evaluation platform (LLM-as-judge, regression suites, red-teaming) | Q2'27 | Continuous quality assurance; trustworthy releases. |

### 2F. Experience & Business Value
| Item | Target | Value |
|---|---|---|
| Self-service low-code agent builder UI | Q1'27–Q2'27 | Business users build agents without engineering. |
| Executive ROI & adoption analytics (value attribution, chargeback) | Q1'27 | Quantifies platform impact; enables cost recovery. |
| Marketplace / catalog of reusable agents & tools | Q3'27 | Network effects; accelerates reuse across the enterprise. |
| Additional channels (Teams/Slack, voice, streaming multi-agent) | Q3'27–Q4'27 | Meets users where they work; expands surface area. |

---

## 9. Timeline at a Glance

> Legend: **[H]** = Hardening (Track 1), **[F]** = Functionality (Track 2)

### Remaining FY26
**Q3 2026 (Jul–Sep) — "Foundations for scale"**
- [H] Durable workflow queue · Parallel task fan-out
- [H] Secrets management · SSRF/egress hardening
- [H] OTLP exporter + golden dashboards
- [H] Enforce eval gate at runtime
- [F] _(spillover buffer; primarily a hardening-heavy quarter)_

**Q4 2026 (Oct–Dec) — "Resilience, security & first big features"**
- [H] Orchestrator resilience · Self-healing · Alerting + runbooks · Structured logging
- [H] Enterprise SSO/SCIM · Fine-grained RBAC
- [H] Load testing + SLOs · DB tuning · Staging + blue/green + migration safety
- [H] Automated compliance evidence pipeline
- [F] MCP support · Live model router · Scheduled/event triggers · Agent versioning + rollback UX

### FY27
**Q1 2027 — "Enterprise-grade + adoption"**
- [H] HA/DR · Pentest + remediation · Encryption at rest · Horizontal scale · Data residency · Cost dashboard · Risk register · Test coverage/contract tests
- [F] Canary/A-B testing · Workflow versioning + templates · Human-in-the-loop inbox · Advanced RAG · Factory feedback loop + portfolio view · SaaS connectors (wave 1) · Self-service builder (start) · ROI analytics

**Q2 2027 — "Certification + differentiation"**
- [H] SOC 2 Type II readiness · Caching layer
- [F] Progressive rollout + auto-rollback · Guardrails 2.0 · Evaluation platform · SaaS connectors (wave 2) · GraphQL + connector SDK · Self-service builder (GA)

**Q3 2027 — "Ecosystem & reach"**
- [F] Bi-directional external platform sync · Marketplace/catalog · Additional channels (start)

**Q4 2027 — "Scale-out & network effects"**
- [F] Additional channels (voice/streaming) · Ecosystem expansion · FY28 planning

---

## 10. Success Metrics (proposed)

**Reliability & scale**
- Workflow-run success rate ≥ 99% (currently a maturity check)
- API P95 latency ≤ SLO under target concurrency
- Zero-downtime deploys; MTTR < 30 min

**Security & compliance**
- Pentest criticals/highs remediated: 100%
- SOC 2 Type II audit-ready by end of FY27
- 100% of connector secrets vaulted & rotatable

**Adoption & value**
- Agent fleet size (active production agents) — set baseline in Q3'26, growth target thereafter
- Self-service agents built without engineering
- Documented ROI / cost-savings per BU; platform maturity score ≥ 85 ("enterprise-ready") sustained

---

## 11. Risks & Dependencies

| Risk | Mitigation |
|---|---|
| Hardening vs. functionality resourcing contention | Front-load hardening in H2 FY26; protect a dedicated hardening capacity allocation. |
| Compliance timelines (SOC 2) gate enterprise deals | Start controls mapping in Q4'26 so evidence accrues ahead of Q2'27 audit. |
| External platform sync depends on third-party APIs | Treat bi-directional sync as FY27-late; keep one-way as fallback. |
| Model/provider cost volatility | Live model router + caching in FY27 to actively manage spend. |
| Scale unknowns until load testing | Q4'26 load test sets the true performance envelope before HA/scale build-out. |

---

## 12. Open Questions (to confirm before the presentation)

1. **Fiscal calendar** — confirm FY = calendar year (assumed here) vs. a different fiscal boundary.
2. **Audience** — is the deck for executive leadership (value/timeline framing) or engineering (delivery detail)? Affects depth.
3. **Capacity** — how many engineers/squads across the two tracks? Sequencing assumes concurrent tracks.
4. **Hard commitments** — any fixed external dates (customer commitments, audit windows, board milestones)?
5. **Priority ties** — if forced to choose, does SOC 2 / security certification precede self-service builder, or vice versa?
```
