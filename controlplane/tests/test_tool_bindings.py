"""
Tests for Tool Bindings (Layer 1 of the autonomous agent build).

Covers:
  - AgentToolBinding lifecycle + effective_mode / is_live_authorized
  - resolve_bindings excludes proposed; includes sandbox/live
  - connector tool spec: sandbox dry-run makes NO external call; live calls connector
  - registry overlay exposes connector tools only when a binding is present
  - promote_to_live guards (connector, approver, package safety boundary)
  - runtime forces sandbox mode for non-live agents
  - end-to-end: a sandbox agent's bound SQL tool dry-runs through the runtime
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from controlplane.models import (
    Agent,
    AgentFactoryPackage,
    AgentToolBinding,
    DataConnector,
)
from controlplane.services.tools import tool_registry
from controlplane.services.tools.registry import ToolContext
from controlplane.services.tools.bindings import (
    build_connector_spec,
    promote_to_live,
    promote_to_sandbox,
    resolve_bindings,
    toolset_for,
)


def _agent(status=Agent.Status.DRAFT, risk_tier=2, tool_names=None, slug="bound-agent"):
    return Agent.objects.create(
        slug=slug, name="Bound Agent", kind=Agent.Kind.CUSTOM,
        platform=Agent.Platform.DJANGO, business_unit="Finance", owner="x",
        technical_owner="x", purpose="p", system_prompt="p",
        status=status, risk_tier=risk_tier, tool_names=tool_names or [],
    )


def _sql_connector():
    return DataConnector.objects.create(
        name="Finance DW", connector_type="sql", config={"url": "postgresql://x/y"},
    )


def _binding(agent, connector=None, status=AgentToolBinding.Status.SANDBOX,
             tool_name="finance_dw", operation="query"):
    return AgentToolBinding.objects.create(
        agent=agent, connector=connector, binding_status=status,
        tool_name=tool_name, operation=operation,
    )


# ── model ─────────────────────────────────────────────────────────────────────

class BindingModelTests(TestCase):

    def test_proposed_not_executable(self):
        b = _binding(_agent(), status=AgentToolBinding.Status.PROPOSED)
        self.assertFalse(b.is_executable)

    def test_sandbox_executable(self):
        b = _binding(_agent())
        self.assertTrue(b.is_executable)

    def test_live_requires_approval_to_be_authorized(self):
        b = _binding(_agent(), _sql_connector(), status=AgentToolBinding.Status.LIVE)
        self.assertFalse(b.is_live_authorized())  # no approved_at yet

    def test_effective_mode_sandbox_run_forces_sandbox(self):
        b = _binding(_agent(), _sql_connector(), status=AgentToolBinding.Status.LIVE)
        b.approved_at = __import__("django.utils.timezone", fromlist=["now"]).now()
        self.assertEqual(b.effective_mode("sandbox"), "sandbox")

    def test_effective_mode_live_when_authorized(self):
        from django.utils import timezone
        b = _binding(_agent(), _sql_connector(), status=AgentToolBinding.Status.LIVE)
        b.approved_at = timezone.now()
        self.assertEqual(b.effective_mode("live"), "live")

    def test_effective_mode_live_status_unapproved_falls_back_to_sandbox(self):
        b = _binding(_agent(), _sql_connector(), status=AgentToolBinding.Status.LIVE)
        self.assertEqual(b.effective_mode("live"), "sandbox")


# ── resolution ──────────────────────────────────────────────────────────────

class ResolveBindingsTests(TestCase):

    def test_excludes_proposed(self):
        agent = _agent()
        _binding(agent, status=AgentToolBinding.Status.PROPOSED, tool_name="a")
        _binding(agent, status=AgentToolBinding.Status.SANDBOX, tool_name="b")
        names = set(resolve_bindings(agent).keys())
        self.assertEqual(names, {"b"})

    def test_toolset_builds_specs_for_executable_bindings(self):
        agent = _agent()
        _binding(agent, _sql_connector(), tool_name="finance_dw")
        specs, bindings = toolset_for(agent, "sandbox")
        self.assertIn("finance_dw", specs)
        self.assertTrue(specs["finance_dw"].requires_binding)
        self.assertIn("finance_dw", bindings)


# ── connector execution ───────────────────────────────────────────────────────

class ConnectorExecutionTests(TestCase):

    def test_sandbox_dry_run_makes_no_external_call(self):
        agent = _agent()
        b = _binding(agent, _sql_connector(), status=AgentToolBinding.Status.SANDBOX)
        spec = build_connector_spec(b)
        ctx = ToolContext(agent=agent, mode="sandbox", bindings={b.tool_name: b})
        with patch("controlplane.services.connectors.sql_connector.SqlConnector.query") as q:
            out = spec.handler({"sql": "SELECT 1"}, ctx)
        q.assert_not_called()
        self.assertEqual(out["mode"], "sandbox")
        self.assertTrue(out["input_valid"])

    def test_sandbox_dry_run_flags_missing_inputs(self):
        agent = _agent()
        b = _binding(agent, _sql_connector(), status=AgentToolBinding.Status.SANDBOX)
        spec = build_connector_spec(b)
        ctx = ToolContext(agent=agent, mode="sandbox", bindings={b.tool_name: b})
        out = spec.handler({}, ctx)  # missing required 'sql'
        self.assertFalse(out["input_valid"])
        self.assertIn("sql", out["missing_inputs"])

    def test_live_binding_calls_connector(self):
        from django.utils import timezone
        agent = _agent(status=Agent.Status.PILOT)
        b = _binding(agent, _sql_connector(), status=AgentToolBinding.Status.LIVE)
        b.approved_at = timezone.now()
        b.save(update_fields=["approved_at"])
        spec = build_connector_spec(b)
        ctx = ToolContext(agent=agent, mode="live", bindings={b.tool_name: b})
        with patch(
            "controlplane.services.connectors.sql_connector.SqlConnector.query",
            return_value={"columns": ["n"], "rows": [[1]], "row_count": 1},
        ) as q:
            out = spec.handler({"sql": "SELECT 1"}, ctx)
        q.assert_called_once()
        self.assertEqual(out["row_count"], 1)

    def test_live_binding_in_sandbox_run_still_dry_runs(self):
        """Even an authorized live binding dry-runs when the RUN mode is sandbox."""
        from django.utils import timezone
        agent = _agent()
        b = _binding(agent, _sql_connector(), status=AgentToolBinding.Status.LIVE)
        b.approved_at = timezone.now()
        b.save(update_fields=["approved_at"])
        spec = build_connector_spec(b)
        ctx = ToolContext(agent=agent, mode="sandbox", bindings={b.tool_name: b})
        with patch("controlplane.services.connectors.sql_connector.SqlConnector.query") as q:
            out = spec.handler({"sql": "SELECT 1"}, ctx)
        q.assert_not_called()
        self.assertEqual(out["mode"], "sandbox")


# ── registry overlay ──────────────────────────────────────────────────────────

class RegistryOverlayTests(TestCase):

    def test_connector_tool_exposed_when_binding_present(self):
        agent = _agent(tool_names=["finance_dw"])
        b = _binding(agent, _sql_connector(), tool_name="finance_dw")
        specs, bindings = toolset_for(agent, "sandbox")
        ctx = ToolContext(agent=agent, mode="sandbox", bindings=bindings)
        names = {s["name"] for s in tool_registry.schemas_for(agent, ctx=ctx, extra_specs=specs)}
        self.assertIn("finance_dw", names)

    def test_connector_tool_hidden_without_binding_in_ctx(self):
        agent = _agent(tool_names=["finance_dw"])
        b = _binding(agent, _sql_connector(), tool_name="finance_dw")
        specs, _ = toolset_for(agent, "sandbox")
        # ctx has NO bindings → requires_binding gate hides the tool
        ctx = ToolContext(agent=agent, mode="sandbox", bindings={})
        names = {s["name"] for s in tool_registry.schemas_for(agent, ctx=ctx, extra_specs=specs)}
        self.assertNotIn("finance_dw", names)

    def test_dispatch_prefers_connector_spec_over_builtin(self):
        agent = _agent()
        b = _binding(agent, _sql_connector(), tool_name="finance_dw")
        specs, bindings = toolset_for(agent, "sandbox")
        ctx = ToolContext(agent=agent, mode="sandbox", bindings=bindings)
        out = tool_registry.dispatch("finance_dw", {"sql": "SELECT 1"}, ctx, extra_specs=specs)
        self.assertEqual(out["mode"], "sandbox")


# ── promotion guards ────────────────────────────────────────────────────────

class PromotionTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="approver", password="x")

    def test_promote_to_sandbox(self):
        b = _binding(_agent(), status=AgentToolBinding.Status.PROPOSED)
        promote_to_sandbox(b, by="alice")
        self.assertEqual(b.binding_status, AgentToolBinding.Status.SANDBOX)

    def test_promote_to_live_requires_connector(self):
        b = _binding(_agent(), connector=None, status=AgentToolBinding.Status.SANDBOX)
        with self.assertRaises(ValueError):
            promote_to_live(b, approver=self.user)

    def test_promote_to_live_requires_approver(self):
        b = _binding(_agent(), _sql_connector())
        with self.assertRaises(ValueError):
            promote_to_live(b, approver=None)

    def test_promote_to_live_respects_package_boundary(self):
        agent = _agent()
        pkg = AgentFactoryPackage.objects.create(
            package_id="AFP-X", safety_boundary={"can_bind_production_tools": False},
        )
        b = _binding(agent, _sql_connector())
        with self.assertRaises(ValueError):
            promote_to_live(b, approver=self.user, package=pkg)

    def test_promote_to_live_succeeds(self):
        agent = _agent(status=Agent.Status.PILOT)
        b = _binding(agent, _sql_connector())
        promote_to_live(b, approver=self.user)
        self.assertEqual(b.binding_status, AgentToolBinding.Status.LIVE)
        self.assertTrue(b.is_live_authorized())


# ── runtime integration ───────────────────────────────────────────────────────

class RuntimeModeTests(TestCase):

    def _run(self, agent):
        from controlplane.services.agent_runtime import PlatformAgentRuntime
        rt = PlatformAgentRuntime(agent=agent, user_label="tester")
        return list(rt.stream("hello"))  # fake engine (no API key)

    def _captured_mode(self, agent):
        # The runtime imports toolset_for locally, so patch it at its source.
        from controlplane.services.tools import bindings as bmod
        with patch.object(bmod, "toolset_for", wraps=bmod.toolset_for) as ts:
            self._run(agent)
        ts.assert_called_once()
        args, kwargs = ts.call_args
        return args[1] if len(args) > 1 else kwargs.get("mode")

    def test_draft_agent_runs_in_sandbox_mode(self):
        agent = _agent(status=Agent.Status.DRAFT)
        _binding(agent, _sql_connector(), tool_name="finance_dw")
        self.assertEqual(self._captured_mode(agent), "sandbox")

    def test_production_agent_runs_in_live_mode(self):
        agent = _agent(status=Agent.Status.PRODUCTION)
        self.assertEqual(self._captured_mode(agent), "live")
