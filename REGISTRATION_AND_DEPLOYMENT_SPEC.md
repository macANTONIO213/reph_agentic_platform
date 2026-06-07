# REPH Agentic Platform — Registration & Governed Deployment

**Audience:** Claude Code (implementation agent).
**Status:** Implementation spec. Part of Phase A in `AGENTIC_RUNTIME_ARCHITECTURE.md`. Read `REVERSE_ENGINEERING_ANALYSIS.md` for current-state details. Do not start coding until the approach below is clear; build behind tests.

---

## 1. Problem this solves

Two gaps in the current build:

1. **No self-serve registration in the product.** The dashboard (`controlplane/templates/controlplane/dashboard.html`) has tabs for Deployments, Monitoring, and Agent Catalog, but **no "Register agent" control anywhere.** Agents only enter the system via the `seed_demo` command or the Django admin add form (`/admin/controlplane/agent/add/`).
2. **The admin is an ungoverned write path.** `AgentAdmin` lets any staff user create an agent and set `status=production`, change `risk_tier`, and edit `tool_names`/grants by writing model fields directly — with **no eval, no approval, no audit, no tenancy scoping.** `Agent.transition_to()` enforces a state machine, but admin (and direct `.save()`) bypass it entirely. This makes the governance story decorative.

We are **not** removing the admin. We are making it a scoped back-office tool and routing every governance-sensitive change through one service, so neither entry point can skip the gates.

---

## 2. Principle: one governance service, two entry points

```
        Agent-owning teams                 Platform / tech team
        ┌──────────────────┐               ┌──────────────────┐
        │  Product UI + API │               │   Django Admin    │
        │  (self-serve)     │               │  (back-office)    │
        └─────────┬────────┘               └─────────┬────────┘
                  │                                   │
                  ▼                                   ▼
        ┌───────────────────────────────────────────────────────┐
        │              GovernanceService (single choke point)     │
        │  register · version · request_eval · record_approval   │
        │  promote · rollback · retire   → enforces gates + audit │
        └───────────────────────────────────────────────────────┘
                                  │
                                  ▼
                         Registry (DB) + AuditLog
```

**Rule:** No code path — UI, API, admin, or management command — writes governance-sensitive state by calling `Agent.save()` directly. They all call `GovernanceService`. Direct field writes to those fields are prevented (see §5).

---

## 3. The governance service layer

Create `controlplane/services/governance.py` with a single `GovernanceService` (stateless, methods take the acting user for authz + audit). It is the only place lifecycle/state changes happen.

```python
class GovernanceService:
    def register_agent(self, *, actor, data) -> Agent: ...
        # creates Agent in status=draft, validates manifest/model_id allowlist,
        # scopes to actor's tenant/org, writes AuditLog("agent.registered")

    def create_version(self, *, actor, agent, manifest) -> AgentVersion: ...
        # immutable snapshot (incl. model_id, tool grants); AuditLog

    def request_eval(self, *, actor, version) -> EvalRun: ...

    def record_approval(self, *, actor, agent, scope, expires_at) -> Approval: ...
        # actor must hold the `approver` role; AuditLog("agent.approved")

    def transition(self, *, actor, agent, to_status, reason="") -> None: ...
        # wraps Agent.transition_to(); enforces ALLOWED_TRANSITIONS
        # promote→production additionally requires: passing EvalRun AND
        # a valid (unexpired) Approval AND an approved GovernanceReview.
        # AuditLog every transition with actor + reason.

    def rollback(self, *, actor, agent, to_version) -> None: ...
    def retire(self, *, actor, agent, reason) -> None: ...
```

