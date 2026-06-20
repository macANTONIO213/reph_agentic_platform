# Autonomous Agent Build — Design (Blueprint → Real Agentic Workflow)

> **Status:** Proposed (design only — no code yet)
> **Author:** Agent Factory working notes
> **Scope:** Turn an approved `AgentBlueprint` / `agent_factory_package` into an
> *executable* agentic workflow, sandbox-first and governance-gated.
> **Target shape:** layered — dynamic per-agent tools first, multi-agent DAG on top.

---

## 1. Goal

Today the Agent Factory produces an `Agent` row whose `tool_names`,
`workflow_steps`, and `tool_binding_plan` are **inert data**. A "build" should
instead produce something that *runs*: an agent (or agent graph) that the
existing runtime can execute autonomously, with real (or sandboxed) tools,
behind the approval/eval gates we already enforce.

We deliberately build in **layers** so each is independently shippable and
testable, and so the riskier capability (live tool/data access) is unlocked
last and only after approval.

---

## 2. Current state (what we reuse, what's missing)

### Already real (reuse as-is)
| Capability | Where | Notes |
|---|---|---|
| Single-agent tool-use loop | `agent_runtime.py` → `adapters/django_runtime.py` | Real Anthropic tool-use loop: model calls tool → result fed back → repeat to `end_turn`. Guardrails, telemetry, cost, OTel already wrap it. |
| Multi-agent DAG executor | `services/orchestrator.py` | Topological `depends_on`, `{{outputs.step.key}}` substitution, retries, shared memory, per-step model routing. |
| Workflow data model | `models.py` `Workflow` / `WorkflowTask` / `WorkflowRun` / `WorkflowTaskRun` | DAG persistence + run history. |
| Connectors | `services/connectors/rest_connector.py`, `sql_connector.py` + `DataConnector` model | Can reach real REST/SQL systems. |
| Model routing | `services/model_router.py` `model_router.select(agent, task=...)` | Per-agent / per-task model choice. |
| Eval gate | `services/eval_service.py` + `EvalSuite`/`EvalCase`/`EvalRun` | Production transition already requires a passing run + approved `GovernanceReview`. |
| Factory lifecycle | `services/factory.py` (`BuildCompiler`), `services/package_ingestor.py` | Blueprint/package → DRAFT `Agent`; proposed (never live) bindings; eval suite generated. |

### The two gaps
1. **Tools are static.** `adapters/registry_tools.py::ANTHROPIC_TOOL_SCHEMAS` is a
   module constant — every agent gets the same 7 hardcoded tools. A blueprint's
   declared tools/bindings are never exposed to the model and have no handlers.
2. **Workflow steps aren't a graph.** `blueprint.workflow_steps` becomes prose in
   the system prompt; it is never compiled into `Workflow` + `WorkflowTask`.

> **Conclusion:** Layer 0 (dynamic tools) is the only genuinely new engine code.
> Layers 2–4 are mostly *assembly* of existing services.

---

## 3. Architecture — the layers

```
Layer 0  Tool Registry + dynamic per-agent tool exposure        [new engine code]
Layer 1  Tool Binding (proposed → sandbox dry-run → live)        [model + gating]
Layer 2  BuildCompiler v2: blueprint → single tool-using agent   [assembly]
Layer 3  WorkflowCompiler: workflow_steps → Workflow DAG         [assembly]
Layer 4  Eval-gated, sandbox-first promotion                     [assembly]
```

Each layer depends only on the ones below it. Layers 0–2 deliver a runnable
single agent; Layer 3 adds multi-agent; Layer 4 closes the governance loop.

---

## 4. Layer 0 — Tool Registry + dynamic tools

**Problem:** the adapter passes one global `tools=[...]` list to every model call.

**Design:** introduce a registry keyed by tool name.

New module `controlplane/services/tools/registry.py`:

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict          # JSON schema (Anthropic shape)
    handler: Callable[[dict, ToolContext], dict]
    risk_tier: int = 1          # min agent risk tier required to use it
    requires_binding: bool = False   # True for connector-backed tools

class ToolRegistry:
    def register(self, spec: ToolSpec) -> None: ...
    def get(self, name: str) -> ToolSpec | None: ...
    def schemas_for(self, agent) -> list[dict]:    # only agent.tool_names ∩ registry
    def dispatch(self, name, inp, ctx) -> dict:    # enforces binding + risk gate
