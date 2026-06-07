# Implementation Roadmap — RELX Agentic AI Platform

> **Last updated:** 2026-06-07  
> **Baseline maturity:** ~33% of target enterprise architecture  
> **Framework reference:** Bain & Company three-layer enterprise agentic AI architecture

---

## Architecture Maturity Snapshot

```
Layer 1 – Orchestration & Reasoning   ██░░░░░░░░  15%
Layer 2 – Data & Knowledge            █░░░░░░░░░  10%
Layer 3 – Observability & Governance  ████████░░  75%
──────────────────────────────────────────────────────
Platform overall                      ███░░░░░░░  ~33%
```

The platform is **governance-first by design** — the correct posture for enterprise AI programs
where policy failure, not capability failure, is the primary risk. The control plane (Layer 3)
is the hardened foundation that all subsequent layers build on.

---

## What Is Already Built (Phase A — Complete)

### Layer 3 — Observability & Governance ✅

| Capability | Implementation |
|---|---|
| Agent lifecycle state machine | `Agent.Status` (5 states) + `ALLOWED_TRANSITIONS` + `GovernanceService` |
| Audit trail | `AuditLog` model — append-only, actor/IP/timestamp on every privileged action |
| Approval workflows | `Approval` model — server-side, expiring, single-use tokens |
| Governance reviews | `GovernanceReview` model — approve/reject with written justification |
| Token + cost tracking | `AgentRun.input_tokens`, `output_tokens`, `cost_usd` stored at run completion |
| RBAC (partial) | Django groups — `agent_approver`, `platform_admin` roles enforced |
| Agent registry UI | Self-serve "Register agent" modal with cascading org selectors |
| Admin panel | Locked governance fields, force-transition actions, colored status display |
| Monitoring dashboard | Cross-agent run metrics, token usage, latency, cost charts |
| Deployment command center | Live agent view with status, purpose, platform, chat interface |
| Multi-adapter runtime | Anthropic, OpenAI, Azure OpenAI, AWS Bedrock, HTTP API adapters |

---

## Roadmap

---

### Phase B — Governance Hardening
**Target:** Q3 2026 | **Effort:** ~4 weeks

*Closes the remaining gaps in the governance layer before scaling agent count.*

#### B1 — Tenant Row-Level Scoping (1 week)
**Gap:** Any authenticated user can register agents in any business unit.

- Add `UserProfile` model linking `User → OrgUnit`
- Enforce `business_unit` filter on `GovernanceService.register_agent()` — builder role sees only own BU
- Add middleware to scope all `Agent` querysets by `request.user.profile.org_unit`
- Wire `_require_role(actor, {"agent_builder", "platform_admin"})` in `register_agent` API

**Files:** `controlplane/models.py`, `controlplane/services/governance.py`, `controlplane/api/views.py`

#### B2 — Prompt Injection & Content Guardrails (1.5 weeks)
**Gap:** No content filter between user input and agent system prompt.

- Add `GuardrailService` — pre-run content scanner for prompt injection patterns
- Detect common attack patterns: role overrides, jailbreak templates, PII leakage
- Log `GUARDRAIL_BLOCK` events to `AuditLog` with redacted payload
- Per-agent configurable guardrail level (off / warn / block) stored on `Agent` model
- Hook into `PlatformAgentRuntime.stream()` before message dispatch

**Files:** `controlplane/services/guardrails.py` (new), `controlplane/runtime/agent_runtime.py`

#### B3 — EvalRun Gate (1 week)
**Gap:** Production promotion checks GovernanceReview + Approval but not eval pass.

- Add `EvalSuite` and `EvalRun` models (suite → set of test cases, run → scored execution)
- Extend `GovernanceService._check_production_gates()` to require passing `EvalRun`
- Add eval management UI to `/manage/` panel
- Wire eval submission to existing agent run infrastructure

**Files:** `controlplane/models.py`, `controlplane/services/governance.py`, `controlplane/templates/controlplane/manage.html`

#### B4 — Behavioral Drift Alerting (0.5 weeks)
**Gap:** Feedback ratings collected but no alerting when quality degrades.

- Define quality baseline per agent (rolling 7-day average score)
- Flag `QUALITY_DRIFT` when score drops >20% below baseline
- Surface drift alerts in Monitoring dashboard as warning chips
- Add `Agent.quality_alert` boolean field, set by nightly management command

**Files:** `controlplane/management/commands/compute_baselines.py` (new), `controlplane/models.py`

---

### Phase C — Data & Knowledge Layer
**Target:** Q4 2026 | **Effort:** ~6 weeks

*Gives agents access to governed enterprise knowledge without bespoke per-agent retrieval code.*

#### C1 — Vector Search with pgvector (2 weeks)
**Gap:** Registry search is keyword-only; agents have no semantic retrieval.

- Enable `pgvector` extension on Postgres (one migration)
- Add `AgentEmbedding` model — stores OpenAI `text-embedding-3-small` vectors for agent metadata
- Replace keyword `registry_search` tool with cosine-similarity search
- Background task to re-embed on agent metadata changes
- Expose `/api/v1/agents/search/?q=` semantic endpoint

**Files:** `controlplane/models.py`, `controlplane/services/embeddings.py` (new), `controlplane/api/views.py`

#### C2 — Document Knowledge Base & RAG Pipeline (3 weeks)
**Gap:** No document store; agents cannot retrieve over enterprise content.

