# Agent Fabric Transformation — Strategy & Roadmap

> **Purpose:** Product-strategy source of truth for evolving the Agentic Platform into a
> **MuleSoft Agent Fabric–class control plane**, while keeping **autonomous agent build** as
> the defensible differentiator. Feeds the executive deck (`AGENTIC_PLATFORM_ROADMAP_DECK.md`)
> and the detailed plan (`PRODUCT_ROADMAP_FY26_FY27.md`).
>
> **Fiscal horizons (RELX convention):** H0 = Jul 2026 · G1 = Aug–Sep 2026 · G2 = Oct–Dec 2026
> · G3 = Jan–Jun 2027 · G4 = Jul–Dec 2027.
>
> **Prepared:** 25 Jul 2026.

---

## 1. Executive recommendation

MuleSoft launched **Agent Fabric** (Sep–Oct 2025) as the enterprise answer to *agent sprawl*:
a control plane to **discover, orchestrate, govern, and observe any AI agent** regardless of
where it was built. It deliberately does **not build agents** — it manages agents other teams
built on Agentforce, Bedrock, Vertex, or Copilot Studio.

Our platform is already strong on the half of Agent Fabric that is hardest to build —
**deep, lifecycle governance** — and we own the one thing Salesforce told the market Fabric
does *not* do: an **autonomous insight → blueprint → sandboxed agent → workflow** build
pipeline.

**Recommendation:** adopt a *"Governed Agent Fabric with an Autonomous Factory"* posture. Close
the four Fabric-parity gaps in a deliberate order — **interop first** — and never surrender the
Factory, which is the wedge no fabric competitor has.

> **Positioning line:** *Every other fabric governs the agents you already have. Ours also
> builds the ones you're missing — sandbox-safe and fully governed.*

---

## 2. What MuleSoft Agent Fabric is

Four modular pillars plus a gateway, built on the Anypoint / Flex Gateway and two open
interop standards (**MCP** for tools, **A2A / JSON-RPC 2.0 agent cards** for agent discovery
and agent-to-agent calls).

| Pillar | Capability | GA |
|---|---|---|
| **Agent Registry** | Central, versioned catalog of every agent, MCP server, and A2A endpoint — public + private, searchable, discoverable by humans *and* agents | Oct 2025 |
| **Agent Scanners** | Auto-discovery: connect Agentforce/Bedrock/Vertex/Copilot once → agents found, their LLM/capabilities/data-access extracted, normalized to A2A agent cards, continuously synced to the Registry | Jan 2026 |
| **Agent Broker** | Intent router (Atlas Reasoning Engine): request in → pick the right agent by domain/context → run the workflow end-to-end | Oct 2025 |
| **Agent Visualizer** | Live map of agent-to-agent interactions with confidence scores, bottleneck detection, hallucination-risk overlays | Oct 2025 |
| **Omni / AI Gateway** | Ingress + egress guard for LLM/agent/MCP traffic: auth, rate limits, prompt protection, PII filters, budget/cost control, model routing, audit | Available |

**Architecture:** five horizontal layers — *Consumers → Ingress Gateway → Agent Fabric (control
plane) → Egress Gateway → Connected Ecosystem (LLMs, agent clouds, tools, APIs, data).*

**What Fabric explicitly does NOT do:** build agents autonomously, replace DevOps/testing, or
provide simulation/guardrail-test frameworks. It is governance-and-orchestration infrastructure
over agents that already exist.

---

## 3. Where we stand today (parity map)