```

- The 7 built-ins in `registry_tools.py` move to `register()` calls (one per
  tool) — behaviour unchanged, just registered instead of hardcoded.
- `ToolContext` carries `agent`, `run`, `workflow_run`, `actor`, and a
  `mode` flag (`"sandbox"` | `"live"`).

**Adapter change** (`django_runtime.py`, mirrored in `openai_adapter.py`):

```python
- tools=ANTHROPIC_TOOL_SCHEMAS,
+ tools=tool_registry.schemas_for(self.agent),
...
- result = self._dispatch_tool(block.name, inp, message)
+ result = tool_registry.dispatch(block.name, inp, self._tool_context(run))
```

So an agent only ever sees the tools its `tool_names` selects, and dispatch is
centrally gated. `Agent.tool_names` is **already populated** by `BuildCompiler`
and `PackageIngestor`, so this immediately makes those values meaningful.

**Backward compatibility:** if `agent.tool_names` is empty, fall back to the
built-in advisory set (preserves current demo behaviour).

---

## 5. Layer 1 — Tool Binding (the safety-critical layer)

A blueprint tool / `tool_binding_plan` entry must resolve to a concrete handler.
Three binding states, mapped from the safety boundary we already enforce:

| Binding state | Meaning | Handler behaviour |
|---|---|---|
| `proposed` | declared, not yet wired | tool **not** exposed to the model (or exposed read-only stub) |
| `sandbox` | wired to a mock/dry-run | handler validates inputs, returns synthetic/mocked result, logs intent — **no external call** |
| `live` | bound to a real `DataConnector` | handler calls `rest_connector`/`sql_connector`; only reachable after approval |

New model `AgentToolBinding` (migration 0015):

```python
class AgentToolBinding(models.Model):
    agent          = FK(Agent, related_name="tool_bindings")
    tool_name      = CharField                      # registry key
    connector      = FK(DataConnector, null=True)   # live target, if any
    binding_status = Choices(proposed|sandbox|live)
    config         = JSONField(default=dict)        # tool-specific params
    approved_by    = FK(User, null=True)
    approved_at    = DateTimeField(null=True)
```

**Gating rule (enforced in `ToolRegistry.dispatch`):**
- `mode="sandbox"` runs ⇒ any binding above `sandbox` is downgraded to `sandbox`.
- `live` binding requires: package `can_bind_production_tools == True`
  **and** an approval record **and** agent status ≥ pilot.
- This is the runtime teeth behind the package rules
  (`can_bind_production_tools=false`, `requires_human_or_policy_approval=true`).

`PackageIngestor` already records bindings as `proposed`/non-live; this layer
gives those records an executable meaning without ever auto-promoting them.

---

## 6. Layer 2 — BuildCompiler v2 (single tool-using agent)

Extend `services/factory.py::BuildCompiler` (and the package path) so a build
*also*:

1. Creates `AgentToolBinding` rows (status `proposed`/`sandbox`) for each
   declared tool — never `live`.
2. Builds a richer system prompt from `workflow_steps` + `guardrails` +
   `decision_policy` (the package ingestor already drafts this).
3. Optionally creates a default `EvalSuite` (package path already does).

Result: an `Agent` in `DRAFT` whose `tool_names` + sandbox bindings make it
**runnable today** via `PlatformAgentRuntime.stream()` — the model autonomously
decides which bound tools to call. No orchestrator needed for the
single-agent case.

`BuildCompiler.build()` stays approval-gated (blueprint must be `APPROVED`,
not `BLOCKED`) — unchanged.

---

## 7. Layer 3 — WorkflowCompiler (multi-agent DAG)

New `services/factory.py::WorkflowCompiler` (or `services/workflow_compiler.py`):

```python
def compile(blueprint_or_package, *, built_by) -> Workflow:
    # 1. Create Workflow (status=DRAFT) tied to the blueprint's business unit.
    # 2. For each step in workflow_steps:
    #      - create a WorkflowTask(step_name=slug(step))
    #      - depends_on = previous step (linear) OR step.depends_on if declared
    #      - input_template chains upstream outputs: "{{outputs.<prev>.text}}"
    #      - assign agent: the built sandbox Agent, or a per-step sub-agent
    #      - system_prompt / model_override from the step if specified
    # 3. Return Workflow; caller runs orchestrator.start()+execute() in sandbox.
```

Decision per step — **single agent vs sub-agent node:**
- If steps share one tool set and persona → one Agent, linear DAG (each task is
  the same agent with a different `input_template`). Simplest.
- If steps need different models/tools/approval → distinct agents per node,
  using the existing `delegate_to_agent` tool or DAG edges.

Reuses orchestrator wholesale: substitution, retries, memory, routing,
`WorkflowRun` history all already exist.

**Heuristic for which compiler to invoke:** `len(workflow_steps) <= 1` or all
steps map to tool calls ⇒ Layer 2 single agent; otherwise Layer 3 DAG. Expose
both as explicit Factory actions so a human can choose.

---

## 8. Layer 4 — Eval-gated, sandbox-first promotion

Wire the lifecycle end to end:

```
blueprint APPROVED
   │  BuildCompiler v2  →  DRAFT Agent + sandbox tool bindings  (+ Workflow if multi-step)
   ▼
SANDBOX RUN
   │  PlatformAgentRuntime / orchestrator.execute() in mode="sandbox"
   │  → WorkflowRun / AgentRun recorded; tools dry-run only
   ▼