- Add `KnowledgeDocument` model — chunked text, source URL, owner BU, embedding vector
- Build ingestion pipeline: PDF/DOCX → chunk → embed → store
- Add `retrieve_knowledge` tool available to all platform agents
- Govern document access by `business_unit` (tenant-scoped retrieval)
- Admin UI for document upload and re-indexing

**Files:** `controlplane/models.py`, `controlplane/services/rag.py` (new), `controlplane/runtime/registry_tools.py`

#### C3 — Structured Data Connectors (1 week)
**Gap:** Only generic HTTP adapter; no standard connectors for common enterprise targets.

- Define `DataConnector` model — type (SQL / REST / GraphQL), connection config, owner BU
- Add connector registry to agent configuration
- Implement `SqlConnector` (SQLAlchemy-backed), `RestConnector` (typed schema), `GraphQLConnector`
- Audit every connector call to `AuditLog`

**Files:** `controlplane/services/connectors/` (new package)

---

### Phase D — Observability & Telemetry
**Target:** Q1 2027 | **Effort:** ~3 weeks

*Full OpenTelemetry instrumentation for production-grade observability.*

#### D1 — OpenTelemetry Traces (2 weeks)
**Gap:** Runs are logged but not traced end-to-end with spans.

- Instrument `PlatformAgentRuntime` with OTel spans (tool call, LLM call, guardrail check)
- Export traces to Grafana Tempo or Datadog
- Add trace ID to `AgentRun` model for cross-link from UI
- Latency percentile charts (p50/p95/p99) in Monitoring dashboard

**Files:** `controlplane/runtime/agent_runtime.py`, `agentic_platform/settings.py`

#### D2 — Cost & Budget Alerts (1 week)
**Gap:** Cost tracked per run but no budget threshold enforcement.

- Add `AgentBudget` model — daily/monthly token + cost limits per agent
- Enforce budget in `PlatformAgentRuntime` — hard stop or soft warn at threshold
- Alert surface in Monitoring dashboard and admin panel

**Files:** `controlplane/models.py`, `controlplane/runtime/agent_runtime.py`

---

### Phase E — Orchestration & Multi-Agent
**Target:** Q2 2027 | **Effort:** ~8 weeks

*Enables complex multi-step workflows and agent-to-agent collaboration.*

#### E1 — Task DAG & Planning Engine (3 weeks)
**Gap:** No task decomposition; agents execute linearly.

- Add `Task` and `TaskDAG` models — directed acyclic graph of subtasks
- Implement `PlanningAgent` — given high-level goal, produces executable DAG
- `TaskRunner` service — traverses DAG, dispatches subtasks, handles retries and failures
- DAG visualisation in Monitoring dashboard

**Files:** `controlplane/models.py`, `controlplane/services/planner.py` (new), `controlplane/runtime/task_runner.py` (new)

#### E2 — Agent-to-Agent Communication (2 weeks)
**Gap:** No inter-agent delegation or communication.

- Add `delegate_to_agent` tool — lets a running agent invoke another registered agent
- Delegate calls are governed (target agent must be in production, same BU)
- Full audit trail: delegating agent, target agent, payload, response
- Recursion depth limit enforced (max 3 hops)

**Files:** `controlplane/runtime/registry_tools.py`, `controlplane/services/governance.py`

#### E3 — Persistent Cross-Agent Memory (2 weeks)
**Gap:** `ConversationSession` is per-agent; no shared working memory across agents.

- Add `WorkingMemory` model — keyed store scoped to a `TaskDAG` execution
- Agents read/write to shared memory store during a task run
- Memory entries expire after DAG completion (configurable retention)
- Governed: only agents within the same BU / task scope can access

**Files:** `controlplane/models.py`, `controlplane/services/memory.py` (new)

#### E4 — Dynamic Model Routing (1 week)
**Gap:** Each agent uses a single fixed model; no per-task cost/quality optimisation.

- Add `ModelRouter` service — selects model based on task complexity, cost budget, latency SLA
- Routing rules configurable per agent: `fast` (Haiku) / `balanced` (Sonnet) / `deep` (Opus)
- Router decision logged to `AuditLog`

**Files:** `controlplane/services/model_router.py` (new), `controlplane/runtime/agent_runtime.py`

---

## Summary Timeline

```
2026 Q3   ████████████  Phase B — Governance Hardening
2026 Q4   ██████████████████  Phase C — Data & Knowledge Layer
2027 Q1   ████████  Phase D — Observability & Telemetry
2027 Q2   ████████████████  Phase E — Orchestration & Multi-Agent
```

| Phase | Focus | Weeks | Cumulative Maturity |
|---|---|---|---|
| A (done) | Governance core | — | ~33% |
| B | Governance hardening | 4 | ~50% |
| C | Data & knowledge | 6 | ~65% |
| D | Telemetry | 3 | ~75% |
| E | Orchestration | 8 | ~95% |

---

## Principles

1. **Governance before capability** — no agent capability ships without a corresponding governance gate.
2. **Control plane stays separate** — the platform governs agents; it does not become an agent execution environment unless explicitly scoped.
3. **Tenant isolation is non-negotiable** — row-level scoping (Phase B1) is a prerequisite for any multi-BU rollout.
4. **Audit everything** — every Phase B–E feature must write to `AuditLog` on every privileged or data-access operation.
5. **No breaking changes to the state machine** — `ALLOWED_TRANSITIONS` is the contract; additive changes only.
