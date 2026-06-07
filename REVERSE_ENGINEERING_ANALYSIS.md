# REPH Agentic Platform — Reverse Engineering Analysis

*Read-only analysis. No code was changed.*

## 1. What this project is

A Django control plane for registering, governing, running, and monitoring AI agents that live on many different runtimes (in-house Django, Azure OpenAI / AI Foundry, Microsoft Copilot Studio, AWS Bedrock, custom REST, vendor, embedded). One agent — **Agent Deployment Advisor** — is actually executable in the demo; the rest are registry entries.

Stack: Django + SQLite, server-rendered dashboard, vanilla JS frontend, Server-Sent Events (SSE) for streaming agent output, pluggable "adapter" classes per runtime.

## 2. Project structure

```
agentic_platform/          Django project (settings, urls, wsgi/asgi)
controlplane/              The single app
  models.py                Org hierarchy, Agent, AgentRun, ToolCall, Telemetry, Governance, Feedback, Version, Session
  views.py                 Dashboard + JSON/SSE API endpoints
  urls.py                  Route table
  admin.py                 Full Django admin for every model
  services/
    agent_runtime.py       PlatformAgentRuntime — owns the run lifecycle
    adapters/
      base.py              AgentAdapter ABC + RuntimeEvent (SSE) + shared helpers
      django_runtime.py    Anthropic Claude path (+ deterministic fake fallback)
      openai_adapter.py    OpenAI / Azure OpenAI
      bedrock.py           AWS Bedrock Converse API
      http_api.py          Generic REST (Copilot, custom, vendor)
      echo.py              No-op reflector (embedded / unknown platform)
      registry_tools.py    The 3 built-in tools + risk keyword logic
  management/commands/seed_demo.py   Repeatable demo data
  static/ , templates/     Dashboard UI + login
```

### Most important files & functions

| File | Function / class | Role |
|------|------------------|------|
| `views.py` | `run_agent` | Single entry point for execution: auth, rate limit, Tier-4 gate, JSON parse, session resolve, kicks off streaming |
| `views.py` | `_resolve_session` | Loads or creates the multi-turn conversation session |
| `services/agent_runtime.py` | `PlatformAgentRuntime.stream` | Orchestrates the whole run: creates `AgentRun`, picks adapter, streams events, persists results + telemetry, updates session |
| `services/agent_runtime.py` | `_select_adapter_class` / `_PLATFORM_ADAPTER_MAP` | Platform → adapter routing |
| `adapters/base.py` | `AgentAdapter`, `RuntimeEvent`, `_emit_tokens`, `_record_tool` | Adapter contract + SSE formatting + fake token streaming |
| `adapters/django_runtime.py` | `_execute_llm` | The real prediction loop: Anthropic messages + tool-use loop |
| `adapters/registry_tools.py` | `RegistryToolsMixin` (`_classify_risk`, `_registry_search`, `_deployment_checklist`, `_dispatch_tool`) | Governance/registry "tools" the model calls |
| `models.py` | `Agent.transition_to` / `ALLOWED_TRANSITIONS` | Lifecycle state machine (draft→review→pilot→production→retired) |
| `models.py` | `AgentRun.cost_usd` | Per-run cost estimate from token counts |

## 3. Request flow

1. Browser loads `/` → `dashboard` view (login-required, sets CSRF cookie), renders agents, metrics, telemetry, monitoring.
2. User submits the chat form → `platform.js:runAgent` → `streamAgentRun` POSTs JSON `{message, session_id?}` to `/api/agents/<uuid>/run/` with `X-CSRFToken`.
3. `run_agent` view: requires login + POST; loads the agent **only if status is pilot or production**; checks IP rate limit; parses/validates the message (non-empty, ≤4000 chars); enforces the Tier-4 approval gate; resolves the conversation session.
4. Returns a `StreamingHttpResponse` of `content_type=text/event-stream`. The JS reads the stream, splits on `\n\n`, and dispatches `status` / `token` / `tool` / `done` / `error` events into the chat log, tool panel, and a star-rating feedback widget.
5. Feedback → `/api/runs/<uuid>/feedback/`; telemetry refresh → `/api/telemetry/`; cascading org dropdowns → `/api/org/children/`; monitoring tiles → `/api/monitoring/`.

## 4. Data flow