EVAL GATE
   │  eval_service.run_suite(suite)   (suite generated from evaluation_pack)
   │  pass_rate ≥ threshold ?
   ▼
APPROVAL
   │  GovernanceReview APPROVED  +  AgentToolBinding promoted proposed→live
   │  (only if package.can_bind_production_tools)
   ▼
PROMOTE
   Agent.transition_to(PILOT→PRODUCTION)  — already requires approved review
```

Nothing here bypasses existing gates; it sequences them. Production binding and
deployment remain blocked until a human/policy approval, exactly as the package
`safety_boundary` requires.

---

## 9. Data model & file summary

**New models** (one migration each, or batched):
- `AgentToolBinding` (Layer 1)

**New modules:**
- `controlplane/services/tools/registry.py` — `ToolRegistry`, `ToolSpec`, `ToolContext`
- `controlplane/services/tools/builtins.py` — register the existing 7 tools + connector-backed tool factory
- `controlplane/services/workflow_compiler.py` — `WorkflowCompiler` (Layer 3)

**Modified:**
- `adapters/django_runtime.py`, `adapters/openai_adapter.py` — use `tool_registry.schemas_for` / `.dispatch`
- `adapters/registry_tools.py` — move schemas into `register()` calls (keep as the builtins source)
- `services/factory.py` — `BuildCompiler` v2 (create bindings, richer prompt); add `WorkflowCompiler` entrypoint
- `services/package_ingestor.py` — emit `AgentToolBinding` rows instead of (or alongside) the JSON plan
- `api/views.py` + `api/urls.py` — endpoints: `POST /factory/blueprints/<id>/build-workflow/`, `POST /agents/<id>/sandbox-run/`, binding promote/approve
- `templates/controlplane/dashboard.html` — "Build & run in sandbox" action; sandbox run viewer; binding status chips
- `controlplane/tests/` — see §11

---

## 10. Sequencing (milestones)

| Milestone | Delivers | Depends on |
|---|---|---|
| **M0** Tool registry | Dynamic per-agent tools; builtins re-registered; adapters switched | — |
| **M1** Bindings + sandbox dispatch | `AgentToolBinding`, sandbox dry-run gating | M0 |
| **M2** BuildCompiler v2 | Approved blueprint → runnable DRAFT agent (single-agent) | M1 |
| **M3** Sandbox run + eval gate | Run agent in sandbox, score against generated suite | M2 |
| **M4** WorkflowCompiler | Multi-step blueprint → Workflow DAG, run via orchestrator | M2 |
| **M5** Promotion path + UI | Binding promote on approval; dashboard actions | M3, M4 |

M0–M3 is a complete vertical slice (single agent runs autonomously in sandbox,
eval-gated). M4–M5 add multi-agent + the promotion UX.

---

## 11. Testing strategy

- **Registry:** schema filtering by `tool_names`; dispatch rejects unbound/over-risk tools; sandbox mode downgrades live bindings.
- **Bindings:** `proposed` never exposed; `live` requires approval + `can_bind_production_tools`; sandbox handlers make no external calls (assert connector not invoked).
- **BuildCompiler v2:** approved blueprint → DRAFT agent + sandbox bindings; still blocks on non-approved/blocked.
- **Sandbox run:** `PlatformAgentRuntime.stream()` with a bound mock tool drives a full tool-use loop using the fake engine (no `ANTHROPIC_API_KEY`).
- **WorkflowCompiler:** N steps → N tasks; `depends_on` wiring; `input_template` chaining; orchestrator runs the compiled DAG to completion.
- **End-to-end:** package ingest → build → sandbox run → eval pass → approve → bindings promote → promote to pilot. Assert production blocked until approval.

---

## 12. Risks & open questions

- **Sandbox fidelity.** Mock handlers must be realistic enough that eval results
  predict live behaviour. Mitigation: record-and-replay against a connector in a
  read-only/staging config rather than pure synthetic mocks where possible.
- **Tool risk vs agent risk.** Need a clear rule for "agent risk_tier N may use
  tools up to tier N." Proposed: `dispatch` rejects tools with
  `risk_tier > agent.risk_tier`.
- **Per-step agents.** Auto-spawning a distinct `Agent` per workflow step
  multiplies governance objects. Prefer one agent + linear DAG unless a step
  declares a different model/tool set.
- **Idempotent rebuilds.** Rebuilding a blueprint should version, not duplicate,
  the agent/workflow/bindings (mirror the existing blueprint `version` pattern).
- **Secrets.** Live bindings reference `DataConnector.config`, which already
  stores env-var references, not raw secrets — keep that invariant.

---

## 13. What this explicitly does **not** change

- No auto-binding of production tools, no auto-deploy — both remain
  approval-gated, consistent with `agent_factory_package` `safety_boundary`.
- No new LLM engine — reuses the existing adapter loop and orchestrator.
- No bypass of `GovernanceReview` or the eval gate for production promotion.
