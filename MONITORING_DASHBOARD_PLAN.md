# REPH Agentic Platform — Agent Catalog + Monitoring Dashboard + Hardening

**Audience:** Claude Code (implementation agent).
**Mode:** This is an implementation brief. Read `REVERSE_ENGINEERING_ANALYSIS.md` in this repo first for the current-state map. Work in phases; each phase has acceptance criteria and tests. Do not skip the tests.

---

## 0. Objective

Turn the existing single-page dashboard into a **professional-grade Agent Catalog + Monitoring surface**, and harden the platform's security/correctness defects in the same effort.

Two user-facing surfaces:

1. **Agent Catalog** — the inventory of registered agents, now with per-agent telemetry inline (usage, last run, success rate, cost, satisfaction) and a per-agent detail/monitoring drill-down.
2. **Monitoring Dashboard** — aggregate, cross-agent observability: usage & adoption, execution health, cost & tokens, quality & feedback — as live tiles plus historical trend charts, filterable by org hierarchy, agent, platform, and time window.

### Metric families to support (all four)
- **Usage & adoption:** runs over time, active users, runs per agent / platform / business unit, channel mix.
- **Execution health:** latency p50/p95/p99, success vs failure rate, error-type breakdown, tool-call volume & duration.
- **Cost & tokens:** input/output tokens and cost per agent / model / BU, cost trend over time.
- **Quality & feedback:** satisfaction score, rating distribution, feedback volume, list of low-rated runs to review.

### Non-goals
- No new agent *runtimes* (adapters stay as-is).
- No move off SQLite for the demo (but write aggregation so it ports to Postgres).
- No auth provider change (keep Django session auth); RBAC is noted as a future item, not built here.

---

## 1. Architecture decision (richer frontend)

Keep **Django as the API + auth + SSE backend**. Add a **React + Vite + TypeScript** dashboard compiled to static assets and served by Django, mounted as a SPA "island" inside the existing authenticated page.

**Why this shape:** it gives the app-like dashboard you want without abandoning Django's auth, admin, ORM, and the existing SSE run pipeline.

