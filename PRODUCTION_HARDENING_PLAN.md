# Production Hardening Plan — "101% production-ready"

> **Purpose:** the definitive, implement-later plan to take the Agentic Platform (feature- and
> interop-complete after the Agent Fabric transformation) to **enterprise production readiness**.
> Every item is scoped, grounded in the current codebase, classified **code / infra / hybrid**,
> and mapped to a gate. Implement top-to-bottom within each gate; each item ships as its own
> tested, reversible increment (the cadence used throughout the transformation).
>
> **Companion docs:** `AGENTIC_PLATFORM_ROADMAP_DECK.md` (exec narrative), `PRODUCT_ROADMAP_FY26_FY27.md`
> (per-item DoD). **Prepared:** 25 Jul 2026.

---

## 1. What "101% production-ready" means (the exit bar)

Beyond "it runs": the platform must **survive failure, prove compliance, scale under load, and be
operable by an on-call team** — with evidence, not assertion. The +1% is the deliberate margin:
we don't just meet each control, we **test that it fails safely** (DR drills, chaos, red-teaming),
so readiness is demonstrated, not assumed.

A gate passes only when its controls are (a) implemented, (b) covered by an automated test or a
documented drill, and (c) producing evidence an auditor or on-call engineer can read.

---

## 2. Current baseline (already in place — do not rebuild)

- **Governance & audit:** single-choke-point `GovernanceService`, immutable `AuditLog`, 3-gate
  production promotion, risk-tiering, guardrails (`services/guardrails.py`).
- **Durable execution:** Celery/Redis backend behind `WorkflowQueueService`; `AsyncAgentTask`;
  stale-run recovery in `workflow_queue.recover_stale_running_runs`.
- **Reliability (partial):** connector circuit breakers (`rest_connector`/`sql_connector`),
  retry/skip in the orchestrator, **parallel task fan-out (just shipped)**.
- **Security (partial):** shared SSRF `net_guard` (DNS-resolving for the HTTP adapter),
  SELECT-only SQL, config-only secrets (no raw secrets in models), API rate-limit middleware.
- **Observability:** OTel spans, Prometheus `/metrics/`, budget/quality-drift alerts, pricing.
- **Compliance (partial):** `export_compliance_evidence`, `enforce_retention` commands.

The plan below **hardens and completes** these — it does not restart them.

---

## 3. Principles

1. **No capability regression.** Every increment keeps the 535-test suite green and preserves the
   governance invariants (gate never moves, audit never bypassed).
2. **Testable or drilled.** A control that can't be demonstrated isn't done.
3. **Safe by default, opt-in escalation.** New behaviours ship behind flags defaulting to today's
   behaviour (as `EXECUTION_BACKEND`, `ORCHESTRATOR_MAX_PARALLEL`, `BROKER_ROUTER_MODE` already do).
