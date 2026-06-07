# REPH Agentic Platform — Control-Plane-First Architecture

**Audience:** Claude Code (implementation agent) + platform engineering reviewers.
**Status:** Architecture brief. Supersedes the runtime-first draft. Builds on `REVERSE_ENGINEERING_ANALYSIS.md` and `MONITORING_DASHBOARD_PLAN.md`.

**Decision (locked):** The platform is a **control plane**, not a compute substrate. Agents — custom and external — run on whatever infrastructure already exists (k8s, serverless, Azure AI Foundry, Bedrock, vendor). The platform makes them **safe, governed, and observable** through a registry, governance workflow, a set of gateways, an SDK, and unified observability. A managed execution tier (running agent code in our own sandboxes) is explicitly **deferred** to an optional future phase.

**Earlier decisions, reinterpreted under this posture:**
- *Containerized execution by workers* → deferred (becomes the optional managed-execution tier). Async workers/queue are still used, but for proxying, telemetry ingestion, evals, and scheduled jobs — **not** for hosting agent code.
- *Framework-agnostic contract* → kept and central: a manifest + a thin **Platform Agent SDK** that custom agents embed wherever they run.
- *External = registry + optional proxy* → kept; now the **same integration model** applies to custom agents too.
- *Async workers + queue (Celery/Redis/Postgres)* → kept, scoped to control-plane jobs.

---

## 1. Principle

> Own the control points, not the compute.

The defensible, high-value surface is the set of **choke points every agent must pass through** — model access, tool access, secrets, governance, and telemetry. If the platform owns those, it governs and observes every agent regardless of where the process actually runs. Owning the compute substrate is the most expensive, least differentiated part and duplicates infrastructure the org already operates, so we delegate it.

### The platform IS
Registry / system of record · governance & approval workflow · model gateway · tool gateway · secrets broker · agent SDK · evaluation harness · unified observability & cost · audit.

### The platform IS NOT (for now)
A container orchestrator · a build/CI system for agent images · a sandbox runtime · the host for arbitrary first-party code.

---

## 2. Two agent classes — both run elsewhere, both governed here

Under control-plane-first the executable/registered distinction collapses into **how the platform integrates**, not **where code runs**. Both run outside the platform.

| | **Custom (first-party)** | **External (Foundry / Bedrock / Copilot / vendor)** |
|---|---|---|
| Built by | Internal teams | External platform |
| Runs on | Existing internal compute (k8s/serverless/etc.) | The external platform |
| Integration | **SDK/callback** (preferred) and/or **proxy** | **Proxy** and/or **attestation-only** |
| Platform governs | Manifest, model/tool/secret access, evals, lifecycle, telemetry | Manifest, risk, lineage, attestation, proxied telemetry |
| Platform hosts code | No | No |

`Agent.kind ∈ {custom, external}` still exists, but it now selects an **integration profile**, not an execution engine.

---

## 3. Integration modes (how agents connect to the control plane)

Three modes, in descending order of governance fidelity:

1. **SDK / callback (custom agents, preferred).** The agent embeds the **Platform Agent SDK** and runs wherever the team deploys it. All model calls, tool calls, and secret access go *out through the platform gateways*, and run events stream back to the platform. The platform never hosts the process but sees and governs every privileged action. Highest fidelity, requires the team to adopt the SDK.

2. **Proxy / endpoint.** The agent exposes an HTTP endpoint; the platform dispatches runs *through* a proxy worker that calls the endpoint and records telemetry. Works for custom agents that can't adopt the SDK and for external platforms (Foundry/Bedrock/Copilot/HTTP — the current adapters become proxy clients). Medium fidelity: the platform sees inputs/outputs and cost-at-the-boundary but not the agent's internal tool calls unless the agent also reports them.

3. **Attestation-only (external).** Pure registry: the owning team certifies controls; periodic re-attestation. No traffic flows through the platform. Lowest fidelity, zero integration cost — the fallback for agents that can't be proxied.

