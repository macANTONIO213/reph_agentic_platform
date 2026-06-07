# Agentic Platform — Inspection Findings Report

**Date:** 2026-06-07
**Scope:** Read-only inspection (no source code modified). Runtime confirmations were run
against a disposable SQLite database (`DATABASE_URL=sqlite:///inspect_throwaway.sqlite3`,
seeded via `seed_demo`), with **no LLM API key** (fake-engine path), and all security
probes confined to **localhost / `file://`** only.
**Baseline:** `python manage.py test controlplane` → **148 passed**, no pending migrations,
`manage.py check` clean (emits the insecure-`SECRET_KEY` warning).

---

## Executive summary

The architecture is clean and well-documented, but the platform's **two headline
capabilities — running an agent and orchestrating a multi-agent workflow — are both
broken at runtime**, and the test suite is green only because neither path is exercised
end-to-end. Several governance and tenancy invariants that the code *describes* are **not
enforced** on the HTTP path. One hypothesised issue (RAG cross-tenant leak) was **tested and
refuted**.

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | **Core run path crashes on every invocation** (`'UUID' object has no attribute 'id'`) | 🔴 Critical | Runtime-confirmed |
| 2 | **Multi-agent orchestration dead** (`ImportError: AgentRuntime`) | 🔴 Critical | Runtime-confirmed |
| 3 | **API production-gate bypass** (Approval + Eval gates skipped) | 🟠 High | Runtime-confirmed |
| 4 | **SSRF / local-file disclosure** via `HttpApiAdapter.endpoint_url` | 🟠 High | Runtime-confirmed |
| 5 | **No tenant scoping** on agent list *or* run (cross-BU read + execute) | 🟠 High | Runtime-confirmed |
| 6 | **Shared-memory write has no ownership check** (cross-run/cross-BU write) | 🟡 Medium | Runtime-confirmed |
| 7 | **Model router downgrades Tier-4 agents under budget pressure** | 🟡 Medium | Runtime-confirmed |
| 8 | **Unauthorized registration returns HTTP 500 instead of 403** | 🟡 Medium | Runtime-confirmed |
| 9 | **Cost accuracy**: unknown model → $0 silently; typo'd id mis-priced | 🟡 Medium | Runtime-confirmed |
| 10 | **Settings hardening**: `DEBUG=True` default, insecure `SECRET_KEY` fallback, `admin/admin` seeded | 🟡 Medium | Code-confirmed |
| 11 | Guardrails are input-only, regex-based (output & tool-args unscanned) | 🟡 Medium | Code-confirmed |
| — | ~~RAG cross-tenant retrieval leak~~ | — | **Refuted / false positive** |

---

## Confirmed findings

### 1. 🔴 Core agent-run path crashes on every invocation
**Location:** [`agent_runtime.py:82`](controlplane/services/agent_runtime.py:82), [`telemetry.py:289-290`](controlplane/services/telemetry.py:289)

`PlatformAgentRuntime.stream()` calls `telemetry_service._trace_id_from_run(run.id)` — passing
a **UUID**. But the method signature is `_trace_id_from_run(self, run)` and its body does
`return _trace_id_from_run(run.id)`, i.e. it expects a *run object* and calls `.id` on it.
Passing a UUID makes it evaluate `UUID.id` → `AttributeError`. The line is **outside** the
`try/except`, so the generator raises before yielding anything; the result is also unused
(the root span recomputes the trace id internally).

**Evidence (Tier-1 production agent, fake engine):**
```
CORE RUN PATH CRASHED -> AttributeError : 'UUID' object has no attribute 'id'
run left in status: started (never completes)
```
**Impact:** Running any agent via `POST /api/agents/<id>/run/` fails; the `AgentRun` is left
stuck in `started`. This is the platform's primary feature.
**Why tests miss it:** No test drains `PlatformAgentRuntime.stream()` or hits the run
endpoint (`grep` of `controlplane/tests/` for the run URL / `streaming_content` / `.stream(`
returns nothing).

### 2. 🔴 Multi-agent orchestration is non-functional
**Location:** [`orchestrator.py:274`](controlplane/services/orchestrator.py:274), [`orchestrator.py:358`](controlplane/services/orchestrator.py:358)

Both `_invoke_agent` and `delegate_to_agent` do
`from controlplane.services.agent_runtime import AgentRuntime`, but that module only defines/
exports **`PlatformAgentRuntime`**.

