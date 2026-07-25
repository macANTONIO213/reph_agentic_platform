# Phase 4 Technical Spec — Broker + Visualizer

> **Scope:** Phase 4 of [AGENT_FABRIC_TRANSFORMATION_STRATEGY.md](AGENT_FABRIC_TRANSFORMATION_STRATEGY.md).
> An intent→agent **Broker** that routes a request to the best agent in the federated registry
> and runs it through the governed runtime, plus **Visualizer** graph data (agent interaction
> map). MuleSoft's Agent Broker + Agent Visualizer pillars — differentiated by governing every
> hop. Builds on Phase 2 registry + Phase 0 `AsyncAgentTask`. **Prepared:** 25 Jul 2026.

---

## 1. Design principles

1. **Every broker hop is governed.** Selection queries the federated registry (approved entries
   only); execution goes through `agent_tasks.submit` → `PlatformAgentRuntime`, so guardrails,
   telemetry, and audit apply. Fabric's Broker routes; ours routes *and stays governed*.
2. **Deterministic selection first.** Step 1 uses transparent keyword/domain/capability scoring
   over the registry — explainable and testable — reusing `model_router`'s spirit but for agent
   choice. An LLM ranker can layer on later.
3. **Only executable, approved agents run.** The broker executes a chosen **first-party** agent
   (which has a governed runtime); external A2A candidates are returned as routing suggestions
   (executing them = an outbound A2A call, a step-2 extension).

## 2. Components (step 1 — Broker)

- `services/interop/broker.py` — `select_candidates(intent, domain)` (scored ranking over
  `RegistryEntry`), `route(...)` (decision + candidates, no execution), `route_and_execute(...)`
  (runs the best first-party agent through the runtime; audited).
- API: `POST /api/v1/broker/route` (any authenticated) and `POST /api/v1/broker/execute`
  (agent_builder+; triggers a governed run). Every decision audited (`broker_route`).

## 3. Components (step 2 — Visualizer)

- Graph-data endpoint built from OTel spans + broker/A2A/delegation edges: nodes = agents /
  registry entries, edges = hops, with metrics (calls, latency, success). Dashboard surface.

## 4. Governance invariants

- The broker only considers `review_status=approved`, `is_active` registry entries.
- Execution never bypasses the runtime; the broker view has no adapter access.
- A route that finds no executable first-party agent returns a decision, not a silent failure.

## 5. Steps

1. Broker: select / route / route-and-execute + API. *(this increment)*
2. Visualizer graph-data endpoint + dashboard surface; optional LLM/A2A-outbound routing.
