# Agentic Platform — Current Capabilities & Production Roadmap (FY26–FY27)

> **Purpose:** Slide-by-slide deck spec for Cowork/PowerPoint. Mirrors the structure and
> executive arc of the *Process Intelligence — Current Capabilities to Production Roadmap*
> deck, but for the **Agentic Platform**. 23 slides, 16:9.
> **Source of truth for content:** `PRODUCT_ROADMAP_FY26_FY27.md` (this deck is the executive
> compression of it).

---

## Design system (apply to every slide)

| Token | Value | Use |
|---|---|---|
| **Accent** | `#FF8200` (orange) | Eyebrows, numbers, gate tags, key rules, title-slide bar |
| **Ink** | `#262626` | Titles and primary text |
| **Body grey** | `#666666` | Body copy, item lists |
| **Deep grey** | `#3F3F3F` | Sub-headings |
| **Hairline / fill** | `#E8E8E8` | Card fills, table lines, dividers |
| **Paper** | `#FFFFFF` | Slide background (light throughout) |
| **Font** | **Open Sans** (all weights) | Everything |

**Layout rules (match the reference):** light/white background on every slide; a small
**orange ALL-CAPS eyebrow** top-left; a large ink title beneath it; content in clean cards,
numbered rows, or thin-lined tables; one **orange takeaway line** at the bottom of each content
slide. No decorative color bars or underlines. Generous margins (≥0.5").

**Fiscal calendar:** FY = calendar year (RELX convention). Horizons: **H0 = Jul 2026**,
**G1 = Aug–Sep 2026**, **G2 = Oct–Dec 2026**, **G3 = Jan–Jun 2027**, **G4 = Jul–Dec 2027**.

---

## Slide 1 — Title

- **Kicker (orange, caps):** AGENTIC PLATFORM
- **Title:** Current capabilities **and production roadmap**
- **Subtitle:** What is already working, and how we strategically move to enterprise-scale production — FY26–FY27
- **Footer:** Platform Product · Senior Leadership Team · 10 July 2026
- *Design:* dark or white hero; orange accent element; large title, quiet subtitle.

---

## Slide 2 — Executive recommendation

- **Eyebrow:** EXECUTIVE RECOMMENDATION
- **Title:** The control plane is built — now we harden it for scaled production
- **Thesis:** The Agentic Platform already governs, runs, observes and *autonomously builds* enterprise AI agents end to end. The strategy is to preserve that value while replacing prototype infrastructure, proving reliability and compliance, then scaling adoption and connections.
- **Three horizon cards:**
  - **FY26 · Make it production-grade** — Durable execution, enterprise identity, secrets management, runtime-enforced quality gates, staged zero-downtime delivery.
  - **FY27 H1 · Connect intelligence to action** — Live model routing, MCP + SaaS connectors, human-in-the-loop, HA/DR, self-service builder, advanced RAG.
  - **FY27 H2 · Scale and certify** — SOC 2 Type II, Guardrails 2.0, marketplace, new channels, bi-directional platform sync, realised-ROI reporting.
- **Ask (orange):** Approve the FY26 hardening plan now; hold FY27 expansion funding behind the *repeatable-production* gate (G2).

---

## Slide 3 — What we have today

- **Eyebrow:** WHAT WE HAVE TODAY
- **Title:** The platform already covers the full insight-to-governed-agent chain
- **Five numbered steps (cards):**
  1. **Register** — Agents onboarded into a 4-level tenant hierarchy with risk tiers (1–4) and RBAC.
  2. **Govern** — Draft→Review→Pilot→Production→Retired lifecycle; approval tokens; immutable audit log; guardrails.
  3. **Run** — Unified multi-platform runtime (Claude / Azure OpenAI / Bedrock / HTTP) + workflow DAG orchestration.
  4. **Observe** — OpenTelemetry spans, Prometheus metrics, per-agent budget & quality-drift alerts, cost pricing.
  5. **Build (Factory)** — Process insight → scored blueprint → approval → runnable sandbox agent with governed tool bindings.
- **Takeaway (orange):** The roadmap productionises an existing, working control plane — it does not restart product discovery.

---

## Slide 4 — Current capability 1: Governance & multi-tenancy

- **Eyebrow:** CURRENT CAPABILITY 1
- **Title:** Every agent action already flows through a governed control plane
- **Left card — "Governance & isolation":**
  - Lifecycle state machine with blocking governance review
  - Single-use Tier-4 approval tokens; immutable append-only audit log
  - Per-agent risk tiers (1–4)
  - 4-level tenant model (BusinessUnit → Division → WorkStream → OrgProcess)
  - Role-based access: viewer / builder / approver / admin
- **Right callout — "Leadership value":** One auditable, tenant-isolated system of record for enterprise AI — not ungoverned point solutions.

---

## Slide 5 — Current capability 2: Runtime & orchestration

- **Eyebrow:** CURRENT CAPABILITY 2
- **Title:** The platform already runs and orchestrates agents across providers
- **Two columns:**
  - **Multi-platform runtime:** Unified `PlatformAgentRuntime` (SSE streaming) · adapters for Anthropic Claude, Azure OpenAI, AWS Bedrock, generic HTTP APIs · per-run cost pricing & telemetry.
  - **Multi-agent orchestration:** Workflow DAG engine · dependency resolution · cross-agent shared memory · retries/timeouts · blueprint→workflow compiler.
- **Takeaway (orange):** Agents are provider-portable today — one governance and runtime layer in front of any model.

---

## Slide 6 — Current capability 3: Safety, evaluation & observability

- **Eyebrow:** CURRENT CAPABILITY 3
- **Title:** Trust and observability are designed into the product
- **"Trust controls" list:**
  - Pre-LLM guardrails: prompt-injection, PII and system-prompt-override scanning (off/warn/block)
  - Evaluation suites/cases/runs with pass thresholds and a promotion gate (modelled)
  - OpenTelemetry spans + Prometheus `/metrics/`
  - Per-agent budget alerts and quality-drift (satisfaction-baseline) alerting
  - Semantic agent search, RAG pipeline (PDF/DOCX/TXT/MD), SQL/REST/GraphQL connectors
- **Takeaway (orange):** A defensible starting point for production governance — not a black-box prototype.

---

## Slide 7 — Current capability 4: Agent Factory

- **Eyebrow:** CURRENT CAPABILITY 4
- **Title:** The Agent Factory already creates a governed, autonomous build
- **"Factory package / lifecycle" list:**
  - Process-insight ingestion + opportunity scoring
  - Blueprint generation → approval gate → build compiler
  - Tool-binding lifecycle: **proposed → sandbox → live**
  - Canonical package handoff with an authoritative safety boundary
- **Important boundary (callout):** A factory package can only ever produce a **draft/sandbox** agent with **proposed** bindings; production and live binding require explicit human/policy approval — enforced at ingestion and non-overridable.

---

## Slide 8 — The production gap

- **Eyebrow:** THE PRODUCTION GAP
- **Title:** A proven control plane still needs a production-grade runtime
- **Two columns (side by side):**
  - **Current controlled pilot:**
    - Sequential DAG; single-process command-driven worker
    - Eval gate modelled but **not runtime-enforced**
    - Model router is a **stub** (no live dispatch)
    - Env-var secrets; basic connector URL validation
    - No scheduled/event triggers; no workflow versioning
    - Single service; no staging; retry-only resilience
  - **Required production state:**
    - Durable queue + parallel fan-out + autoscaled workers
    - Eval gate enforced at promotion; canary/A-B + auto-rollback
    - Live cost/latency/quality model router
    - Managed secrets vault + rotation; SSRF/egress hardening
    - Scheduled & event/webhook triggers; workflow versioning
    - Staging + blue/green; circuit breaker/dead-letter/idempotency; HA/DR
- **Takeaway (orange):** Preserve the proven product layer; systematically replace the prototype operating layer.

---

## Slide 9 — Target platform architecture (logical)

- **Eyebrow:** TARGET PLATFORM ARCHITECTURE
- **Title:** A governed control plane in front of every agent action
- **Personas band (top):** AGENT BUILDERS · APPROVERS · PLATFORM ADMINS · SECURITY / RISK · BUSINESS OWNERS
- **Stacked layers (each a horizontal band):**
  - **Experience** — Dashboard · self-service builder · HITL task inbox · ROI/chargeback analytics · marketplace · web/API + Teams/Slack/voice
  - **Governance & safety** — Lifecycle + runtime eval gate · SSO/SAML/OIDC + SCIM · fine-grained RBAC · Guardrails 2.0 · immutable audit · SOC 2 controls
  - **Agent Factory** — Continuous insight ingestion · telemetry→blueprint feedback · portfolio/ROI ranking · binding lifecycle
  - **Orchestration & runtime** — Parallel DAG · durable queue + autoscaled workers · live model router · canary/A-B/rollback
  - **Integration / adapters** — LLM adapters · MCP tools/servers · SaaS connector catalog · connector SDK · bi-directional platform sync
  - **Data & knowledge** — Advanced RAG (hybrid + reranking, multi-modal) · embeddings/semantic search · caching
- **Control plane (cross-cutting):** SSO & RBAC · provenance & audit · eval & safety gates · human approval & policy · observability, recovery & FinOps
- **Takeaway (orange):** Teams add connectors and capabilities without weakening evidence, access or release controls.

---

## Slide 10 — Target technical architecture

- **Eyebrow:** TARGET TECHNICAL ARCHITECTURE
- **Title:** Production separates user interactions from resilient agent execution
- **Boxes / flow (left→right, top row = sync path, bottom row = async path):**
  - **Enterprise sources:** SharePoint · SQL/REST/GraphQL · SaaS APIs · document stores
  - **Secure access:** Enterprise SSO (SAML/OIDC) · SCIM · TLS / WAF · rate limits
  - **Web application & API:** Django web service · v1 JSON APIs · governance & approval workflow
  - **Durable job/agent services:** Queue + autoscaled workers · parallel DAG fan-out · retries / dead-letter / idempotency · scheduled & event triggers
  - **Approved services:** LLM providers via live model router · MCP + SaaS connectors · notifications / exports
- **Data tier (bottom band):**
  - **Managed PostgreSQL (+pgvector):** agents, runs, workflows, factory, lineage, audit — HA + tested restore
  - **Encrypted object storage / secrets vault:** payloads, packages, exports · rotated secrets · encryption at rest
  - **Cache & search:** prompt/embedding/RAG caches · retrieval indexes
- **Cross-cutting control plane (footer strip):** Secrets & keys · CI/CD + security scanning · logs, metrics & APM · backup/restore & DR · policy & quality gates

---

## Slide 11 — Roadmap overview (five horizons)

- **Eyebrow:** ROADMAP OVERVIEW
- **Title:** Five horizons sequence readiness before scale
- **Timeline row of 5 gate cards:**

| Date | Gate | Title | Focus |
|---|---|---|---|
| JUL 2026 | **H0** | Mobilise | Architecture · policies · baselines |
| AUG–SEP 2026 | **G1** | Pilot ready | Identity · durable execution · runtime eval gate |
| OCT–DEC 2026 | **G2** | Repeatable production | Resilience · staging/blue-green · MCP · model router |
| JAN–JUN 2027 | **G3** | Connected service | HA/DR · SaaS connectors · HITL · self-service builder |
| JUL–DEC 2027 | **G4** | Enterprise scale | SOC 2 · Guardrails 2.0 · marketplace · channels |

- **Principle (orange):** Each gate must pass security, quality, adoption and value criteria before scope or funding expands.

---

## Slide 12 — Delivery model (six workstreams)

- **Eyebrow:** DELIVERY MODEL
- **Title:** Six workstreams run across every horizon
- **Matrix (rows = workstreams, columns = FY26 pilot / FY26 production / FY27 connected / FY27 scale):**

| Workstream | FY26 pilot (G1) | FY26 production (G2) | FY27 connected (G3) | FY27 scale (G4) |
|---|---|---|---|---|
| **Platform & security** | SSO/SCIM, secrets vault | RBAC, staging, blue/green | HA/DR, encryption at rest | Pentest, SOC 2, assurance |
| **Reliability & scale** | Durable queue, parallel fan-out | Circuit breaker, self-healing, load/SLOs | Horizontal autoscale | Capacity testing, caching |
| **Quality & safety** | Runtime eval-gate enforcement | Migration safety, contract tests | Advanced RAG, canary/A-B | Guardrails 2.0, eval platform |
| **Experience & workflow** | Agent versioning + rollback UX | Scheduled/event triggers | HITL inbox, self-service builder | Marketplace, new channels |
| **Agent Factory** | Continuous insight ingestion | Blueprint pipeline at scale | Telemetry→blueprint feedback | Portfolio/ROI ranking |
| **Adoption & value** | Fleet baseline + owners | 2 repeatable BU engagements | ROI analytics + chargeback | Realised benefits + domain packs |

- **Note (orange):** Workstreams share one release train; feature and connector work cannot bypass security, quality or operating-model gates.

---

## Slide 13 — Horizon 0 (Mobilise)

- **Eyebrow:** HORIZON 0 · JULY 2026
- **Title:** July 2026 locks architecture, policies and the production baseline
- **Outcome:** A buildable, governed plan.
- **Scope line:** Removes architecture, policy, scope and ownership ambiguity before the production build starts.
- **Production hardening:**
  - Approve target hosting, network and queue/broker pattern
  - Confirm SSO model, roles and segregation of duties
  - Define secrets-vault, encryption and HA/DR architecture
  - Set retention, residency and AI-provider routing policy
  - Baseline reliability, security, quality and cost metrics
- **Product & adoption:**
  - Select pilot agent fleet and 2 pilot business units + named owners
  - Validate register → govern → run → observe journeys
  - Confirm eval-gate thresholds and promotion criteria
  - Convert the two-track backlog into outcome-based releases
- **Exit evidence (orange):** Target architecture approved; pilot charter signed; data/AI policy agreed; baseline metrics captured; named owners in place.

---

## Slide 14 — Horizon 1 (Pilot ready · G1)

- **Eyebrow:** HORIZON 1 · AUG–SEP 2026
- **Title:** A secure, durable pilot with quality gates enforced
- **Outcome:** Pilot-ready service.
- **Production hardening:**
  - Enterprise SSO (SAML/OIDC) + server-side sessions
  - Secrets management: managed vault, encrypted at rest, rotation
  - SSRF/egress hardening on connectors (allowlists, metadata-endpoint block)
  - Durable workflow queue (Celery/RQ + broker) replacing single-process worker
  - Parallel task fan-out in the orchestrator
  - OTLP exporter → external APM + golden dashboards
- **Product & adoption:**
  - **Enforce the eval gate at runtime** (block promotion on failing suite)
  - Agent versioning + one-click rollback UX
  - Pilot onboarding, training and support loop
- **Exit evidence (orange):** Security review passed, no critical/high findings; secrets vaulted; eval gate blocking in prod; pilot fleet runs without admin intervention.

---

## Slide 15 — Horizon 2 (Repeatable production · G2)

- **Eyebrow:** HORIZON 2 · OCT–DEC 2026
- **Title:** October–December turns the pilot into a repeatable service
- **Outcome:** Repeatable production.
- **Production hardening:**
  - Orchestrator resilience: circuit breaker, dead-letter, idempotency keys
  - Stale-run recovery & self-healing
  - Fine-grained RBAC & least-privilege review
  - Load & soak testing harness + published SLOs; DB tuning
  - Staging environment + blue/green deploys + migration safety
  - Alerting + on-call runbooks; structured logging + PII scrubbing
  - Automated compliance-evidence pipeline
- **Product & adoption:**
  - **MCP support** (consume MCP tools/servers)
  - **Live model router** (cost/latency/quality-aware dispatch)
  - Scheduled & event-driven workflow triggers (cron + webhooks)
  - Executive opportunity/portfolio view
- **Exit evidence (orange):** Two BUs on one configuration; SLOs met twice; zero-downtime deploys; ≥99% workflow-run success; evidence pipeline running.

---

## Slide 16 — Horizon 3 (Connected service · G3)

- **Eyebrow:** HORIZON 3 · JAN–JUN 2027
- **Title:** FY27 H1 connects sources to governed action at scale
- **Outcome:** Connected enterprise service.
- **Production hardening:**
  - HA/DR: Postgres HA, automated backups, tested restore, RPO/RTO
  - Third-party penetration test + remediation
  - Encryption at rest + field-level encryption for sensitive payloads
  - Horizontal scale for runtime + workers (autoscaling)
  - Data residency & per-tenant retention; risk register
  - Expanded test coverage + API v1 contract tests
- **Product & adoption:**
  - SaaS connector catalog wave 1 (ServiceNow, Salesforce, SharePoint, Snowflake)
  - Advanced RAG (hybrid search + reranking, multi-modal)
  - Canary / A-B testing for agents (traffic splitting)
  - Human-in-the-loop task inbox (approvals, exceptions, escalations)
  - Workflow versioning + templates library
  - Self-service low-code agent builder (start); factory feedback loop + portfolio view
- **Exit evidence (orange):** DR restore drill passes; pentest criticals/highs remediated; connectors live with lineage; HITL operational; ranked ROI backlog with owners.

---

## Slide 17 — Horizon 4 (Enterprise scale · G4)

- **Eyebrow:** HORIZON 4 · JUL–DEC 2027
- **Title:** July–December 2027 scales adoption and certifies the service
- **Outcome:** Supported, certifiable enterprise capability.
- **Production hardening:**
  - SOC 2 Type II readiness (controls mapped + evidence)
  - Caching layer (prompt / embedding / RAG)
  - Enterprise service levels and capacity testing
  - FinOps for models, storage, connectors and engagements
  - Recurring independent security assurance
- **Product & adoption:**
  - Guardrails 2.0 (jailbreak, toxicity, groundedness, output-schema validation)
  - Evaluation platform (LLM-as-judge, regression suites, red-teaming)
  - Progressive rollout + auto-rollback on quality/cost regression
  - Marketplace / catalog of reusable agents & tools
  - Additional channels (Teams/Slack, voice, streaming multi-agent)
  - Bi-directional platform sync (Azure AI Foundry, Copilot Studio)
  - Executive ROI & adoption analytics (value attribution, chargeback)
- **Exit evidence (orange):** SOC 2 audit-ready; maturity score ≥85 sustained; realised benefits validated; BUs reuse marketplace patterns.

---

## Slide 18 — Workstream detail: production hardening

- **Eyebrow:** WORKSTREAM DETAIL
- **Title:** Production hardening is a core delivery workstream, not a tax
- **Table (Control layer / Planned delivery / Leadership outcome):**

| Control layer | Planned delivery | Leadership outcome |
|---|---|---|
| **Identity & security** | SSO/SCIM, RBAC, secrets vault, SSRF/egress, pentest, encryption | Named users and privileged actions are controlled and auditable |
| **Reliability** | Durable queue, parallel fan-out, circuit breaker, self-healing, HA/DR | Business-critical agents run at fleet scale without data loss |
| **Scale & performance** | Load/soak SLOs, DB tuning, autoscaling, caching | Concurrent agent fleets do not degrade the service |
| **AI quality** | Runtime eval gate, canary/A-B, Guardrails 2.0, eval platform | Quality/safety drift is detected and blocked before release |
| **Operations** | APM, structured logs, alerting, runbooks, RPO/RTO, compliance evidence | Support teams can detect, explain and recover failures |

- **Hard rule (orange):** Hardening receives a protected capacity allocation through G1 and cannot be traded away to accelerate visible features.

---

## Slide 19 — Functionality roadmap

- **Eyebrow:** FUNCTIONALITY ROADMAP
- **Title:** New functionality is sequenced around the next user decision
- **Three wave cards:**
  - **FY26 Wave 1 · Trust & control** — Runtime eval gate · agent versioning + rollback · budget/quality alerting hardened.
  - **FY26 Wave 2 · Operate & connect** — MCP support · live model router · scheduled/event triggers · executive portfolio.
  - **FY27 · Connect & realise value** — SaaS connectors · HITL inbox · advanced RAG · canary/A-B · self-service builder · Guardrails 2.0 · marketplace · new channels · ROI analytics.
- **Prioritisation test (orange):** Does the feature improve trust, shorten a decision cycle, or move an agent toward validated production value?

---

## Slide 20 — Critical path

- **Eyebrow:** CRITICAL PATH
- **Title:** Six dependencies determine whether the dates are achievable
- **Six numbered items:**
  1. **Architecture decision** — Hosting, queue/broker, identity, data and model-routing patterns approved in July.
  2. **Security & data policy** — Classification, retention, residency and AI-provider assertions agreed.
  3. **Pilot commitment** — Two BUs, a defined agent fleet and named owners available to validate.
  4. **Quality baseline** — Golden eval cases and promotion thresholds defined before releases.
  5. **Source & tool access** — Connector credentials and MCP/tool approvals secured before build.
  6. **Benefit ownership** — Finance / benefit owners validate ROI estimates and realised outcomes.
- **Rule (orange):** Any unresolved dependency moves the affected gate; none should be hidden inside delivery estimates.

---

## Slide 21 — Operating model

- **Eyebrow:** OPERATING MODEL
- **Title:** Named business owners must govern the roadmap
- **Table (Role / Accountability / Operating rhythm):**

| Role | Accountability | Operating rhythm |
|---|---|---|
| **Enterprise sponsor** | Owns mandate, funding and gate decisions | Monthly gate review |
| **Product owner** | Prioritises roadmap, value and adoption | Weekly product council |
| **Platform build lead** | Owns architecture, delivery and operations | Release train |
| **Security, Risk & IT** | Approve controls, data use and production readiness | Control gates |
| **BU owners & builders** | Author agents, validate outcomes and benefits | Review-queue SLAs |
| **Finance / benefit owner** | Validates business case and realised value | Quarterly value review |

- **Rule (orange):** The operating model begins in July; it is not deferred until after the technology build.

---

## Slide 22 — Measures & governance

- **Eyebrow:** MEASURES AND GOVERNANCE
- **Title:** Every release gate has evidence-based thresholds
- **Table (Gate / Dimension / Metric):**

| Gate | Dimension | Threshold |
|---|---|---|
| **G1 Pilot** | Security & trust | No critical/high findings; secrets vaulted; eval gate blocking in prod |
| **G2 Production** | Repeatability & reliability | 2 BUs on one config; zero-downtime deploys; ≥99% workflow-run success; MTTR <30 min |
| **G3 Connected** | Integration & governance | DR restore drill passes; pentest criticals remediated; auditable connector lineage |
| **G4 Scale** | Certification & value | SOC 2 audit-ready; maturity score ≥85 sustained; realised benefits validated |

- **Gate action (orange):** At each gate, leadership may release the next tranche, redirect scope, or stop the programme.

---

## Slide 23 — Decision required

- **Eyebrow:** DECISION REQUIRED
- **Title:** July decisions determine whether the roadmap can start
- **Four numbered asks:**
  1. **Approve the FY26 hardening plan** — Fund mobilisation (H0) now; hold FY26 production funding to the G1 security & quality checkpoint. *(Budget envelope: [insert NTE].)*
  2. **Name the sponsor, product owner and two pilot business units** — Commit builders, Security, Risk and Finance to the roadmap gates.
  3. **Approve the target architecture and data-governance policy** — Complete hosting, identity, model-routing, retention and source/tool-access decisions in July.
  4. **Reserve FY27 expansion funding subject to G2** — Release only when repeatable production, adoption, quality and credible value are demonstrated. *(Reserve: [insert NTE].)*
- **Close (orange):** Approval now starts mobilisation; the next leadership gate is the end of September 2026 (G1).

---

### Notes for the deck builder
- Dollar figures are left as `[insert NTE]` placeholders — the reference deck used specific
  not-to-exceed envelopes; fill in yours before presenting.
- Content maps 1:1 to `PRODUCT_ROADMAP_FY26_FY27.md` (Tracks 1 & 2). If a target quarter here
  differs from that doc, the doc's per-item DoD is authoritative — this deck rounds items into
  the nearest horizon/gate for the executive narrative.
- Keep to Open Sans + the orange/grey palette above so it reads as a sibling to the Process
  Intelligence deck.