| Fabric capability | Our current state | Verdict |
|---|---|---|
| Deep lifecycle governance | `GovernanceService` single choke point; 3-gate production promotion; immutable `AuditLog`; risk-tier tool gating; audited break-glass | **We exceed** — Fabric governs at the gateway; we govern the whole lifecycle |
| Egress control | SSRF-guarded REST connector, SELECT-only SQL, circuit breakers, per-call audit | **Match** (narrower surface) |
| Prompt protection + PII filters | `guardrails.py` — ~18 injection/PII/exfiltration rules, block/warn levels, audited | **Match** |
| Observability + cost | OTel spans, budget alerts, p50/p95/p99 rollups, SOC2/ISO evidence export | **Match** |
| Multi-tenancy | BU → Division → WorkStream → OrgProcess scoping across all entities | **We exceed** |
| **Autonomous agent build** | `ProcessInsight → scored Blueprint → approval → BuildCompiler → sandbox agent + tool bindings → WorkflowCompiler → DAG` | **Unique — Fabric has nothing here** |
| MCP / A2A interop | None. Registry dual-emits Anthropic/OpenAI schemas only | **Gap** |
| Auto-discovery / scanners | Manual registration only (`Agent.platform` enum anticipates targets) | **Gap** |
| Broker / dynamic agent routing | `model_router` + `delegate_to_agent` seed, no intent→agent broker | **Gap** |
| Federated (external) registry | Internal-only catalog | **Gap** |

**Net:** we are ahead on governance and unique on build; we trail on **interop, discovery,
brokering, and federation** — the connective tissue that makes Fabric a *fabric*.

---

## 4. The four gaps, ranked

1. **Interop standards (MCP + A2A) — highest leverage.** Without MCP we can't consume the
   external tool ecosystem; without A2A agent cards we can't be discovered or called by other
   fabrics (including Fabric itself). Everything else — scanners, broker, federation — depends
   on this. **Lead here.**
2. **Auto-discovery / scanners.** The most visible "Fabric-like" demo. Schema already
   anticipates Bedrock/Vertex/Agentforce/Copilot via `Agent.Platform`; we lack the crawlers.
3. **Broker / dynamic routing.** Intent → domain → agent selection, run through our governance
   path. `delegate_to_agent` + `model_router` are the seed.
4. **Federated registry.** Promote external / MCP / A2A entries to first-class registry
   citizens, discoverable by other agents.

---

## 5. Transformation roadmap

Each phase is **additive evolution of existing code**, not a rewrite. The Factory is threaded
through every phase as the differentiator.

### Phase 1 — Interop foundation *(G1–G2 · Aug–Dec 2026)* — **LEAD**

**Goal:** speak MCP and A2A, both directions, through our existing governance gate.

- **MCP client (inbound tools).** Extend `services/tools/registry.py` to ingest external MCP
  tool schemas alongside the Anthropic/OpenAI emitters. External MCP tools pass through the
  *same* risk-tier + binding + audit gate as native tools. Support curated public MCP registry
  **and** register-by-URL (Fabric parity).
- **MCP server + A2A agent cards (outbound).** Expose our registered agents as **A2A agent
  cards** (JSON-RPC 2.0) so external systems can discover and call them. New `AgentCard`
  projection derived from `Agent` + bindings + guardrail level + capability metadata.
- **Governance invariant:** every inbound MCP tool call and inbound A2A request runs through
  `GovernanceService` / guardrails / `AuditLog` — our differentiated control does not lapse at
  the interop boundary.

**Exit (G2 gate):** an external MCP tool is callable by a governed agent; one of our agents is
discoverable + invocable as an A2A endpoint by a third party; both paths fully audited.

### Phase 2 — Federated Registry *(G3 · Jan–Jun 2027)*

- Promote `Agent.Kind` / `Platform` **external / MCP / A2A** entries to first-class registry
  citizens (not just our own runtime agents).
- Discoverability API: agents (and humans) query the registry by capability/domain.
- Registry becomes a catalog of *any* agent — the Fabric definition.

### Phase 3 — Scanners / auto-discovery *(G3–G4 · 2027)*

- New `services/scanners/` with Bedrock / Vertex / Agentforce / Copilot crawlers that extract
  capability + LLM + data-access and normalize to A2A cards.
- **Reuse the `PackageIngestor` pattern** (validate → normalize → sandbox-only): scanned agents
  land as **governed, non-live** registry entries until explicitly approved. Fabric ingests
  ungoverned; we ingest *governed by default*.

### Phase 4 — Broker + Visualizer *(G4 · Jul–Dec 2027)*