- `PlatformAgentRuntime.stream` creates an `AgentRun` (status `started`), emits an `agent_invoked` `TelemetryEvent`, and yields a `status` SSE frame.
- It loads prior turns from `ConversationSession.messages`, picks the adapter, and delegates to `adapter.execute(run, message, history, meta)`. The adapter mutates `meta` with `output_text`, token counts, and `model_id`, and yields `token`/`tool` SSE frames.
- Each tool invocation is persisted as an `AgentToolCall` (input/output payloads, duration) via `_record_tool`.
- On success: the run is updated (`completed`, output, tokens, model, latency), the session is appended with the new user+assistant turn, a `task_completed` telemetry event is written, and a `done` frame is sent.
- On any exception: run marked `failed`, `task_failed` telemetry written, and an `error` frame (containing the exception text) is streamed.
- Feedback recomputes the agent's `satisfaction_score` as the mean of all its runs' ratings.

## 5. Prediction flow (Django Runtime / Anthropic path)

`DjangoRuntimeAdapter.execute` branches on whether `ANTHROPIC_API_KEY` is set (and whether the `anthropic` package imports):

- **Real path** (`_execute_llm`): builds `system` from the agent's prompt, seeds `messages` with history + the new user message, then runs a loop calling `client.messages.create(model, tools=ANTHROPIC_TOOL_SCHEMAS, thinking={"type":"adaptive"})`. On `stop_reason == "tool_use"` it dispatches each tool locally via `_dispatch_tool`, appends the assistant turn and `tool_result` blocks, and loops. On `end_turn` it streams the text tokens and exits. Token usage is accumulated across loop iterations.
- **Fake path** (`_execute_fake`): deterministically runs all three tools (`registry_search`, `risk_classifier`, `deployment_gate_builder`) and composes a templated deployment recommendation. Used when no key/package is present, so the demo always works offline.

The three "tools" are not external services — they query the local `Agent` registry and apply keyword rules in `registry_tools.py`. Other adapters mirror this: OpenAI/Azure use the same tools in OpenAI schema format; Bedrock/HTTP/Echo do a single non-tool call.

---

## 6. Bugs and correctness issues

1. **Tier-4 "human approval" is client-asserted and unrecorded.** `run_agent` only checks `payload.get("human_approved") is True`. The JS sets that flag itself after a browser `confirm()`. Any client can POST `{"human_approved": true}` to bypass the gate. No approver identity is captured, nothing is written to `GovernanceReview`, and there is no audit trail. This is the most serious governance defect.

2. **Cross-user session hijack.** `_resolve_session` looks up `ConversationSession` by `id` and `agent` only — **not** by user. Any authenticated user who supplies another user's `session_id` resumes that conversation, reads its history (fed into the prompt), and appends to it. Conversation history leaks across users.

3. **Feedback has no ownership check.** `submit_feedback` accepts any `run_id` from any logged-in user and recomputes the agent's `satisfaction_score`. Trivial metric manipulation / tampering across runs the user never made.

4. **Governance is decorative — never enforced.** `transition_to(PRODUCTION)` is allowed from `pilot` regardless of any `GovernanceReview` status or `next_review_at`. Pending/rejected reviews do not block promotion.

5. **Version snapshot drops `model_id`.** `Agent._snapshot_version` writes `version`, `system_prompt`, `tool_names` but omits `model_id`, even though `AgentVersion` has that field. Version history is incomplete.

6. **Cost is wrong for most runs.** `AgentRun.cost_usd` only has pricing keys for `claude-opus-4-8` / `claude-sonnet-4-6` / `claude-haiku-4-5`. It returns `0.0` for OpenAI/Azure (`gpt-4o`), Bedrock (e.g. `anthropic.claude-3-5-sonnet-...v2:0`), `fake`, and `echo`. The monitoring "cost" tile is therefore near-zero in any non-Claude/non-exact-match deployment.

7. **Rate limiting is ineffective in production.** `_is_rate_limited` keys on `REMOTE_ADDR` (everyone behind a proxy/NAT shares one bucket; the real client IP in `X-Forwarded-For` is ignored) and uses the default `LocMemCache`, which is per-process — multiple workers each get their own counter, and the check/increment is not atomic (race condition).

8. **Dead/misleading auth-label code.** In `run_agent`, `user_label = request.user.username or _sanitise_user_label(payload.get("user", ...))`. Because the view is `login_required`, `request.user.username` is always truthy, so `_sanitise_user_label` and the client-supplied `user` field are never used. Harmless but misleading.

9. **Stale comment / routing.** `agent_runtime.py` says `"bedrock" is a string value not yet in Platform.choices`, but `Agent.Platform.BEDROCK = "bedrock"` *is* in choices. Routing happens to work (TextChoices are `str` subclasses), but the comment is wrong and the special-cased dict entry is unnecessary.

