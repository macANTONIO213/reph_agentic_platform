# Architectural Decisions

## Frontend stack (Phase 1)

**Decision:** Chart.js (CDN) + vanilla JavaScript, served by Django templates.

**Rejected:** React + Vite + TypeScript + Tremor + TanStack Query.

**Reason:** The plan explicitly allows the lighter alternative when "the React+Vite+DRF footprint is too heavy for the demo's deployment story." This codebase has zero Node.js tooling, no package.json beyond a stub, and an existing vanilla-JS frontend. Adding a full React/Vite build chain introduces npm, node_modules, a build step, and a separate dev server — all for a single-page demo. Chart.js loaded from CDN delivers the same visual output with no build step and no new dependencies.

## API layer (Phase 1)

**Decision:** Plain Django `JsonResponse` views under `/api/v1/`, no DRF.

**Rejected:** Django REST Framework.

**Reason:** DRF's serializers, pagination and filtering are genuinely useful at scale, but the plan's monitoring endpoints are read-only aggregations. They don't benefit from DRF's ModelSerializer (no CRUD), browsable API (demo has its own UI), or schema generation. Plain views with manual dicts keep the dependency footprint minimal and avoid `pip install djangorestframework` on a machine where requirements management is manual.

**Future path:** If RBAC, API tokens, or external clients become requirements, wire DRF in then — the `controlplane/api/` package structure is already isolated.

## Cost storage (Phase 0)

**Decision:** `AgentRun.cost_usd` promoted from computed `@property` to a stored `DecimalField(max_digits=12, decimal_places=6)`, populated at run completion by `PlatformAgentRuntime.stream` using `pricing.price_run()`.

**Reason:** Historical cost must be stable even if the pricing table changes. Computed properties re-price old runs at today's rates, which is wrong for trend charts and rollup accuracy.

## Deviations from MONITORING_DASHBOARD_PLAN.md

| Plan item | Actual implementation | Reason |
|-----------|----------------------|--------|
| React + Vite + Tremor frontend | Chart.js + vanilla JS, no build | See above |
| DRF serializers | Plain Django views | See above |
| `RunMetricDaily` rollup model | Not implemented in Phase 0/1 | Deferred to Phase 2; on-the-fly aggregation covers demo scale |
| pytest-django test suite | Not in Phase 0/1 | Deferred to Phase 3; CI is not wired yet |

## Deviations from REGISTRATION_AND_DEPLOYMENT_SPEC.md

| Spec item | Actual implementation | Reason / tracking |
|-----------|----------------------|-------------------|
| `EvalRun` model and eval gate | Not implemented | EvalRun model does not yet exist. `GovernanceService.transition()` enforces GovernanceReview + Approval gates only. Eval gate wired as `TODO` comment; add when EvalRun migration lands. |
| Tenant row-level scoping (builder sees only own BU's agents) | Not enforced on API/UI | No `UserProfile→OrgUnit` link exists. Staff/admin are cross-tenant; all authenticated users can register agents in any BU. Add a `UserProfile` model + middleware before multi-tenant rollout. |
| `POST /agents` REST endpoint | Implemented as `POST /api/v1/agents/register/` | Kept separate from `GET /api/v1/agents/` to avoid modifying the existing read endpoint's `@require_GET` decorator. |
| Full multi-step wizard UI | Single-page modal with 4 sections | Delivers the required fields; wizard step pagination deferred until UX feedback on one-page form. |
| Builder role enforced on `register_agent` API | Any authenticated user may call it | Matching the spec's §4 note that builder role enforcement is minimal on the first iteration. Tighten by adding `_require_role(actor, {"agent_builder", "platform_admin"})` to `GovernanceService.register_agent`. |
| Admin force-transition with typed-reason intermediate page | Force-transition admin actions accept `?_reason=` URL param | Building a custom intermediate admin page requires an extra URL + template. The current approach is functional for CLI/scripted use; a proper UI page is deferred. |