4. **Code before infra.** Land the app-level controls first (they're portable and testable); wire
   the platform/infra controls (HA/DR, autoscaling) against them.

---

## 4. Workstreams

Each item: **ID · What — Why · How (grounded) · Acceptance · Type · Effort(S/M/L) · Gate.**

### A. Reliability & resilience

- **A1 · Idempotency keys for runs/tasks** — Why: retried Celery deliveries or double-clicks must
  not double-execute. How: add an `idempotency_key` to `WorkflowRun`/`AsyncAgentTask`; `enqueue`/
  `submit` dedupe on it; `execute` is a no-op if already terminal (partly there). Acceptance: a
  duplicate enqueue with the same key yields one execution. *Code · M · G2*
- **A2 · Dead-letter queue** — Why: poison tasks must not spin forever. How: after N failed
  claims, move a `WorkflowRun`/task to a `DEAD_LETTER` state with the last error; a
  `list_dead_letters` API + requeue action. Acceptance: a task failing > N times lands in
  dead-letter, not retried. *Code · M · G2*
- **A3 · Orchestrator circuit breaker** — Why: a failing agent shouldn't be hammered across a
  fleet. How: reuse the connector breaker pattern (cache-based) keyed by agent; skip/fail-fast
  when open. Acceptance: N consecutive agent failures open the breaker; a test proves fast-fail.
  *Code · M · G2*
- **A4 · Bounded parallelism + backpressure** — Why: fan-out must not exhaust workers/DB. How:
  `ORCHESTRATOR_MAX_PARALLEL` exists; add a global concurrency semaphore + queue depth limit; shed
  or delay when `PLATFORM_QUEUE_PENDING_WARN_THRESHOLD` exceeded. Acceptance: load test holds p95
  under target with bounded workers. *Code · M · G2*
- **A5 · Timeout & cancellation** — Why: hung agent calls must free workers. How: per-run
  deadline; cooperative cancel via `AsyncAgentTask` state → `CANCELED`; Celery soft/hard limits
  already set. Acceptance: a run exceeding its deadline is CANCELED and the worker freed.
  *Code · M · G2*
- **A6 · Self-healing worker supervision** — Why: crashed workers must recover. How: containerised
  worker with restart policy; `recover_stale_running_runs` on a schedule. Acceptance: killing a
  worker mid-run recovers the run within the stale window (drill). *Hybrid · M · G3*

### B. Scale & performance

- **B1 · Load & soak harness + published SLOs** — Why: prove concurrent fleets don't degrade. How:
  a `locust`/k6 scenario driving `/api/agents/<id>/run/` + workflows; assert p50/p95/p99 vs
  `PLATFORM_SLO_*`. Acceptance: documented SLOs met twice on target hardware. *Code · M · G2*
- **B2 · DB tuning + connection pooling** — Why: Postgres is the bottleneck under fan-out. How:
  `conn_max_age`, PgBouncer, indexes on hot paths (AgentRun.started_at, AuditLog, RegistryEntry —
  some exist), query review of `aggregations.py`. Acceptance: no N+1 on dashboards; pool saturates
  gracefully. *Hybrid · M · G3*
- **B3 · Caching layer** — Why: repeated prompts/embeddings/RAG are expensive. How: cache
  embeddings + RAG retrievals + model-route decisions (Redis). Acceptance: cache-hit path
  measurably faster; correctness unchanged. *Code · M · G4*
- **B4 · Horizontal autoscaling** — Why: absorb spikes. How: stateless web + worker pools scale on
  queue depth / CPU. Acceptance: autoscale event under load, no dropped runs. *Infra · L · G3*

### C. Security & identity

- **C1 · Enterprise SSO (SAML/OIDC) + SCIM** — Why: named users, deprovisioning. How: `mozilla-
  django-oidc` / SAML; map to existing `UserProfile.role` + BU. Acceptance: SSO login + SCIM
  deprovision revokes access. *Hybrid · L · G1*
- **C2 · Secrets vault + rotation** — Why: `auth_ref`/connector configs reference secrets; need a
  real store. How: back `auth_ref`/`DataConnector.config` with Vault/cloud secrets manager;
  resolve at call time; rotation runbook. Acceptance: no secret material in DB/logs; rotation
  without downtime. *Hybrid · L · G1*
- **C3 · Egress hardening completion** — Why: SSRF/exfiltration. How: `net_guard` exists; add an
  outbound allowlist per connector/MCP server, and enable `resolve=True` for MCP/A2A clients where
  DNS-rebinding matters. Acceptance: a rebinding/allowlist test blocks disallowed egress.
  *Code · S · G1*
- **C4 · A2A/MCP inbound auth upgrade** — Why: bearer tokens are the interim (`A2A_ACCESS_TOKENS`).
  How: per-consumer OIDC/mTLS; scoped tokens; rotation. Acceptance: an unauthorised A2A caller is
  rejected; token scope enforced. *Code · M · G3*
- **C5 · Fine-grained RBAC + least-privilege review** — Why: privilege creep. How: audit role→action
  matrix (`security.require_role_json`); add per-BU scoping tests for every mutating endpoint.
  Acceptance: a matrix test asserts each endpoint's required role. *Code · M · G2*
- **C6 · Dependency & supply-chain scanning** — Why: CVEs, typosquats. How: `pip-audit` +
  Dependabot + SBOM in CI; pin/verify. Acceptance: CI fails on a known-vuln dependency.
  *Infra · S · G1*
- **C7 · Pentest + remediation** — Why: independent assurance. How: third-party test of the
  `/api/v1`, `/a2a/` surfaces + auth. Acceptance: criticals/highs remediated. *Infra · L · G3*

### D. AI quality & safety

- **D1 · Runtime eval gate enforcement** — Why: modelled but not blocking at promotion. How:
  `GovernanceService.transition` already checks a passing `EvalRun`; wire it to **block** and add a
  regression: promotion fails on a failing suite. Acceptance: promoting an agent with a failing
  eval is rejected in prod. *Code · S · G1*
- **D2 · Canary / A-B + auto-rollback** — Why: catch quality/cost regressions in prod. How: traffic
  split via `model_router`/broker; monitor `quality_alert`/`budget_alert`; auto-revert on breach.
  Acceptance: a seeded regression triggers rollback. *Code · L · G3*
- **D3 · Guardrails 2.0** — Why: jailbreak/toxicity/groundedness/output-schema. How: extend
  `services/guardrails.py` with groundedness + schema validation + an LLM-judge option; keep
  off/warn/block per agent. Acceptance: new rule classes block in tests. *Code · M · G4*
- **D4 · Evaluation platform** — Why: LLM-as-judge, red-teaming, regression suites. How: build on
  `eval_service`/`EvalSuite`; scheduled regression + adversarial suites. Acceptance: red-team
  suite runs in CI and gates. *Code · L · G4*

### E. Operations & observability

- **E1 · External APM + golden dashboards** — Why: detect/explain/recover. How: OTLP exporter →
  APM (Grafana/Datadog); dashboards for latency/success/cost/queue-depth. Acceptance: an induced
  failure is visible + alerts fire. *Hybrid · M · G1*
- **E2 · Structured logging + PII scrubbing + correlation IDs** — Why: debuggable, safe logs. How:
  JSON logs with run/trace ids; scrub PII (reuse guardrail patterns). Acceptance: a log sample has
  no PII and carries a correlation id. *Code · M · G2*
- **E3 · Alerting + on-call runbooks** — Why: MTTR. How: alerts on SLO breach/queue backlog/
  budget/circuit-open; runbooks per alert. Acceptance: on-call drill resolves a paged incident.
  *Hybrid · M · G2*
- **E4 · Staging + blue/green + migration safety** — Why: zero-downtime. How: staging env; expand/
  contract migrations; blue/green deploy. Acceptance: a zero-downtime deploy + a reversible
  migration in a drill. *Infra · L · G2*

### F. Data, compliance & DR

- **F1 · HA Postgres + automated backups + tested restore (RPO/RTO)** — Why: durability. How:
  managed HA Postgres (+pgvector); scheduled backups; **restore drill**. Acceptance: a restore
  drill meets documented RPO/RTO. *Infra · L · G3*
- **F2 · Encryption at rest + field-level for sensitive payloads** — Why: confidentiality. How:
  storage encryption; field encryption for run payloads/packages. Acceptance: at-rest data is
  encrypted; sensitive fields unreadable without keys. *Hybrid · M · G3*
- **F3 · Automated compliance-evidence pipeline** — Why: SOC 2 needs continuous evidence. How:
  schedule `export_compliance_evidence`; retain snapshots; map controls. Acceptance: evidence
  produced on a schedule and stored immutably. *Code · S · G2*
- **F4 · Data residency + per-tenant retention** — Why: regulatory. How: extend `enforce_retention`
  with per-BU windows + residency routing of AI providers. Acceptance: retention runs per-tenant;
  residency policy enforced. *Code · M · G3*
- **F5 · SOC 2 Type II readiness** — Why: certification. How: map controls to evidence; recurring
  independent assurance. Acceptance: audit-ready control set with evidence. *Hybrid · L · G4*
- **F6 · Backup/restore for Redis/queue state** — Why: don't lose in-flight work. How: durable
  broker config + `AsyncAgentTask`/`WorkflowRun` are the source of truth (already persisted).
  Acceptance: broker restart loses no acknowledged work (drill). *Hybrid · S · G3*

### G. The +1% — beyond baseline (deliberate resilience margin)

- **G-1 · Chaos testing** — kill workers/DB/broker under load; assert recovery. *Hybrid · M · G3*
- **G-2 · DR game-days** — scheduled full-restore + failover drills with timed RPO/RTO. *Infra · M · G4*
- **G-3 · Red-team / adversarial evals** — continuous jailbreak/prompt-injection suites gating
  releases. *Code · M · G4*
- **G-4 · FinOps** — per-model/connector/BU cost attribution + budget enforcement + chargeback
  (builds on `pricing`/`budget_alert`). *Code · M · G4*
- **G-5 · Progressive delivery** — automated canary → full rollout with auto-rollback on quality/
  cost/error regression. *Code · L · G4*

---

## 5. Sequencing (gates, aligned to the roadmap deck)

| Gate | Theme | Must-land items |
|---|---|---|
| **G1 · Pilot-ready** | Identity, secrets, quality gate, visibility | C1 SSO/SCIM, C2 secrets vault, C3 egress, C6 supply-chain, D1 eval-gate blocking, E1 APM |
| **G2 · Repeatable production** | Resilience, ops, staging | A1 idempotency, A2 dead-letter, A3 breaker, A4 backpressure, A5 timeouts, B1 load/SLOs, C5 RBAC matrix, E2 logging, E3 alerting/runbooks, E4 staging/blue-green, F3 evidence pipeline |
| **G3 · Connected service** | HA/DR, scale, hardening | A6 self-healing, B2 DB tuning, B4 autoscaling, C4 A2A/MCP OIDC, C7 pentest, D2 canary/rollback, F1 HA+restore, F2 encryption, F4 residency, F6 broker durability, G-1 chaos |
| **G4 · Enterprise scale + certify** | Certification, advanced quality, FinOps | B3 caching, D3 Guardrails 2.0, D4 eval platform, F5 SOC 2 Type II, G-2 DR game-days, G-3 red-team, G-4 FinOps, G-5 progressive delivery |

**Rule:** a gate opens the next only after its items pass acceptance (implemented + tested/drilled
+ evidence). Hardening gets a protected capacity allocation and cannot be traded for features.

---

## 6. How we prove each gate (evidence)

- **Automated:** the test suite (currently 535) grows with each code item; load/SLO, RBAC-matrix,
  idempotency, dead-letter, breaker, eval-gate, and red-team tests are all executable.
- **Drills (documented, dated):** DR restore, worker-kill recovery, chaos, blue-green deploy,
  on-call incident, token-rotation — each with a runbook and a recorded outcome.
- **Evidence artifacts:** `export_compliance_evidence` snapshots, SBOMs, pentest reports, APM
  dashboards, SLO reports — retained immutably and mapped to SOC 2 controls.

---

## 7. Risks & open decisions

- **DB under fan-out:** parallel fan-out shipped; without B2 (pooling/tuning) high concurrency can
  saturate connections. Decision: land B1 load harness early to size the pool before G3 autoscale.
- **SQLite in dev vs Postgres in prod:** parallel writes serialize on SQLite; ensure all load/
  concurrency testing runs against Postgres. Move dev default to Postgres (cheap, do at G1).
- **Secrets store choice (C2):** Vault vs cloud-native — decide at G1; both satisfy the
  reference-not-material invariant already in the models.
- **Inbound auth (C4):** OIDC vs mTLS for external A2A/MCP consumers — decide at G3; scoped bearer
  tokens are the interim.
- **Build vs buy for APM/eval-judge:** prefer managed APM (E1) and a hosted LLM-judge for D4 to
  avoid undifferentiated ops.

---

## 8. First implementation slice (when we resume)

Recommended order to start (all code, all testable, highest risk-reduction per unit effort):
1. **A1 idempotency keys** → **A2 dead-letter** → **A3 orchestrator breaker** (resilience core).
2. **D1 eval-gate blocking** + **C5 RBAC matrix tests** (governance teeth, small).
3. **B1 load/SLO harness** (sizes everything downstream).
4. **F3 evidence-pipeline scheduling** (cheap SOC 2 progress).

Then move to the infra-heavy G3 items (HA/DR, autoscaling, pentest) with the app-level controls
already proven.
