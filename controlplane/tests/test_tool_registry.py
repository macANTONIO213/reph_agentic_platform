"""
Tests for the Tool Registry (Layer 0 of the autonomous agent build).

Covers:
  - per-agent schema exposure via Agent.tool_names
  - empty/missing tool_names → all builtins (backward compatible)
  - unknown tool names skipped in schemas, error on dispatch
  - risk gate (tool risk_tier may not exceed agent risk_tier)
  - binding gate (requires_binding tool unavailable without a binding)
  - dispatch returns handler output and never raises
  - builtins still produce correct results through the registry
"""
from types import SimpleNamespace

from django.test import TestCase

from controlplane.models import Agent
from controlplane.services.tools import tool_registry
from controlplane.services.tools.registry import ToolContext, ToolRegistry, ToolSpec


def _agent(tool_names=None, risk_tier=1):
    """Lightweight stand-in — registry only reads tool_names / risk_tier / id."""
    return SimpleNamespace(
        tool_names=tool_names if tool_names is not None else [],
        risk_tier=risk_tier,
        id="00000000-0000-0000-0000-000000000000",
        org_unit_id=None,
    )


class SchemaExposureTests(TestCase):

    def test_empty_tool_names_exposes_all_builtins(self):
        schemas = tool_registry.schemas_for(_agent(tool_names=[]))
        names = {s["name"] for s in schemas}
        self.assertIn("registry_search", names)
        self.assertEqual(len(schemas), len(tool_registry.names()))

    def test_tool_names_filter_exposed_set(self):
        schemas = tool_registry.schemas_for(_agent(tool_names=["registry_search", "memory_read"]))
        names = {s["name"] for s in schemas}
        self.assertEqual(names, {"registry_search", "memory_read"})

    def test_unknown_tool_names_skipped(self):
        schemas = tool_registry.schemas_for(_agent(tool_names=["sap_connector", "registry_search"]))
        names = {s["name"] for s in schemas}
        self.assertEqual(names, {"registry_search"})  # unknown connector tool skipped

    def test_openai_format(self):
        schemas = tool_registry.schemas_for(_agent(tool_names=["registry_search"]), fmt="openai")
        self.assertEqual(schemas[0]["type"], "function")
        self.assertEqual(schemas[0]["function"]["name"], "registry_search")


class DispatchGateTests(TestCase):

    def setUp(self):
        # Isolated registry so synthetic tools don't leak into the global one.
        self.reg = ToolRegistry()
        self.reg.register(ToolSpec(
            name="echo", description="echo", input_schema={"type": "object", "properties": {}},
            handler=lambda inp, ctx: {"echoed": inp.get("v")}, risk_tier=1,
        ))
        self.reg.register(ToolSpec(
            name="danger", description="high risk", input_schema={"type": "object", "properties": {}},
            handler=lambda inp, ctx: {"ok": True}, risk_tier=4,
        ))
        self.reg.register(ToolSpec(
            name="needs_binding", description="connector", input_schema={"type": "object", "properties": {}},
            handler=lambda inp, ctx: {"ok": True}, risk_tier=1, requires_binding=True,
        ))

    def test_dispatch_runs_handler(self):
        out = self.reg.dispatch("echo", {"v": 7}, ToolContext(agent=_agent(risk_tier=1)))
        self.assertEqual(out, {"echoed": 7})

    def test_unknown_tool_errors(self):
        out = self.reg.dispatch("nope", {}, ToolContext(agent=_agent()))
        self.assertIn("Unknown tool", out["error"])

    def test_risk_gate_blocks_high_tier_tool(self):
        out = self.reg.dispatch("danger", {}, ToolContext(agent=_agent(risk_tier=2)))
        self.assertIn("risk tier", out["error"])

    def test_risk_gate_allows_when_tier_sufficient(self):
        out = self.reg.dispatch("danger", {}, ToolContext(agent=_agent(risk_tier=4)))
        self.assertEqual(out, {"ok": True})

    def test_high_tier_tool_hidden_from_schemas(self):
        names = {s["name"] for s in self.reg.schemas_for(_agent(risk_tier=2))}
        self.assertNotIn("danger", names)

    def test_binding_gate_blocks_without_binding(self):
        out = self.reg.dispatch("needs_binding", {}, ToolContext(agent=_agent()))
        self.assertIn("no active binding", out["error"])

    def test_binding_gate_allows_with_binding(self):
        ctx = ToolContext(agent=_agent(), bindings={"needs_binding": object()})
        self.assertEqual(self.reg.dispatch("needs_binding", {}, ctx), {"ok": True})

    def test_requires_binding_hidden_from_schemas_without_binding(self):
        names = {s["name"] for s in self.reg.schemas_for(_agent())}
        self.assertNotIn("needs_binding", names)

    def test_handler_exception_becomes_error_dict(self):
        self.reg.register(ToolSpec(
            name="boom", description="raises", input_schema={"type": "object", "properties": {}},
            handler=lambda inp, ctx: (_ for _ in ()).throw(RuntimeError("kaboom")),
        ))
        out = self.reg.dispatch("boom", {}, ToolContext(agent=_agent()))
        self.assertIn("kaboom", out["error"])


class BuiltinBehaviourTests(TestCase):
    """The ported builtins still produce correct results through the registry."""

    def test_risk_classifier_via_registry(self):
        out = tool_registry.dispatch(
            "risk_classifier",
            {"query": "agent will write to the production database with customer data"},
            ToolContext(agent=_agent(risk_tier=4)),
        )
        self.assertEqual(out["risk_tier"], 4)

    def test_deployment_gate_via_registry(self):
        out = tool_registry.dispatch(
            "deployment_gate_builder", {"risk_tier": 4}, ToolContext(agent=_agent(risk_tier=4))
        )
        self.assertTrue(any("Compliance review" in c for c in out["controls"]))

    def test_registry_search_via_registry(self):
        Agent.objects.create(
            slug="finance-bot", name="Finance Invoice Bot", kind=Agent.Kind.CUSTOM,
            platform=Agent.Platform.DJANGO, business_unit="Finance", owner="x",
            technical_owner="x", purpose="process invoices", system_prompt="p",
            status=Agent.Status.DRAFT, risk_tier=1,
        )
        out = tool_registry.dispatch(
            "registry_search", {"query": "invoice finance"}, ToolContext(agent=_agent())
        )
        self.assertIn("matches", out)
        self.assertGreaterEqual(len(out["matches"]), 1)