Gate logic for promotion to `production` (raise a domain error otherwise):
- current status allows the transition (`Agent.ALLOWED_TRANSITIONS`),
- the target version has a passing `EvalRun`,
- an approved `GovernanceReview` exists,
- a valid `Approval` by an authorized approver exists (replaces the client-trusted `human_approved` flag — analysis bug #1),
- actor has permission for this agent's tenant.

Every method writes an `AuditLog` row (append-only): actor, action, agent/version, before/after status, reason, timestamp, source (`ui` | `api` | `admin` | `system`).

---

## 4. Path 1 — Product UI + API (agent-owning teams)

The governed, self-serve front door. This is the "Register agent" experience the dashboard is missing.

**API (DRF, under `/api/v1/`):**
- `POST /agents` → `register_agent` (status starts `draft`).
- `POST /agents/<id>/versions` → `create_version` from a manifest.
- `POST /agents/<id>/versions/<v>/evals` → `request_eval`.
- `POST /agents/<id>/approvals` → `record_approval` (approver role only).
- `POST /agents/<id>/versions/<v>/promote` → `transition(to=production)`.
- `POST /agents/<id>/rollback`, `POST /agents/<id>/retire`.

All endpoints: `IsAuthenticated`, tenant-scoped (a user sees/edits only agents in their org subtree), CSRF for session-auth mutations.

**UI:**
- Add a **"Register agent"** button on the Deployments tab (primary action, top-right of the section heading) and in the Agent Catalog top bar.
- Opens a guided form/wizard collecting: name, owner, technical owner, business unit + org hierarchy (reuse the existing cascading `org_children` selects), platform/`kind`, purpose, system prompt, model (from allowlist), tools, data sources, risk tier (declared), endpoint (if external). Validates client + server side.
- On submit → `POST /agents` → lands the agent in `draft`, shown in the catalog with a "Draft — not deployed" badge and next-step actions (request review, run eval, request approval, promote) gated by role and current status.
- Promotion and approval actions are visible but disabled with explanatory tooltips when the gates aren't met.

**Stopgap (optional, if a working button is needed before the full wizard):** the "Register agent" button can deep-link to the Django admin add page for now, clearly labeled as the interim path, then be swapped to the wizard. Do not ship the stopgap as the final state.

---

## 5. Path 2 — Django admin (platform / tech team)

Keep the admin, but demote and lock it:

1. **Scope access.** Only platform-team members get `is_staff`. Agent-owning teams use the product UI, not admin. Document this in the deployment runbook.
2. **Make governance-sensitive fields safe in admin.** In `AgentAdmin`, make `status`, `risk_tier`, `tool_names`/grants, `model_id`, and `deployed_at` **read-only**. Status changes happen only through admin *actions* that call `GovernanceService.transition(...)` — so even a superuser hits the same gates. Plain identity/metadata fields (owner, purpose, descriptions) may stay editable.
3. **Route admin writes through the service.** Override `AgentAdmin.save_model` so creating/changing an agent calls `GovernanceService` (or at minimum logs an `AuditLog` and respects the state machine) rather than a raw `.save()`.
4. **Break-glass, audited.** If the platform team genuinely needs to override a gate (incident response), provide a single explicit, permission-gated "force transition" admin action that **requires a typed reason** and writes a high-visibility `AuditLog("agent.transition.forced")`. No silent overrides.
5. **Audit everything.** Every admin mutation writes an `AuditLog` row with `source="admin"`.

Net effect: admin is a convenience and break-glass tool for the tech team, not a hole in the control plane.

---

## 6. RBAC roles (minimum)

- **viewer** — read catalog, monitoring, runs (tenant-scoped).
- **builder** — register agents, create versions, request eval/review (own tenant).
- **approver** — record approvals, approve `GovernanceReview`, authorize promotion.
- **platform-admin** — staff/admin access, break-glass, cross-tenant.

Enforce via Django groups/permissions + tenant (org-subtree) row-level filtering. The existing BusinessUnit→Division→WorkStream→Process tree is the tenancy spine.

---

## 7. Data model touchpoints

- **`AuditLog`** (new): id, actor, action, target (agent/version), from_status, to_status, reason, source, created_at. Append-only (no update/delete in admin).
- **`Approval`** (new): agent, approver, scope, created_at, expires_at, consumed flag.
- **`GovernanceReview`** (exists): used as a promotion gate now.
- **`AgentVersion`** (exists, extend): ensure `model_id` is snapshotted (analysis bug #5).
- **`Agent.transition_to`** (exists): keep, but it must be reached **only** via `GovernanceService.transition`; stop calling `Agent.save()` on governance fields elsewhere.

---

## 8. Acceptance criteria

1. A builder can register an agent from the product UI; it appears in the catalog as `draft`.
2. An agent cannot reach `production` from any path (UI, API, admin, shell-ish admin action) without a passing eval, an approved review, and a valid approval by an approver.
3. The admin cannot set `status`/`risk_tier`/grants by direct field edit; those are read-only and only change via service-backed actions.
4. A forged or absent approval is rejected (no `human_approved` shortcut remains).
5. Tenancy holds: a builder in BU A cannot see or modify BU B's agents via UI or API.
6. Every register/version/approve/promote/rollback/retire — from any source — produces an `AuditLog` row identifying actor and source.
7. Break-glass force-transition works only for platform-admin, requires a reason, and is loudly audited.

## 9. Tests (required)

- GovernanceService: each gate (happy path + each failure: no eval, no review, no approval, expired approval, illegal transition, wrong tenant, wrong role).
- Admin: governance fields read-only; `save_model` routes through service; force-transition requires reason + role; audit rows written with `source="admin"`.
- API: auth, tenancy scoping, register→promote flow, forged-approval rejected.
- UI: register form validates and posts; gated actions disabled when gates unmet (component test).
- Regression: no remaining code path mutates `Agent.status` via raw `.save()` (grep/test for it).

## 10. Guardrails

- Additive migrations only.
- One governance service; neither UI, API, admin, nor commands write governance-sensitive state directly.
- Keep the admin — scope and lock it; don't delete it.
- Land behind passing tests; record any deviation in `DECISIONS.md`.
