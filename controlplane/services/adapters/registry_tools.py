"""
Compatibility facade over the ToolRegistry (see services/tools/).

Historically this module *was* the tool implementation — hardcoded schema lists
plus a mixin of tool methods.  The implementations now live in
``controlplane.services.tools.builtins`` and are exposed via ``tool_registry``.

This module is kept so existing call sites keep working:
  - ``ANTHROPIC_TOOL_SCHEMAS`` / ``OPENAI_TOOL_SCHEMAS`` — the full builtin set.
  - ``RegistryToolsMixin`` — thin shims that build a ToolContext from the adapter
    instance and delegate to ``tool_registry``.

New code should prefer ``tool_registry.schemas_for(agent)`` and
``tool_registry.dispatch(name, inp, ctx)`` directly.
"""
import logging

from controlplane.services.tools import tool_registry
from controlplane.services.tools.registry import ToolContext

logger = logging.getLogger(__name__)

# Full builtin schema sets (all registered tools), kept for backward compatibility.
ANTHROPIC_TOOL_SCHEMAS = tool_registry.anthropic_schemas()
OPENAI_TOOL_SCHEMAS = tool_registry.openai_schemas()


class RegistryToolsMixin:
    """Thin compatibility shims that delegate to the ToolRegistry.

    Adapters mixing this in get ``self.agent`` / ``self.user_label`` and may set
    ``self._workflow_run``; we build a ToolContext from those and dispatch.
    """

    def _tool_ctx(self, message: str = "", run=None) -> ToolContext:
        return ToolContext(
            agent=getattr(self, "agent", None),
            run=run,
            workflow_run=getattr(self, "_workflow_run", None),
            actor=getattr(self, "user_label", "agent"),
            message=message,
        )

    def _registry_search(self, message: str) -> dict:
        return tool_registry.dispatch("registry_search", {"query": message}, self._tool_ctx(message))

    def _classify_risk(self, message: str) -> dict:
        return tool_registry.dispatch("risk_classifier", {"query": message}, self._tool_ctx(message))

    def _deployment_checklist(self, risk_result: dict) -> dict:
        return tool_registry.dispatch(
            "deployment_gate_builder",
            {"risk_tier": risk_result.get("risk_tier", 1)},
            self._tool_ctx(),
        )

    def _retrieve_knowledge(self, query: str, top_k: int = 4) -> dict:
        return tool_registry.dispatch("retrieve_knowledge", {"query": query, "top_k": top_k}, self._tool_ctx(query))

    def _memory_read(self, key: str) -> dict:
        return tool_registry.dispatch("memory_read", {"key": key}, self._tool_ctx())

    def _memory_write(self, key: str, value: str, ttl_seconds: int = 0) -> dict:
        return tool_registry.dispatch(
            "memory_write", {"key": key, "value": value, "ttl_seconds": ttl_seconds}, self._tool_ctx()
        )

    def _delegate_to_agent(self, agent_slug: str, message: str) -> dict:
        return tool_registry.dispatch(
            "delegate_to_agent", {"agent_slug": agent_slug, "message": message}, self._tool_ctx(message)
        )

    def _dispatch_tool(self, name: str, inp: dict, fallback_query: str) -> dict:
        return tool_registry.dispatch(name, inp, self._tool_ctx(fallback_query))