**Evidence (real 2-step workflow, no mocks):**
```
WORKFLOW STATUS: failed   ERROR: Failed steps: ['step1']   OUTPUTS: {}
step1 | failed  | "cannot import name 'AgentRuntime' from 'controlplane.services.agent_runtime'"
step2 | skipped | 'Upstream dependency failed.'
```
**Secondary bug (latent behind #2):** even with the class name fixed, the SSE parse loop in
`_invoke_agent` filters on `sse_line.startswith("data:")` and reads `payload["type"]`, but
adapters yield whole `"event: <name>\ndata: {...}\n\n"` blocks whose data dict has **no
`type` key** ([`base.py:22`](controlplane/services/adapters/base.py:22)) — so output/`run_id`
would still never be captured. And #1 would still crash the underlying run.
**Why tests miss it:** every `OrchestratorTests` case patches `_invoke_agent`;
`test_workflow_trigger` patches `execute` ([`test_phase_e.py:257,391`](controlplane/tests/test_phase_e.py:257)).

### 3. 🟠 API production-gate bypass
**Location:** [`api/views.py:296`](controlplane/api/views.py:296) vs [`governance.py:304`](controlplane/services/governance.py:304)

`GovernanceService._check_production_gates` enforces **three** gates (approved
GovernanceReview + valid Approval token + passing EvalRun). But the API endpoint
`agent_transition` calls `agent.transition_to()` **directly**, which enforces **only** the
GovernanceReview gate ([`models.py:227`](controlplane/models.py:227)). The strict path is only
used by `admin.py`.

**Evidence (same agent: approved review, no approval token, active suite with no passing run):**
```
PATH A GovernanceService.transition : BLOCKED -> ...a valid ... Approval ... is required
PATH B agent.transition_to (API)    : PROMOTED -> status = production
```
**Impact:** Via the HTTP API an agent reaches production without an approval token or a
passing eval. Violates the "single choke point" invariant in `governance.py`.

### 4. 🟠 SSRF / local-file disclosure via HttpApiAdapter
**Location:** [`http_api.py:67-75`](controlplane/services/adapters/http_api.py:67)

`agent.endpoint_url` is passed straight to `urllib.request.urlopen` — **no scheme check, no
host allowlist, no private-IP block**. urllib's default opener also handles `file://`.

**Evidence (`file://` to a local secret file, fully offline):**
```
endpoint_url accepted (no validation): file:///.../ssrf_secret.txt
adapter output_text: 'TOP-SECRET-INTERNAL-DATA-12345'
RESULT: LOCAL FILE EXFILTRATED via file:// SSRF
```
**Impact:** A registered (or prompt-injected) agent can read local files and reach internal
services (`http://127.0.0.1:...`, cloud metadata IPs); the platform's `HTTP_API_BEARER_TOKEN`
is attached to outbound requests. Note: `URLField` form validation would reject `file://` in
the *admin*, but the API registration path uses `Agent.objects.create()` which skips field
validation; `http://127.0.0.1:<port>` is a valid URL accepted everywhere.

### 5. 🟠 No tenant scoping on read or execute
**Location:** [`api/views.py:74`](controlplane/api/views.py:74) (`agents_list`), [`views.py:106`](controlplane/views.py:106) (`run_agent`); `UserProfile.can_access_agent` exists ([`models.py:472`](controlplane/models.py:472)) but is referenced **only in tests**.

**Evidence:**
```
TENANT: viewer(BU=Elsevier) sees 10/10 agents; org_units in result = {Reed Elsevier, LexisNexis, Elsevier}
RUN:    viewer(BU=Elsevier) runs Reed Elsevier agent -> HTTP 200 streaming (allowed)
```
**Impact:** Any authenticated user can list and execute every agent in every business unit.
Builders are scoped only at *registration* time, not at read/run time.

### 6. 🟡 Shared-memory write has no ownership/scope check
**Location:** [`api/views.py:996`](controlplane/api/views.py:996), [`memory.py`](controlplane/services/memory.py)

**Evidence:**
```
MEMORY: unrelated viewer writes to a BU2 workflow run (triggered_by=other_user) -> HTTP 200, wrote=True
```
**Impact:** Any logged-in user can write arbitrary keys into any `WorkflowRun`'s shared memory
by guessing its UUID — a cross-agent context-poisoning vector once orchestration works.

### 7. 🟡 Model router downgrades Tier-4 agents under budget pressure
**Location:** [`model_router.py:98`](controlplane/services/model_router.py:98) (budget checked **before** risk tier)

**Evidence:**
```
Tier-4 + budget_alert -> model: claude-haiku-4-5   (docstring promises opus for Tier-4)
```
**Impact:** The most safety-sensitive agents are silently routed to the weakest/cheapest
model when a budget alert is set — cost pressure overrides risk-based capability. Untested.

### 8. 🟡 Unauthorized registration returns 500 instead of 403
**Location:** [`api/views.py:414-417`](controlplane/api/views.py:414); `governance.register_agent` raises `PermissionError` ([`governance.py:362`](controlplane/services/governance.py:362))

**Evidence:**
```
viewer register -> HTTP 500 body={"error": "Unexpected error: Action requires one of these roles: ['agent_approver', 'agent_builder'...]"}
```
**Impact:** `PermissionError` falls through to the generic `except Exception → 500`. Leaks an
internal error message and misreports an authz failure. (The endpoint docstring also wrongly
claims "any authenticated user may register".)

### 9. 🟡 Cost accounting accuracy
**Location:** [`pricing.py`](controlplane/services/pricing.py)

**Evidence:**
```
'totally-unknown-xyz' -> normalize='totally-unknown-xyz' price=0      (silent $0)
''                    -> normalize=''                    price=0      (silent $0)
'gpt-4o-typo'         -> normalize='gpt-4o'              price=0.0125 (mis-priced as gpt-4o)
'claude-haiku-4-5'    -> normalize='claude-haiku-4-5'    price=0.006  (router output prices OK)
```
**Impact:** A genuinely unknown/typo'd `model_id` costs $0 with no warning (budget alarm
blind spot); a near-miss typo silently mis-prices to a different model. (Note: the earlier
worry that the router's `claude-haiku-4-5` output is unpriced is **false** — it prices fine; the
remaining issue is only the governance allowlist key mismatch, `claude-haiku-4-5` vs
`claude-haiku-4-5-20251001`.)

