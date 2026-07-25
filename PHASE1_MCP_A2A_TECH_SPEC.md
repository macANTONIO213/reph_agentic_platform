# Phase 1 Technical Spec — MCP + A2A Interop Layer

> **Scope:** the interop foundation from [AGENT_FABRIC_TRANSFORMATION_STRATEGY.md](AGENT_FABRIC_TRANSFORMATION_STRATEGY.md)
> §5 Phase 1. Makes the platform speak **MCP** (consume external tools) and **A2A / JSON-RPC 2.0
> agent cards** (be discovered and called by other agents), both directions, through the
> existing `GovernanceService` / guardrail / `AuditLog` path.
>
> **Non-goals (later phases):** scanners/auto-discovery (Phase 3), broker/dynamic routing
> (Phase 4), federated multi-org registry UI (Phase 2). This spec delivers the *protocol
> plumbing and its governance*, nothing more.
>
> **Target horizon:** G1–G2 (Aug–Dec 2026). **Prepared:** 25 Jul 2026.

---

## 1. Design principles (non-negotiable)

1. **The gate never moves.** Every inbound MCP tool call routes through the same
   `ToolRegistry.dispatch` risk-tier + binding gate as native tools
   ([registry.py:148](controlplane/services/tools/registry.py)). Every inbound A2A request
   runs the target agent through `PlatformAgentRuntime.stream`
   ([agent_runtime.py:71](controlplane/services/agent_runtime.py)) — i.e. through guardrails,
   telemetry, pricing, and `AuditLog` — with **zero bypass path**.
2. **Sandbox-by-default at the boundary.** An external MCP tool binds as `SANDBOX`/`PROPOSED`
   exactly like a connector today ([bindings.py:172](controlplane/services/tools/bindings.py));
   it never auto-promotes to `LIVE`. This is the differentiation vs Fabric, not an afterthought.
3. **No secrets in models.** MCP server auth follows the `DataConnector` precedent — config
   holds a **reference** to a secret, never the secret. Resolved at call time.
4. **Additive, reversible.** New models + new services + new URL prefixes. No change to the
   `Agent` state machine, `GovernanceService` gates, or existing tool contracts. Two feature
   flags gate the whole surface.

**Feature flags** (settings): `MCP_CLIENT_ENABLED`, `A2A_SERVER_ENABLED` — both default `False`.

---

## 2. Architecture — where the two halves land

```
INBOUND (we are the consumer)                OUTBOUND (we are the provider)
─────────────────────────────               ──────────────────────────────
External MCP server                          External A2A client / MuleSoft Fabric
   │  (tools/list, tools/call)                  │  (GET agent card, JSON-RPC message/send)
   ▼                                            ▼
services/interop/mcp_client.py               api/a2a_views.py  (new URL prefix /a2a/)
   │  normalise → ToolSpec                      │  authenticate + rate-limit (existing middleware)
   ▼                                            ▼
McpToolBinding  (new, extends binding model)  AgentCard projection (services/interop/a2a_cards.py)
   │  proposed → sandbox → live                 │
   ▼                                            ▼
ToolRegistry.dispatch  ── GATE ──►            PlatformAgentRuntime.stream ── guardrails/audit ──►
```

Both halves live under a new package **`controlplane/services/interop/`**. The A2A HTTP surface
lives under a new URL prefix **`/a2a/`** (kept separate from `/api/v1/` so the frozen public API
contract in `ApiVersionHeadersMiddleware` is untouched).

---

## 3. Data model changes (`controlplane/models.py`)

Three additions. All follow existing conventions (UUID pk, `created_at`/`updated_at`, JSON config
without secrets).

### 3.1 `RemoteMcpServer`

A registered external MCP endpoint — the MCP analogue of `DataConnector`.

```python
class RemoteMcpServer(models.Model):
    class Status(models.TextChoices):
        REGISTERED = "registered", "Registered"   # known, not yet introspected
        ACTIVE     = "active",     "Active"        # tools/list succeeded, catalog cached
        DISABLED   = "disabled",   "Disabled"

    id            = UUIDField(pk)
    name          = CharField(unique per BU)
    business_unit = CharField()                    # BU scoping, mirrors DataConnector
    base_url      = URLField()                     # SSRF-guarded on every call (see §4.3)
    transport     = CharField(choices=["http", "sse"], default="http")
    auth_ref      = CharField(blank)               # reference to a secret, NEVER the secret
    status        = CharField(choices=Status, default=REGISTERED)
    source        = CharField(default="manual")    # "manual" | "public_registry" | (Phase 3: "scanner")
    tool_catalog  = JSONField(default=list)         # cached normalised tools/list result
    catalog_synced_at = DateTimeField(null=True)
    is_active     = BooleanField(default=True)
    created_at / updated_at
```