Stack choices (concrete, so Claude Code doesn't have to re-decide):
- **Frontend:** React 18 + TypeScript, built with Vite. Tailwind CSS for layout. **Tremor** (`@tremor/react`) for dashboard cards/charts, with **Recharts** (Tremor's dependency) available for custom charts. TanStack Query for data fetching/caching. `date-fns` for time handling.
- **Backend API:** Django REST Framework (DRF) for serializers, pagination, and filtering. Add `djangorestframework` to dependencies. All API routes under `/api/v1/`.
- **Auth:** same-origin session cookie (user already logged in via Django). CSRF token via cookie/header for mutations. No tokens/JWT.
- **Build/serve:** Vite builds to `controlplane/static/controlplane/dashboard/`. Django template loads the built `index` bundle. Provide a Vite dev-server proxy config for `/api` during development. Document both `npm run dev` (proxy to Django) and `npm run build` (production assets) in the README.
- **SSE run streaming stays unchanged** (the live agent chat already works); the React app consumes the same `/api/agents/<id>/run/` endpoint via `fetch` + `ReadableStream`.

Directory additions:
```
frontend/                      # Vite + React + TS source (new)
  src/
    main.tsx
    api/client.ts              # fetch wrapper w/ CSRF + error handling
    api/types.ts               # shared response types
    components/                # cards, charts, tables, filters
    routes/Catalog.tsx
    routes/Monitoring.tsx
    routes/AgentDetail.tsx
  vite.config.ts
  package.json
controlplane/api/              # DRF views/serializers (new package)
  serializers.py
  views.py
  urls.py
  aggregations.py              # all metric query logic, framework-agnostic
```

> If, after reading the codebase, Claude Code finds the React+Vite+DRF footprint too heavy for the demo's deployment story, the acceptable lighter alternative is **HTMX + Alpine.js + Chart.js** served directly by Django templates (no build step). Pick one and state the choice in a `DECISIONS.md`; do not mix both.

---

## 2. Data model & aggregation changes

### 2.1 Fix cost so monitoring is accurate (also analysis bug #6)
- Create `controlplane/services/pricing.py` with a single `PRICING` table keyed by a **normalized model id**, and `price_run(input_tokens, output_tokens, model_id) -> Decimal`.
- Normalize Bedrock/Azure ids to a base model (e.g. `anthropic.claude-3-5-sonnet-...v2:0` → `claude-3-5-sonnet`). Include OpenAI/Azure (`gpt-4o`, `gpt-4o-mini`), Claude family, and a documented `0` fallback for `echo`/`fake`/unknown.
- Add a **stored** `AgentRun.cost_usd = DecimalField(max_digits=12, decimal_places=6, default=0)` populated at run completion in `PlatformAgentRuntime.stream`. Reason: historical cost must be stable even if pricing changes. Keep the old computed property name free of conflict (rename the property or remove it in favor of the field; update `views.py` monitoring/dashboard accordingly).
- Migration required.

### 2.2 Time-series aggregation (Phase 2)
Two-tier approach:
- **On-the-fly:** add `controlplane/api/aggregations.py` using ORM `TruncDay`/`TruncHour` + `Count`/`Avg`/`Sum` over `AgentRun`, `AgentToolCall`, `AgentFeedback`, `TelemetryEvent`. This covers demo-scale data with no new tables.
- **Scale path (optional, implement model + command, wire scheduled run):** `RunMetricDaily` rollup model keyed by `(date, agent, platform, business_unit)` storing run_count, success/fail counts, token sums, cost sum, latency percentiles snapshot, avg_rating. A management command `rollup_metrics` recomputes/upserts daily rows; intended to be run nightly. The API reads rollups when present and falls back to on-the-fly for "today."

### 2.3 Percentile correctness (analysis bug #11)
- Add a small helper `percentile(sorted_values, q)` (linear interpolation) in `aggregations.py`; use it for p50/p95/p99 instead of the index hack in `views.py`. Unit-test it.

### 2.4 Version snapshot fix (analysis bug #5)
- Include `model_id` in `Agent._snapshot_version`. Migration not needed (field exists). Add a test.

---

## 3. API surface (DRF, under `/api/v1/`)

All endpoints `login_required`/`IsAuthenticated`, read-only unless noted. Support query params: `window` (`24h|7d|30d`), `agent`, `platform`, `business_unit`/org-hierarchy ids, `bucket` (`hour|day`).

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/v1/agents/` | Catalog list: agent fields + denormalized telemetry summary (runs_30d, success_rate, avg_latency_ms, cost_30d, satisfaction, last_run_at). Paginated, filterable, searchable. |
| GET | `/api/v1/agents/<id>/` | Agent detail incl. recent runs, tool-call summary, feedback summary, version history. |
| GET | `/api/v1/agents/<id>/metrics/` | Per-agent time-series for all four families over `window`. |
| GET | `/api/v1/monitoring/summary/` | Aggregate live tiles: total runs, active users, success rate, p50/p95/p99, total cost, total tokens, avg satisfaction, pending reviews. |
| GET | `/api/v1/monitoring/timeseries/` | Aggregate buckets over `window` for runs, latency, cost, tokens, success/fail, ratings. |
| GET | `/api/v1/monitoring/breakdowns/` | Group-bys: runs/cost by agent, by platform, by business_unit; error-type breakdown; rating distribution; channel mix. |
| GET | `/api/v1/feedback/low-rated/` | Recent runs with rating ≤ 2 for review (paginated). |
| GET | `/api/v1/org/tree/` | Full org hierarchy for filter controls (replaces/extends `org_children`). |
| POST | `/api/v1/runs/<id>/feedback/` | Submit feedback (with ownership check — see §4). |

Keep the existing SSE `run_agent` endpoint; optionally re-expose it under `/api/v1/agents/<id>/run/` for consistency.

Serializers must not leak raw `input_text`/`output_text` in list endpoints (see §4 PII). Detail endpoints may include them only for staff users.

---

## 4. Hardening (full pass — do alongside, gated before any non-local deploy)

Implement these; each gets a regression test. Priority order:

1. **Server-side Tier-4 approval (analysis bug #1).** Stop trusting the client `human_approved` flag. Require an approval record: a logged-in user with an approver role (use Django permission/group `agent_approver`) creates a `GovernanceReview`/approval row tied to the agent (and optionally a short-lived approval token/run). `run_agent` checks for a valid, recent, unexpired approval by an authorized approver before executing a Tier-4 agent, and writes who/when to telemetry. Update the frontend approval UX to call a real `POST /api/v1/agents/<id>/approvals/` endpoint.
2. **Session scoping (analysis bug #2).** `_resolve_session` must filter `ConversationSession` by `user_label`/owner as well as `agent`. Reject or ignore a `session_id` owned by another user.
3. **Feedback ownership (analysis bug #3).** `submit_feedback` must verify the run belongs to the requesting user (or the user is staff) before accepting a rating.
4. **Enforce governance on promotion (analysis bug #4).** `Agent.transition_to(PRODUCTION)` requires an approved `GovernanceReview`; raise `ValueError` otherwise. Add admin action + API to record reviews.
5. **Cost fix (§2.1).**
6. **Rate limiting (analysis bug #7).** Move to user-scoped keys; use a shared cache backend (document Redis for prod, keep LocMem for local). Honor `X-Forwarded-For` only behind a trusted-proxy setting. Make check-and-increment atomic (cache `add`/`incr`).
7. **PII & error hygiene (analysis risks #4, #5).** Don't stream raw `str(exc)` to clients — log full detail server-side, send a generic message + error id. Keep `input_text`/`output_text` out of list APIs and telemetry payloads. Add a settings flag `STORE_RUN_CONTENT` (default off in non-debug) controlling whether full text is persisted; otherwise store a truncated/redacted preview.
8. **model_id allowlist + history cap + provider timeouts (analysis missing-validation items).** Validate `Agent.model_id` against the pricing/allowlist; cap `ConversationSession.messages` length fed to the model; add request timeouts to the Anthropic/OpenAI/Bedrock adapter calls (HTTP adapter already has 60s).
9. **Secrets (analysis risk #7).** Add `.env` and `*.sqlite3` to `.gitignore`; document key rotation; fail loudly (not warn) on default `SECRET_KEY` when `DEBUG=False`.
10. **Remove dead code / stale comment (analysis bugs #8, #9).**

---

## 5. Frontend build

### 5.1 Shell & navigation
- Top-level tabs/routes: **Catalog**, **Monitoring**, **Agent Detail** (drill-down from either). Preserve the existing live-agent chat as a panel/route.
- Global filter bar: time window (24h/7d/30d), org hierarchy cascading selects (BU → Division → WorkStream → Process), platform, agent search. Filter state persists to `localStorage` and the URL query string.

### 5.2 Catalog view
- Data table (sortable, filterable, paginated) of agents with columns: name, platform, org unit, status, risk tier, **runs (30d)**, **success rate**, **avg latency**, **cost (30d)**, **satisfaction**, last run. Status and risk tier as colored badges.
- Row click → Agent Detail.
- Keep existing filter semantics (status/published, search, org cascade) but driven by the API, not client-side row hiding.

### 5.3 Monitoring view (the professional dashboard)
- **Tiles row (live snapshot, Phase 1):** total runs, active users, success rate, p50/p95/p99 latency, total cost, total tokens, avg satisfaction, pending reviews. Each tile shows delta vs previous period.
- **Charts (Phase 2):**
  - Usage: runs over time (area/line, bucketed), runs by platform (bar), runs by business unit (bar), channel mix (donut).
  - Execution: latency percentiles over time (multi-line), success vs failure (stacked area), error-type breakdown (bar), tool-call volume & avg duration (bar).
  - Cost & tokens: cost over time (area), cost by agent/model/BU (bar), token in/out over time (stacked).
  - Quality: satisfaction over time (line), rating distribution (bar), low-rated runs (table with link to run detail).
- Auto-refresh tiles on an interval (e.g. 30s) via TanStack Query; charts refresh on filter change and manual refresh button.

### 5.4 Agent Detail view
- Header: identity, status, risk tier, owners, version, model.
- Per-agent versions of the four metric families over the selected window.
- Recent runs table (latency, tokens, cost, status, rating) → run drill-down (tool calls, feedback). Respect PII setting from §4.

### 5.5 Polish (professional-grade)
- Consistent design tokens (reuse `relx-logo.png`, brand palette from existing `platform.css`).
- Loading skeletons, empty states, error toasts. Accessible (keyboard nav, ARIA on charts' data tables). Responsive down to tablet. Dark-mode optional.

---

## 6. Phasing & milestones

**Phase 0 — Foundations (no UI change)**
- Add DRF + `controlplane/api/` package, `aggregations.py`, `pricing.py`, percentile helper.
- Implement read APIs (§3) backed by on-the-fly aggregation.
- Fix cost (stored field + migration), version snapshot, percentile math.
- *Acceptance:* all new endpoints return correct data verified by tests; existing dashboard still works.

**Phase 1 — Live snapshot dashboard + Catalog**
- Stand up Vite/React shell, global filters, Catalog table (API-driven), Monitoring tiles, Agent Detail (snapshot only).
- Wire build into Django static serving; document dev/prod commands.
- *Acceptance:* Catalog and Monitoring render real data; filters work; logged-out users redirected to login.

**Phase 2 — Historical trends**
- Add timeseries/breakdowns endpoints + `RunMetricDaily` rollup model, `rollup_metrics` command, and (optionally) a nightly scheduled run.
- Build all charts in Monitoring + Agent Detail.
- *Acceptance:* charts match aggregation tests across 24h/7d/30d windows; rollup and on-the-fly paths agree within tolerance.

**Phase 3 — Hardening**
- Implement §4 items 1–10 with tests. Gate: Tier-4 cannot run without a real server-side approval; sessions/feedback are user-scoped; promotion requires approved review.
- *Acceptance:* security tests pass; manual bypass attempts (forged `human_approved`, foreign `session_id`, foreign `run_id`) are rejected.

> Phases 0–2 deliver the dashboard; Phase 3 can run in parallel with Phase 1/2 since it touches mostly backend `views.py`/runtime. Sequence 0 → (1 ∥ 3) → 2 if you want value fastest.

---

## 7. Testing strategy (required, currently zero tests)

Add `controlplane/tests/` (pytest-django). Minimum coverage:
- **Aggregations:** percentile helper; runs/cost/latency/rating group-bys against a seeded fixture with known expected values; window filtering; rollup vs on-the-fly parity.
- **Pricing:** every model id in `PRICING`, normalization of Bedrock/Azure ids, unknown → 0.
- **State machine:** all `ALLOWED_TRANSITIONS`, production-promotion side effects, governance gate.
- **Security:** Tier-4 approval enforcement, session ownership, feedback ownership, rate limiter (user-scoped, atomic).
- **API contract:** each endpoint's auth requirement, response shape, pagination, filter params; PII not leaked in list endpoints.
- **Adapter contract:** each adapter populates `meta` keys; tool-use loop in `_execute_llm` (mock the Anthropic client).
- **Frontend:** component tests for Catalog table, filter bar, and one chart (Vitest + Testing Library). Smoke test that the build serves and the app mounts.

Provide an expanded `seed_demo` (or a `seed_metrics` command) that generates a few weeks of `AgentRun`/`AgentToolCall`/`AgentFeedback`/`TelemetryEvent` history so the charts and tests have realistic data.

---

## 8. Acceptance criteria (definition of done)

1. Catalog shows every registered agent with live telemetry columns and drills into a per-agent monitoring view.
2. Monitoring dashboard shows all four metric families as live tiles (Phase 1) and historical charts (Phase 2), filterable by time window, org hierarchy, platform, and agent.
3. Cost and percentile numbers are correct and covered by tests; cost is non-zero for OpenAI/Azure/Bedrock runs.
4. Tier-4 execution requires a real, recorded, authorized approval; sessions and feedback are user-scoped; production promotion requires an approved governance review.
5. No raw exception text or run content leaks to non-staff clients.
6. Test suite exists and passes; `seed_demo`/`seed_metrics` produces demo data; README documents migrate → seed → build → runserver.
7. A `DECISIONS.md` records the frontend stack choice and any deviations from this plan.

---

## 9. Guardrails for the implementing agent

- Do **not** edit migrations already applied; add new ones.
- Keep the live-agent SSE pipeline working at every step.
- Land each phase behind passing tests before starting the next.
- Don't commit `.env`, the SQLite DB, or any API key.
- Where this plan and the actual code disagree, trust the code and note the discrepancy in `DECISIONS.md`.