### 10. 🟡 Settings hardening
**Location:** [`settings.py:11,27`](agentic_platform/settings.py:11); `seed_demo`

`DEBUG` defaults to `True`; `SECRET_KEY` falls back to an insecure default (warn-only, run
continues); no `SECURE_*` / `SESSION_COOKIE_SECURE` / HSTS; `seed_demo` creates an
`admin/admin` superuser. The insecure-key warning fires on every `manage.py` invocation.

### 11. 🟡 Guardrails are shallow and input-only
**Location:** [`guardrails.py`](controlplane/services/guardrails.py)

Regex rules scan **only the user input** before the LLM call; the model output and tool-call
arguments are never scanned, and the patterns are evadable (encoding/paraphrase/multilingual).

---

## Refuted hypothesis (important)

**RAG cross-tenant retrieval leak — NOT a bug.** The earlier concern was that the BU filter
`Q(business_unit__isnull=True) | (Q(business_unit_id=bu_id) if bu_id else Q())`
([`rag.py:182`](controlplane/services/rag.py:182)) would match all BUs when `bu_id is None`,
because an empty `Q()` is "match-all". **Tested and refuted:** Django *drops* the empty `Q()`,
so the WHERE clause resolves to `business_unit_id IS NULL`.

**Evidence:**
```
anon(None) SQL WHERE: ... business_unit_id IS NULL
anon(None) retrieves: ['Platform Wide']            # only platform-wide docs
BU1 agent retrieves : ['BU1 Secret Policy', 'Platform Wide']   # own BU + platform-wide
```
Scoping is correct. This was caught by running the probe rather than trusting the reading.

---

## Test-gap matrix

| Area | Existing coverage | Missing |
|---|---|---|
| Core run endpoint (`run_agent` / `stream`) | **None** | Integration test that drains the SSE stream → would catch #1 |
| Orchestrator agent invocation | DAG logic only; `_invoke_agent`/`execute` mocked | Un-mocked end-to-end run → would catch #2 |
| Production gate via API | Model-level `transition_to` tested | Test asserting the **API** enforces Approval + Eval → #3 |
| `HttpApiAdapter` URL handling | None | SSRF/scheme-validation test → #4 |
| Tenant scoping on read/run path | `can_access_agent` unit-tested on model only | Cross-BU list/run assertion → #5 |
| Shared-memory authorization | Write/read happy path | Cross-user/cross-run write rejection → #6 |
| Router Tier-4 + budget interaction | tier4→opus (budget off), budget→haiku (tier 3) | Tier-4 **with** budget_alert → #7 |
| Pricing unknown/typo model | Known models only | Unknown-id and prefix-typo cases → #9 |
| Guardrail output/tool-arg scanning | Input-rule unit tests | Output & tool-argument scanning (currently absent by design) → #11 |

---

## Suggested remediation order (for approval — no code changed yet)

1. **#1 core run crash** — one-line fix (`_trace_id_from_run(run)` not `run.id`, or drop the
   unused line) + an integration test that drains the stream. Unblocks everything.
2. **#2 orchestrator** — `AgentRuntime → PlatformAgentRuntime`, fix the SSE `event:`/`type`
   parsing, add an un-mocked workflow test.
3. **#3 production-gate bypass** — route `agent_transition` through `governance.transition`.
4. **#4 SSRF** — scheme allowlist (`http`/`https`) + private-IP/host validation on
   `endpoint_url`, applied on the registration path too.
5. **#5 tenant scoping** — enforce `can_access_agent` in `agents_list` / `run_agent`.
6. **#6–#11** — memory authz, router ordering, register 403, pricing warnings, settings
   hardening, guardrail output scanning.

---

## Inspection artifacts / cleanup

- No source files were modified.
- Throwaway DB `inspect_throwaway.sqlite3` (gitignored) and a temporary `ssrf_secret.txt`
  were created during runtime probes; both are removed/safe to remove. Your real
  `agentic_platform_demo.sqlite3` was never touched.