- **Broker:** promote `delegate_to_agent` + `model_router` into an intent → domain → agent
  router. **Differentiator:** every broker hop is gated, guardrailed, and audited — Fabric's
  Broker is not lifecycle-governed.
- **Visualizer:** we already emit OTel spans and (post-Phase 1) A2A graph edges — add the
  front-end interaction map (confidence / bottleneck / hallucination-risk overlays) onto the
  existing `dashboard.html`.

### The Factory wedge — threaded through all phases

As scanners and the registry reveal **gaps** — a process opportunity with no agent that can
serve it — the autonomous pipeline **proposes and builds one**, sandbox-safe and approval-gated.
This closes a loop no competitor has:

> **Discover sprawl → govern it → and manufacture the missing agents.**

---

## 6. Differentiation summary

| Dimension | MuleSoft Agent Fabric | Our platform (target) |
|---|---|---|
| Discover / register / observe agents | ✅ Core | ✅ Parity (Phases 1–4) |
| Govern agents | ✅ Gateway-level | ✅✅ Lifecycle-deep + gateway |
| Interop (MCP / A2A) | ✅ | ✅ (Phase 1) |
| **Build agents autonomously** | ❌ Explicitly out of scope | ✅✅ **Unique moat** |
| Governed-by-default ingestion | ❌ Ingests ungoverned | ✅ Sandbox-first |

**One-line strategy:** reach Fabric parity on the connective tissue (interop → discovery →
broker → federation), and win on the two things Fabric structurally can't match — **lifecycle-deep
governance** and the **autonomous agent factory**.

---

## 7. Risks & open decisions

- **Standards drift.** MCP and A2A are young and evolving; build the interop layer behind an
  adapter so schema changes don't ripple. (Mirrors the existing `adapters/` pattern.)
- **Governance vs. openness tension.** Fabric optimizes for reach; we optimize for control.
  Decision: keep sandbox-by-default even for scanned/federated agents, accepting slower "time
  to catalog" in exchange for zero ungoverned execution — this *is* our positioning.
- **Broker build vs. buy.** Atlas Reasoning Engine is Salesforce-proprietary; our broker should
  lean on the existing `model_router` + governance rather than a new reasoning stack.
- **Scope of scanners.** Prioritize Bedrock + Agentforce (largest enterprise install base)
  before Vertex/Copilot.

---

## 8. Sources

- [Salesforce — MuleSoft Agent Fabric announcement](https://www.salesforce.com/news/stories/mulesoft-agent-fabric-announcement/)
- [Salesforce — Automated Discovery for Any AI Agent or Tool](https://www.salesforce.com/news/stories/mulesoft-agent-fabric-automated-agent-discovery/)
- [MuleSoft — Agent Fabric product page](https://www.mulesoft.com/ai/agent-fabric)
- [MuleSoft — Introducing A2A Support](https://www.mulesoft.com/platform/ai/a2a-support)
- [MuleSoft Blog — Private Agentic AI with MCP and A2A support in PCE](https://blogs.mulesoft.com/dev-guides/private-agentic-ai-with-mcp-and-a2a-support-in-mulesoft-pce/)
- [Salesforce Architect — MuleSoft Agent Fabric Deep Dive](https://architect.salesforce.com/docs/architect/fundamentals/guide/mulesoft-agent-fabric-deep-dive.html)
- [SalesforceDevops.net — Turning AI Agents Into Enterprise Infrastructure](https://salesforcedevops.net/index.php/2025/09/25/mulesoft-agent-fabric/)
- [InfoWorld — Agent Fabric adds new ways to keep AI agents in line](https://www.infoworld.com/article/4159228/mulesoft-agent-fabric-adds-new-ways-to-keep-ai-agents-in-line.html)
- [CIO — MuleSoft launches Agent Fabric to tackle agent sprawl](https://www.cio.com/article/4063090/mulesoft-launches-agent-fabric-to-tackle-agent-sprawl-and-unify-enterprise-ai-workflows.html)