> Governance level is a property of the integration mode and is shown in the catalog, so risk owners can see which agents are fully governed vs. attested-only.

---

## 4. The gateways — the actual product

These are the choke points that make the control plane worth having. They run in the control plane (or as thin sidecars) and are reachable by agents in any environment over authenticated APIs.

- **Model Gateway.** All LLM calls route here. Holds provider keys (agents never see them), enforces the **`model_id` allowlist**, applies **per-agent/per-tenant cost and rate ceilings**, records **accurate token usage and cost** (fixes analysis bug #6 at the source), and can enforce input/output policies (PII redaction, content filters). One integration point for Anthropic/OpenAI/Azure/Bedrock.
- **Tool Gateway.** All tool/MCP calls route here. Enforces the **per-agent tool grants** from the manifest (an ungranted tool is denied), brokers built-in tools (`registry_search`, etc.) and approved **MCP servers**, and logs every call as an `AgentToolCall`. This is also where data-source access policy lives.
- **Secrets Broker.** Resolves manifest secret *references* to **short-lived, scoped** credentials from a secrets manager (Vault / Azure Key Vault / AWS Secrets Manager). Agents request by name; raw long-lived keys never enter agent environments.

The SDK wraps all three so a custom agent looks like:
```python
from reph_agent_sdk import agent, RunContext

@agent(manifest="agent.yaml")
def handle(ctx: RunContext):
    hits = ctx.tools.registry_search(query=ctx.input["message"])   # → Tool Gateway (grant-checked, logged)
    reply = ctx.model.complete(system=..., messages=...)           # → Model Gateway (keys, allowlist, cost)
    ctx.emit_text(reply.text)                                      # → streamed to platform as telemetry
    return {"answer": reply.text}
```
The SDK handles auth to the gateways, event streaming (the same `status/token/tool_call/tool_result/usage/done/error` protocol as today's `RuntimeEvent`), and usage accounting — so adoption cost for teams is low.

---

## 5. Run & telemetry flow (SDK mode)

```
Team's infra (anywhere)                    Control plane
┌───────────────────┐                      ┌───────────────────────────────┐
│ custom agent       │  model.complete ───►│ Model Gateway (keys, cost)     │
│  + Platform SDK    │  tools.x ──────────►│ Tool Gateway (grants, MCP)     │
│                    │  secrets.get ──────►│ Secrets Broker (short-lived)   │
│                    │  events  ──────────►│ Telemetry ingest → AgentRun,   │
└───────────────────┘                      │   ToolCall, TelemetryEvent     │
                                           └───────────────────────────────┘
                                                       │
                                                  Dashboard / audit / cost
```
Proxy mode is the same picture except a **proxy worker** in the control plane sits between the requester and the agent's endpoint, and emits the events. The web tier bridges live events to the browser over SSE exactly as today.

This keeps the orchestration layer **lightweight** — dispatch + proxy + the existing reasoning loop, plus a queue (Celery/Redis) for proxy calls, telemetry ingestion, evals, and scheduled attestation/rollup jobs. No sandbox lifecycle to operate.

---

## 6. Domain model (lighter than runtime-first)

Refactor the monolithic `Agent`; **drop** image/build/SBOM models (not needed until a managed execution tier exists). Keep:

- **`Agent`** (registry core): `kind`, integration_mode, ownership, org hierarchy, business_unit, risk tier, status, current_version, governance_level, telemetry flags.
- **`AgentVersion`** (extended): immutable manifest snapshot (JSON), `model_id`, tool grants, secret refs, eval status. (Snapshot the full spec incl. `model_id` — fixes analysis bug #5.)
- **`Endpoint`**: for proxy/external — URL, auth secret ref, `proxy_enabled`, health/attestation status, `last_attested_at`.
- **`Capability` / `ToolGrant`**: tool/MCP catalog + which agents/versions are granted which scopes (enforced by the Tool Gateway).
- **`SecretBinding`**: agent ↔ secret-path references (no values), scope + rotation metadata.
- **`AgentRun`** (extended): integration_mode, stored `cost_usd` (real field), tokens, latency, error class + server-side error id (not raw text), content per `STORE_RUN_CONTENT` policy. May be created by SDK ingest or proxy worker.
- **`AgentToolCall`, `TelemetryEvent`, `AgentFeedback`, `ConversationSession`**: retained; sessions become user-scoped (fixes analysis bug #2); feedback ownership-checked (bug #3).
- **`EvalSuite` / `EvalRun` / `EvalResult`**: gate promotion.
- **`Approval`**: server-side approval records (approver identity, scope, expiry) replacing the client-trusted `human_approved` flag (fixes analysis bug #1).
- **`GovernanceReview`** (retained): now actually gates promotion (fixes analysis bug #4).
- **`AuditLog`**: append-only record of every privileged action.
- **Tenancy/RBAC**: roles (viewer / builder / approver / platform-admin) + org-scoped row-level access, using the existing BU→Division→WorkStream→Process tree as the tenancy spine.

Migrations additive; data-migrate existing agents to `external` except the advisor → `custom` (SDK mode).

---

## 7. External agents — registry + proxy + attestation

Unchanged in spirit: catalog with owner, platform, endpoint ref, risk, data sources, lineage, and **attestation** (certify controls, periodic re-attestation with expiry). When `proxy_enabled`, runs flow through a proxy worker for unified telemetry/cost/audit; the existing Foundry/Bedrock/Copilot/HTTP adapters are repositioned as **proxy clients** behind the Model Gateway. Expired attestation flags the agent and can disable the proxy.

---

## 8. Governance, evals & lifecycle

Lifecycle (both kinds): `draft → registered → evaluated → reviewed/approved → pilot → production → retired`, with rollback to any prior immutable `AgentVersion`.

- **Eval gate:** a version must pass its `EvalSuite` (golden inputs + assertions + safety/red-team probes + cost/latency budgets) before promotion. Evals run as control-plane jobs that drive the agent via its integration mode.
- **Approval + review gate:** promotion to production requires a passing eval **and** an approved `GovernanceReview` **and** a valid `Approval` by an authorized approver. No exceptions in code.
- **Tier-4 execution:** requires a current server-side `Approval` — checked at the Model/Tool Gateway, so it holds regardless of where the agent runs.

---

## 9. Observability (feeds the dashboard plan)

Because all model/tool calls and run events pass through gateways/SDK, the four dashboard metric families (usage, execution health, cost/tokens, quality) are computed from the same tables as `MONITORING_DASHBOARD_PLAN.md`, now tagged by `kind`, `integration_mode`, `governance_level`, and tenant. Adopt **OpenTelemetry** (run = trace, tool/gateway calls = spans) and export to your collector. Cost is correct because the Model Gateway records real usage + pricing.

---

## 10. Infrastructure

- **Control plane:** Django API + admin + dashboard, **Postgres**, **Redis + Celery** (proxy calls, telemetry ingestion, evals, scheduled attestation/rollups), object storage for transcripts/eval artifacts, a secrets manager.
- **Gateways:** part of the control plane initially; can be split into independently scaled services later.
- **No** sandbox runtime, build pipeline, or image registry until/unless the managed execution tier is pursued.
- **Scale path:** split gateways into their own services, managed Postgres/Redis, multi-region object storage. (Kubernetes for the gateways/workers if needed — for *control-plane* scaling, not agent hosting.)

---

## 11. Deferred: optional managed execution tier (future)

If, later, teams without compute need a place to *run* agents, add a managed execution tier: containerized sandboxes, build/scan/sign/SBOM pipeline, per-run isolation, worker pool. The control plane is designed so this slots in as **a fourth integration mode** ("hosted") without reworking the registry, gateways, governance, or SDK. Don't build it now.

---

## 12. API surface (under `/api/v1/`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/agents` | Register agent (custom or external) from manifest |
| POST | `/agents/<id>/versions` | New immutable version (manifest snapshot) |
| POST | `/agents/<id>/versions/<v>/evals` | Trigger eval suite |
| POST | `/agents/<id>/approvals` | Create server-side approval (approver role) |
| POST | `/agents/<id>/versions/<v>/promote` | Promote (gated by eval + review + approval) |
| POST | `/gateway/model/complete` | Model Gateway (SDK/agent → provider) |
| POST | `/gateway/tools/<tool>` | Tool Gateway (grant-checked tool call) |
| POST | `/gateway/secrets/resolve` | Secrets Broker (short-lived creds) |
| POST | `/agents/<id>/runs` | Start a run (proxy mode) → run id + stream URL |
| POST | `/runs/ingest` | Telemetry ingest (SDK mode) |
| GET | `/runs/<id>/events` | SSE/WebSocket live stream |
| POST | `/agents/<id>/attestations` | External attestation / re-attestation |
| (reads) | per `MONITORING_DASHBOARD_PLAN.md` §3 | Catalog + monitoring |

---

## 13. Phased roadmap (control-plane-first)

**Phase A — Registry & governance core (close the worst gaps).** `kind`/integration_mode model split + data migration; RBAC/tenancy; `AuditLog`; **server-side `Approval`** (kills the forged-flag bypass); enforce review+approval on promotion; user-scope sessions & feedback. Existing adapters become proxy clients. *Acceptance:* catalog distinguishes kinds & governance level; Tier-4 needs a real approval; promotion is gated; cross-user session/feedback access denied.

**Phase B — Gateways + SDK.** Model Gateway (keys, allowlist, cost/rate), Tool Gateway (grants, MCP), Secrets Broker; the Platform Agent SDK + run-event protocol. Migrate the Deployment Advisor to a **custom SDK agent** calling the gateways. *Acceptance:* an SDK agent runs on separate infra, all model/tool/secret access flows through gateways, accurate cost/telemetry recorded, keys never leave the gateway.

**Phase C — Proxy + external + evals.** Proxy workers for endpoint/external agents with unified telemetry; attestation lifecycle; `EvalSuite`/eval gate; canary + rollback. *Acceptance:* external runs appear with correct cost/latency; promotion blocked without passing eval; rollback works.

**Phase D — Observability + dashboard.** OTel wiring; ship the Catalog + Monitoring dashboard over real data across both kinds and all integration modes. *Acceptance:* dashboard shows usage/health/cost/quality segmented by kind, mode, governance level, tenant.

**Phase E — Scale / (optional) managed execution tier.** Split gateways into services; managed datastores. Only build the hosted execution tier if a real "teams have no compute" need emerges.

> Sequence: **A → B → (C ∥ D) → E**. Phase A alone removes the most dangerous governance defects.

---

## 14. Open decisions to confirm
1. **SDK transport** to gateways: REST per call vs a persistent gRPC/streaming channel (affects latency for chatty tool loops).
2. **Secrets manager**: Vault vs Azure Key Vault vs AWS Secrets Manager (follow existing cloud).
3. **MCP scope**: which internal tools become MCP servers vs built-in platform tools behind the Tool Gateway.
4. **Tenancy granularity**: tenant = Business Unit, or a Tenant entity above the org tree.
5. **Eval depth at launch**: assertion-based only, or LLM-graded + red-team from day one.
6. **SDK languages**: Python only at launch, or also Node/.NET for teams on those stacks.

Tell me which to pin down and I'll fold them in. I can also draft the **Phase A ticket breakdown** (model split + migration + RBAC + server-side approvals + governance gating) as the first hand-off to Claude Code.