### 3.2 `McpToolBinding` — reuse, don't fork

**Decision:** do **not** create a parallel binding model. Extend `AgentToolBinding`
([models.py:1331](controlplane/models.py)) with a nullable MCP target so the proposed→sandbox→live
lifecycle, `effective_mode`, and `promote_to_live` guards apply unchanged.

Add to `AgentToolBinding`:
```python
mcp_server = models.ForeignKey("RemoteMcpServer", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="tool_bindings")
mcp_tool_name = models.CharField(max_length=120, blank=True)  # tool name on the remote server
```
Invariant (enforced in `clean()` + a check): a binding targets **either** a `connector` **or** an
`mcp_server`, never both, never neither once past `PROPOSED`. `is_live_authorized()` and
`effective_mode()` are inherited verbatim — MCP live calls require LIVE + approval, same as
connectors.

### 3.3 `AgentCard` — projection, not a new source of truth

A denormalised, cacheable A2A card derived from an `Agent`. Stored (not computed per request) so
it can be versioned and served fast.

```python
class AgentCard(models.Model):
    id         = UUIDField(pk)
    agent      = OneToOneField(Agent, related_name="a2a_card")
    card_json  = JSONField()          # full A2A agent-card document (see §5.2)
    is_published = BooleanField(default=False)  # gate: only published agents are discoverable
    version    = CharField()          # tracks Agent.version at publish time
    published_at = DateTimeField(null=True)
    created_at / updated_at
```

**Migration:** one migration, three models + two fields on `AgentToolBinding`. No data
backfill required (all new-nullable).

---

## 4. Inbound — MCP client

### 4.1 New service `controlplane/services/interop/mcp_client.py`

Responsibilities:
- **`list_tools(server: RemoteMcpServer) -> list[dict]`** — JSON-RPC `tools/list` against the
  server; normalise each tool to `{name, description, input_schema}` (MCP tool schema is already
  JSON-Schema, so it maps 1:1 to our `ToolSpec.input_schema`). Cache into
  `server.tool_catalog`, set `status=ACTIVE`, stamp `catalog_synced_at`.
- **`call_tool(server, mcp_tool_name, arguments) -> dict`** — JSON-RPC `tools/call`; return the
  result content normalised to a dict (mirrors connector return shape). Raises on transport error
  — caller converts to `{"error": ...}` so a run never crashes.

### 4.2 Bridge into the registry — extend `bindings.py`

Add an MCP analogue of `build_connector_spec`
([bindings.py:100](controlplane/services/tools/bindings.py)):

```python
def build_mcp_spec(binding) -> ToolSpec:
    def handler(inp, ctx):
        mode = binding.effective_mode(getattr(ctx, "mode", "live"))
        if mode == "live":
            return mcp_client.call_tool(binding.mcp_server, binding.mcp_tool_name, inp)
        return _dry_run_mcp(binding, inp)   # validates args against cached schema, NO call
    return ToolSpec(
        name=binding.tool_name,
        description=_describe(binding),
        input_schema=_mcp_input_schema(binding),   # from server.tool_catalog
        handler=handler,
        risk_tier=getattr(binding.agent, "risk_tier", 1),
        requires_binding=True,
    )
```

`toolset_for` ([bindings.py:136](controlplane/services/tools/bindings.py)) gains a branch: for
each resolved binding, pick `build_mcp_spec` when `binding.mcp_server_id` is set, else
`build_connector_spec`. **No change to `ToolRegistry` or the dispatch gate** — an MCP tool is just
another `ToolSpec` with `requires_binding=True`, so the risk-tier and binding checks at
[registry.py:163-174](controlplane/services/tools/registry.py) apply for free.

### 4.3 Governance & safety for inbound

- **SSRF guard reused.** `mcp_client` validates `base_url` with the same private-IP/loopback
  blocklist the REST connector uses ([rest_connector.py:72](controlplane/services/connectors/rest_connector.py)).
  Factor that check into `services/interop/net_guard.py` and import it in both places (removes
  duplication).
- **Binding creation is sandbox-first.** `create_bindings_from_plan` gains MCP awareness:
  when a plan entry names a tool available on a registered `RemoteMcpServer`, it creates a
  binding as `SANDBOX` (server ACTIVE) or `PROPOSED`, **never** `LIVE` — identical policy to
  connectors ([bindings.py:216](controlplane/services/tools/bindings.py)).
- **Promotion path unchanged.** `promote_to_live` ([bindings.py:263](controlplane/services/tools/bindings.py))
  gets one added guard: for an MCP binding, require `mcp_server.is_active and mcp_server.status == ACTIVE`
  alongside the existing approver + pilot-status + package-boundary checks.