10. **Blocking I/O inside the SSE generator.** `_emit_tokens` calls `time.sleep` per token and the LLM/REST calls are synchronous with no timeout (Anthropic, OpenAI, Bedrock; only `HttpApiAdapter` sets a 60s timeout). Under the dev server this is fine; under a sync WSGI worker each run ties up a worker for its full duration and a hung provider call hangs the worker indefinitely.

11. **Percentile math is crude.** `p99 = latencies[max(0, int(len*0.99)-1)]` over only the last 20 runs is a rough index, not a real percentile; for small samples it is effectively "near the max." Acceptable for a demo, misleading as a real SLO.

## 7. Missing validation

- No ownership check on `session_id` (see bug 2) or `run_id` (bug 3).
- No allowlist on `Agent.model_id` — whatever string is stored is sent straight to the provider API.
- No cap on conversation length: `ConversationSession.messages` and the history fed to the model grow unbounded → escalating token cost and eventual context-window failures.
- `HttpApiAdapter` POSTs to `agent.endpoint_url` with no URL allowlist/scheme check → SSRF surface if endpoint values are ever influenced outside trusted admins.
- Tool inputs from the model (`risk_tier`) are not range-clamped (defaults to 1 on missing, but an out-of-range int would pass through to `_deployment_checklist`).
- No input/output content validation or moderation beyond length.

## 8. Test gaps

There are **no tests at all** — no `tests.py`, no `pytest`, no fixtures. High-value untested logic:

- The lifecycle state machine (`can_transition_to` / `transition_to`, including the production snapshot side effects).
- `_classify_risk` keyword tiering (band precedence, `break` behavior, "no match" default).
- The Tier-4 gate and the rate limiter in `run_agent`.
- `_resolve_session` (and the cross-user bug above would be caught by a test).
- Adapter contract conformance (each adapter must populate `meta` keys) and the tool-use loop in `_execute_llm`.
- `cost_usd` pricing coverage.
- SSE framing (`RuntimeEvent.to_sse`) and end-to-end streaming.

## 9. AI safety risks

1. **Approval gate is bypassable and unaudited** (bug 1). High-risk agent execution can proceed with a forged flag; no record of who approved or why.

2. **Risk classification is keyword substring matching and advisory-only.** `_classify_risk` matches literal phrases ("customer data", "production system"), so it is trivially evaded by paraphrase, spacing ("p i i"), or synonyms, and it only takes the first phrase per band. More importantly, its output **never gates anything** — the execution Tier-4 check uses the agent's statically-registered `risk_tier`, not the live classification of the user's message. A registered Tier-1 agent handed a request to touch customer data runs unimpeded.

3. **Indirect prompt-injection via the registry.** `registry_search` returns stored agent `name` / `purpose` / `business_unit` text to the model as tool output. Anyone who can register an agent can plant instructions there that reach the model during another user's run.

4. **No PII/secret redaction in persistence or telemetry.** `AgentRun.input_text` / `output_text`, `ConversationSession.messages`, and `TelemetryEvent.payload` are stored in plaintext SQLite. If users paste customer or regulated data (exactly the Tier-4 scenario), it is retained unredacted and surfaced in the admin and dashboards.

5. **Error/internal detail leakage to the client.** The `except` path streams `str(exc)` to the browser and stores it in `output_text`; `HttpApiAdapter` raises errors containing the internal `endpoint_url`. With `DEBUG` defaulting to `True`, this leaks infrastructure detail.

6. **Data egress through the HTTP adapter.** `HttpApiAdapter` ships the system prompt, full history, and user message to an arbitrary external `endpoint_url` using one shared `HTTP_API_BEARER_TOKEN`, with no per-agent credential scoping or destination review.

7. **Secret handling.** A live `ANTHROPIC_API_KEY` sits in `.env` inside a OneDrive-synced folder, and `SECRET_KEY` falls back to a known default (warned but not blocked). Keys for OpenAI/Azure/AWS/HTTP are all loaded into `settings` and reachable from any adapter.

8. **No output guardrails or moderation** on either user input or model output, and no rate/cost ceiling per user beyond the (ineffective) IP limiter.

## 10. Suggested priorities (when you're ready to change code)

1. Make Tier-4 approval server-side: record an approving user + reason in `GovernanceReview`, and require it to exist before execution.
2. Scope `session_id` and `run_id` lookups to the requesting user.
3. Enforce governance/review state in `transition_to(PRODUCTION)`.
4. Add an `Agent.model_id` allowlist, conversation-length caps, and provider call timeouts.
5. Replace IP rate limiting with user-scoped limiting on a shared cache (Redis).
6. Redact/limit what is persisted in runs, sessions, and telemetry; stop streaming raw exceptions.
7. Add a test suite starting with the state machine, risk classifier, the Tier-4 gate, and session resolution.
