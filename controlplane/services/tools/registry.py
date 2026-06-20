"""
Tool Registry — Layer 0 of the autonomous agent build (see AUTONOMOUS_BUILD_DESIGN.md).

Replaces the previous static, global tool list with a registry keyed by tool
name.  Each agent is exposed only the tools its ``tool_names`` selects, and every
tool call is dispatched through a single gate that enforces:

  - risk:    a tool's ``risk_tier`` may not exceed the agent's ``risk_tier``
  - binding: a ``requires_binding`` tool is unavailable until a binding is
             attached to the call context (wired in Layer 1)

Adapters build a :class:`ToolContext` per run and call
``tool_registry.schemas_for(agent)`` / ``tool_registry.dispatch(name, inp, ctx)``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# A tool handler takes the model-supplied input dict and the call context,
# and returns a JSON-serialisable result dict.
ToolHandler = Callable[[dict, "ToolContext"], dict]


@dataclass
class ToolContext:
    """Everything a tool handler may need, resolved once per run by the adapter."""
    agent: Any = None
    run: Any = None
    workflow_run: Any = None
    actor: str = "agent"
    message: str = ""           # fallback query when the model omits one
    mode: str = "live"          # "live" | "sandbox" — sandbox forbids external calls
    bindings: dict = field(default_factory=dict)  # tool_name -> AgentToolBinding (Layer 1)


@dataclass
class ToolSpec:
    """A registered tool: its schema, handler, and governance metadata."""
    name: str
    description: str
    input_schema: dict
    handler: ToolHandler
    risk_tier: int = 1
    requires_binding: bool = False

    def anthropic_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRegistry:
    """In-process registry of available tools."""

    def __init__(self):
        self._specs: dict[str, ToolSpec] = {}

    # ── registration ────────────────────────────────────────────────────────
    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            logger.debug("Tool '%s' re-registered (overwriting).", spec.name)
        self._specs[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs.keys())

    # ── exposure ──────────────────────────────────────────────────────────────
    def _selected_names(self, agent) -> list[str]:
        """
        Which registered tools to expose to ``agent``.

        - A non-empty ``agent.tool_names`` selects those tools (unknown names are
          skipped — e.g. connector tools not yet registered until Layer 1).
        - An empty / missing list falls back to all registered builtins, which
          preserves the pre-registry behaviour for advisory agents.
        """
        tool_names = getattr(agent, "tool_names", None)
        if isinstance(tool_names, list) and tool_names:
            selected = []
            for n in tool_names:
                if n in self._specs:
                    selected.append(n)
                else:
                    logger.debug("Agent requests unregistered tool '%s' — skipped.", n)
            return selected
        return self.names()

    def _is_available(self, spec: ToolSpec, agent, ctx: ToolContext | None = None) -> bool:
        agent_tier = getattr(agent, "risk_tier", 1) if agent is not None else 4
        if spec.risk_tier > agent_tier:
            return False
        if spec.requires_binding:
            # Available only when a binding is attached (Layer 1 supplies it).
            return bool(ctx and ctx.bindings.get(spec.name))
        return True

    def schemas_for(
        self,
        agent,
        fmt: str = "anthropic",
        ctx: ToolContext | None = None,
        extra_specs: dict[str, "ToolSpec"] | None = None,
    ) -> list[dict]:
        """
        Return the schema list (Anthropic or OpenAI shape) for ``agent``.

        ``extra_specs`` are per-agent, per-run tools (e.g. connector-backed
        bindings from Layer 1) overlaid on top of the global builtins.
        """
        out: list[dict] = []
        for name in self._selected_names(agent):
            spec = self._specs[name]
            if not self._is_available(spec, agent, ctx):
                continue
            out.append(spec.openai_schema() if fmt == "openai" else spec.anthropic_schema())
        for spec in (extra_specs or {}).values():
            if not self._is_available(spec, agent, ctx):
                continue
            out.append(spec.openai_schema() if fmt == "openai" else spec.anthropic_schema())
        return out

    def anthropic_schemas(self) -> list[dict]:
        return [s.anthropic_schema() for s in self._specs.values()]

    def openai_schemas(self) -> list[dict]:
        return [s.openai_schema() for s in self._specs.values()]

    # ── dispatch ──────────────────────────────────────────────────────────────
    def dispatch(
        self,
        name: str,
        inp: dict,
        ctx: ToolContext,
        extra_specs: dict[str, "ToolSpec"] | None = None,
    ) -> dict:
        """Execute a tool by name, enforcing the risk and binding gates.

        ``extra_specs`` (per-run connector tools) take precedence over builtins.
        """
        spec = (extra_specs or {}).get(name) or self._specs.get(name)
        if spec is None:
            return {"error": f"Unknown tool: {name}"}

        agent = ctx.agent
        agent_tier = getattr(agent, "risk_tier", 1) if agent is not None else 4
        if spec.risk_tier > agent_tier:
            return {
                "error": (
                    f"Tool '{name}' requires risk tier {spec.risk_tier} but agent is "
                    f"tier {agent_tier}."
                )
            }

        if spec.requires_binding and not ctx.bindings.get(name):
            return {"error": f"Tool '{name}' has no active binding (proposed only)."}

        try:
            result = spec.handler(inp or {}, ctx)
            return result if isinstance(result, dict) else {"result": result}
        except Exception as exc:  # noqa: BLE001 — tools must never crash a run
            logger.exception("Tool '%s' raised", name)
            return {"error": f"Tool '{name}' failed: {exc}"}


# Module-level singleton.
tool_registry = ToolRegistry()