- **Every MCP call is audited.** `call_tool` writes an `AuditLog` row
  (`action="mcp_tool_call"`, resource = binding, payload = server + tool + truncated args),
  mirroring the connector audit trail.

---

## 5. Outbound — MCP server + A2A agent cards

### 5.1 A2A HTTP surface — new file `controlplane/api/a2a_views.py`, prefix `/a2a/`

| Method + path | Purpose |
|---|---|
| `GET /a2a/agents/` | Discovery: list published agent cards (respects BU/tenant scope) |
| `GET /a2a/agents/<slug>/card/` | Return one agent's A2A card JSON (the `.well-known` card) |
| `POST /a2a/agents/<slug>/rpc/` | JSON-RPC 2.0 endpoint — `message/send` invokes the agent |

Wire in a new `controlplane/api/a2a_urls.py` included at `/a2a/` in the project `urls.py`.
Reuse the existing `ApiGlobalRateLimitMiddleware` and add an auth check (token/OIDC) — inbound
A2A callers are external, so authentication is mandatory, not optional.

### 5.2 Card projection — `controlplane/services/interop/a2a_cards.py`

`build_card(agent) -> dict` produces an A2A agent card (aligned to the A2A card spec that
Fabric's scanners normalise to):
```jsonc
{
  "name": "<agent.name>",
  "description": "<agent.purpose>",
  "url": "https://<host>/a2a/agents/<slug>/rpc/",
  "version": "<agent.version>",
  "capabilities": { "streaming": true },
  "skills": [ /* derived from agent.tool_names + live bindings */ ],
  "provider": { "organization": "<agent.business_unit>" },
  "x-governance": {                      // our differentiator, surfaced in the card
    "risk_tier": <agent.risk_tier>,
    "guardrail_level": "<agent.guardrail_level>",
    "governance_level": "<agent.governance_level>",
    "status": "<agent.status>"
  }
}
```
`publish_card(agent)` writes/updates the `AgentCard` row and sets `is_published=True`. **Only
agents at `pilot`/`production` with `is_published` are discoverable** — a draft/sandbox agent is
never exposed externally. Publishing is an explicit action (API below), audited.

### 5.3 Invocation path — the gate holds

`POST /a2a/agents/<slug>/rpc/` with `message/send`:
1. Authenticate caller; resolve target `Agent`; reject if not published/live.
2. Map the A2A message to a prompt and call
   `PlatformAgentRuntime(agent).stream(message)` — **the same runtime path as any run**, so
   guardrails ([guardrails.py]), telemetry, pricing, OTel span, and the `AgentRun` record all
   happen with no special-casing.
3. Stream results back as A2A task events (SSE) or return a completed task object.
4. `AuditLog` row `action="a2a_inbound_invoke"` with caller identity.

**Explicitly:** there is no way to invoke an agent over A2A that skips the runtime — the RPC view
has no direct adapter access. This is the property Fabric's Broker does not guarantee.

### 5.4 Optional: expose our tools as an MCP server

Lower priority within Phase 1 (behind `A2A_SERVER_ENABLED` too). A `GET/POST /a2a/mcp/`
endpoint that exposes selected **builtin** tools (e.g. `registry_search`, `retrieve_knowledge`)
as an MCP server so external agents can consume our governed tools. Deliver only if G1 lands
early; otherwise defer to Phase 2.

---

## 6. API additions (control-plane, under `/api/v1/`)

Add to [api/urls.py](controlplane/api/urls.py) (all role-gated via existing `require_role_json`):

| Path | View | Role |
|---|---|---|
| `mcp/servers/` (GET, POST) | list / register a `RemoteMcpServer` | `agent_builder` |
| `mcp/servers/<id>/sync/` (POST) | run `tools/list`, cache catalog | `agent_builder` |
| `mcp/servers/<id>/` (GET, DELETE) | detail / disable | `agent_builder` |
| `agents/<id>/mcp-bindings/` (POST) | bind an MCP tool to an agent (creates sandbox/proposed) | `agent_builder` |
| `agents/<id>/a2a-card/` (GET) | preview the projected card | `agent_builder` |
| `agents/<id>/a2a-card/publish/` (POST) | publish/unpublish the card | `agent_approver` |

Reuse the promotion endpoint that already exists —
`factory/agents/<id>/tool-bindings/promote/` ([api/urls.py:72](controlplane/api/urls.py)) — for
MCP bindings; `promote_to_live` already handles both target types after §4.3.

---

## 7. Governance invariants (test these explicitly)

| Invariant | Enforced at |
|---|---|
| An MCP tool call from a sandbox/draft agent makes **no external call** | `binding.effective_mode` forces sandbox ([models.py:1401](controlplane/models.py)) |
| An MCP binding cannot go live without approver + pilot status + active server | extended `promote_to_live` (§4.3) |
| MCP tool exceeding agent risk tier is unavailable | `ToolRegistry` gate ([registry.py:163](controlplane/services/tools/registry.py)), unchanged |
| A draft/retired agent is never A2A-discoverable | `publish_card` gate (§5.2) |
| No inbound A2A invocation bypasses guardrails/audit | RPC view calls `stream` only (§5.3) |
| MCP server base_url cannot hit internal network | shared `net_guard` SSRF check (§4.3) |
| Every MCP call / A2A invoke is audited | `AuditLog` rows (§4.3, §5.3) |

---

## 8. Testing plan

New test modules under `controlplane/tests/`:
- `test_mcp_client.py` — normalisation of `tools/list`; `call_tool` error → `{"error": ...}`;
  SSRF rejection; audit row written.
- `test_mcp_bindings.py` — sandbox dry-run makes no call; risk-tier gate; `promote_to_live`
  guards (no approver / draft agent / inactive server all raise); idempotent creation.
- `test_a2a_cards.py` — card shape incl. `x-governance`; unpublished/draft agents absent from
  discovery; publish requires approver role.
- `test_a2a_invoke.py` — inbound RPC runs through `stream` (guardrail block on a poisoned
  prompt returns an A2A error, not a raw model reply); auth required; audit row written.

Follows the existing `test_tool_bindings.py` / `test_inspection_fixes.py` patterns. Target: the
seven §7 invariants each have a dedicated failing-path test.

---

## 9. Sequencing (within G1–G2)

1. ✅ **Models + migration** (§3) — `RemoteMcpServer`, `AgentCard`, MCP fields on
   `AgentToolBinding` + either/or invariant. Migration `0017`. *(done)*
2. ✅ **`net_guard` extraction + `mcp_client`** (§4.1, §4.3) — shared SSRF guard + JSON-RPC
   client (`tools/list` / `tools/call`), audited, size/time-capped. *(done)*
3. ✅ **Binding bridge + registry wiring + inbound API** (§4.2, §6) — `build_mcp_spec`,
   `toolset_for` routing, `create_mcp_binding`, MCP guard on `promote_to_live`, and the
   register/sync/bind API. First end-to-end: a governed agent calls an external MCP tool in
   sandbox. **G1 demo reached.** *(done)*
4. ✅ **A2A cards + discovery + publish** (§5.1–5.2, §6) — `a2a_cards` projection with the
   `x-governance` block, the `/a2a/` discovery surface (feature-flagged + bearer-token gated),
   and the control-plane preview/publish endpoints. *(done)*
5. ✅ **A2A invocation through runtime** (§5.3) — `/a2a/…/rpc/` implements `message/send` +
   `tasks/get`, mapping onto a durable `AsyncAgentTask` run through `PlatformAgentRuntime`
   (no adapter access from the view). A runtime failure maps to an A2A *failed task*, never a
   raw reply; every invoke is audited. **G2 gate reached.** *(done)*
6. ✅ *(stretch)* MCP-server export (§5.4) — allowlisted builtins exposed as an MCP server at
   `/a2a/mcp/` (JSON-RPC: `initialize` / `tools/list` / `tools/call` / `ping`), feature-flagged
   + token-gated, calls dispatched through the `tool_registry` gate and audited. *(done)*

**Prerequisite delivered ahead of Phase 1:** Phase 0 durable execution (Celery adapter +
`AsyncAgentTask`) — the durable long-running task primitive that step 5's A2A `message/send`
invokes.

**G2 exit criteria:** (a) a native agent calls an external MCP tool, sandbox→live with approval,
fully audited; (b) one of our agents is discovered and invoked by an external A2A client, running
through guardrails/audit; both behind feature flags, both with the §7 invariants under test.

---

## 10. Risks & decisions

- **MCP/A2A spec churn.** Both are young. Isolate wire-format handling in
  `services/interop/` behind the normalised `ToolSpec` / `card_json` boundary so a spec bump is a
  one-file change (mirrors the `adapters/` pattern).
- **Auth for inbound A2A.** Decision needed: token vs OIDC/mTLS for external callers. Recommend
  starting with signed bearer tokens per registered consumer, OIDC in Phase 2.
- **Streaming semantics.** A2A supports streamed task updates; our runtime already yields SSE
  ([agent_runtime.py:71](controlplane/services/agent_runtime.py)) — map events rather than
  buffering. Low risk.
- **Public MCP registry curation.** Registering arbitrary public MCP servers is an attack
  surface. Decision: Phase 1 allows manual/register-by-URL only, admin-approved; curated public
  list deferred to Phase 2 with an allowlist.
- **Scope creep toward the broker.** Resist adding routing here — Phase 1 is protocol + governance
  only. Discovery returns a list; it does not *choose*.
```
