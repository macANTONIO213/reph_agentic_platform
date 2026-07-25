"""
Phase 1 step 3 tests — MCP binding bridge + register/sync/bind API.

Verifies that an MCP tool flows through the SAME governance machinery as a
connector tool: sandbox-by-default, dry-run makes no external call, risk/binding
gates apply, and live promotion is approval- and server-state-gated.
"""
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from controlplane.models import (
    Agent, AgentToolBinding, BusinessUnit, DataConnector, RemoteMcpServer,
)
from controlplane.services.tools.bindings import (
    build_mcp_spec, create_mcp_binding, promote_to_live, toolset_for,
)
from controlplane.services.tools.registry import ToolContext, tool_registry


# ── helpers ───────────────────────────────────────────────────────────────────

def _bu(name="Engineering"):
    return BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:8].upper()})[0]


def _agent(slug="mcp-agent", bu=None, tier=2, status=Agent.Status.PILOT):
    return Agent.objects.create(
        name=slug, slug=slug, purpose="p", business_unit=(bu.name if bu else "Engineering"),
        owner="o", technical_owner="o", system_prompt="s", platform="django_runtime",
        status=status, risk_tier=tier, org_unit=bu,
    )


def _server(name="Acme MCP", usable=True, bu=None):
    kw = dict(name=name, base_url="https://mcp.acme.test/rpc", business_unit=bu)
    if usable:
        kw.update(status=RemoteMcpServer.Status.ACTIVE,
                  tool_catalog=[{"name": "search", "description": "Search",
                                 "input_schema": {"type": "object", "required": ["q"],
                                                  "properties": {"q": {"type": "string"}}}}])
    return RemoteMcpServer.objects.create(**kw)


def _user(username, *, role=None, bu=None, staff=False):
    u, _ = User.objects.get_or_create(username=username, defaults={"is_staff": staff})
    if role or bu:
        p = u.profile
        if bu:
            p.business_unit = bu
        if role:
            p.role = role
        p.save()
    return u


# ── binding bridge ──────────────────────────────────────────────────────────────

class McpBindingSpecTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.agent = _agent(bu=self.bu)
        self.server = _server(bu=self.bu)

    def _binding(self, status=AgentToolBinding.Status.SANDBOX):
        return AgentToolBinding.objects.create(
            agent=self.agent, tool_name="search", mcp_server=self.server,
            mcp_tool_name="search", binding_status=status, operation="call",
        )

    def test_sandbox_dry_run_makes_no_call(self):
        spec = build_mcp_spec(self._binding())
        ctx = ToolContext(agent=self.agent, mode="live")  # binding is sandbox → dry-run
        with patch("controlplane.services.interop.mcp_client.call_tool") as call:
            out = spec.handler({"q": "gdpr"}, ctx)
        call.assert_not_called()
        self.assertEqual(out["mode"], "sandbox")
        self.assertEqual(out["mcp_server"], self.server.name)
        self.assertTrue(out["input_valid"])

    def test_dry_run_reports_missing_inputs(self):
        spec = build_mcp_spec(self._binding())
        out = spec.handler({}, ToolContext(agent=self.agent, mode="live"))
        self.assertFalse(out["input_valid"])
        self.assertIn("q", out["missing_inputs"])

    def test_live_binding_calls_mcp_client(self):
        b = self._binding(status=AgentToolBinding.Status.LIVE)
        b.approved_at = timezone.now()
        b.save(update_fields=["approved_at"])
        spec = build_mcp_spec(b)
        with patch("controlplane.services.interop.mcp_client.call_tool",
                   return_value={"text": "hit", "is_error": False}) as call:
            out = spec.handler({"q": "x"}, ToolContext(agent=self.agent, mode="live"))
        call.assert_called_once()
        self.assertEqual(out["text"], "hit")

    def test_sandbox_run_mode_forces_dry_run_even_for_live_binding(self):
        b = self._binding(status=AgentToolBinding.Status.LIVE)
        b.approved_at = timezone.now()
        b.save(update_fields=["approved_at"])
        spec = build_mcp_spec(b)
        with patch("controlplane.services.interop.mcp_client.call_tool") as call:
            out = spec.handler({"q": "x"}, ToolContext(agent=self.agent, mode="sandbox"))
        call.assert_not_called()
        self.assertEqual(out["mode"], "sandbox")

    def test_toolset_for_routes_mcp_and_connector(self):
        self._binding()  # mcp sandbox binding "search"
        connector = DataConnector.objects.create(name="warehouse", connector_type="sql")
        AgentToolBinding.objects.create(
            agent=self.agent, tool_name="warehouse", connector=connector,
            binding_status=AgentToolBinding.Status.SANDBOX, operation="query",
        )
        extra_specs, bindings = toolset_for(self.agent)
        self.assertEqual(set(extra_specs), {"search", "warehouse"})
        # MCP spec dry-run has an "mcp_server" key; connector dry-run has "connector".
        mcp_out = extra_specs["search"].handler({"q": "1"}, ToolContext(agent=self.agent, mode="sandbox"))
        conn_out = extra_specs["warehouse"].handler({"sql": "SELECT 1"}, ToolContext(agent=self.agent, mode="sandbox"))
        self.assertIn("mcp_server", mcp_out)
        self.assertIn("connector", conn_out)

    def test_registry_binding_gate_applies(self):
        b = self._binding()
        spec = build_mcp_spec(b)
        ctx = ToolContext(agent=self.agent, mode="sandbox")
        # requires_binding: unavailable unless the binding is attached to the context.
        err = tool_registry.dispatch("search", {"q": "1"}, ctx, extra_specs={"search": spec})
        self.assertIn("no active binding", err["error"])
        ctx.bindings = {"search": b}
        ok = tool_registry.dispatch("search", {"q": "1"}, ctx, extra_specs={"search": spec})
        self.assertEqual(ok["mode"], "sandbox")


# ── binding creation + live promotion ───────────────────────────────────────────

class McpBindingLifecycleTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.agent = _agent(bu=self.bu)
        self.approver = _user("approver", role="agent_approver", bu=self.bu)

    def test_create_sandbox_when_server_usable(self):
        b = create_mcp_binding(self.agent, _server(bu=self.bu), "search")
        self.assertEqual(b.binding_status, AgentToolBinding.Status.SANDBOX)
        self.assertEqual(b.mcp_tool_name, "search")
        self.assertEqual(b.target_kind, "mcp")

    def test_create_proposed_when_server_not_usable(self):
        b = create_mcp_binding(self.agent, _server(usable=False, bu=self.bu), "search")
        self.assertEqual(b.binding_status, AgentToolBinding.Status.PROPOSED)

    def test_create_is_idempotent(self):
        s = _server(bu=self.bu)
        b1 = create_mcp_binding(self.agent, s, "search")
        b2 = create_mcp_binding(self.agent, s, "search")
        self.assertEqual(b1.id, b2.id)

    def test_promote_live_blocked_when_server_inactive(self):
        s = _server(usable=False, bu=self.bu)  # REGISTERED, no catalog
        b = create_mcp_binding(self.agent, s, "search")
        with self.assertRaises(ValueError):
            promote_to_live(b, approver=self.approver)

    def test_promote_live_succeeds_when_active(self):
        b = create_mcp_binding(self.agent, _server(bu=self.bu), "search")
        promoted = promote_to_live(b, approver=self.approver)
        self.assertEqual(promoted.binding_status, AgentToolBinding.Status.LIVE)
        self.assertTrue(promoted.is_live_authorized())


# ── register / sync / bind API ──────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self, n=None):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class McpApiTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.builder = _user("builder", role="agent_builder", bu=self.bu)
        self.agent = _agent(bu=self.bu)
        self.client.force_login(self.builder)

    def _post(self, url, body):
        return self.client.post(url, data=json.dumps(body), content_type="application/json")

    def test_register_server(self):
        resp = self._post("/api/v1/mcp/servers/", {"name": "Acme", "base_url": "https://mcp.acme.test/rpc"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["status"], RemoteMcpServer.Status.REGISTERED)

    def test_register_rejects_localhost(self):
        resp = self._post("/api/v1/mcp/servers/", {"name": "Local", "base_url": "http://localhost:9000/rpc"})
        self.assertEqual(resp.status_code, 400)

    def test_register_duplicate_conflict(self):
        self._post("/api/v1/mcp/servers/", {"name": "Acme", "base_url": "https://mcp.acme.test/rpc"})
        resp = self._post("/api/v1/mcp/servers/", {"name": "Acme", "base_url": "https://mcp.acme.test/rpc"})
        self.assertEqual(resp.status_code, 409)

    def test_viewer_cannot_register(self):
        self.client.force_login(_user("viewer1", role="viewer", bu=self.bu))
        resp = self._post("/api/v1/mcp/servers/", {"name": "X", "base_url": "https://mcp.acme.test/rpc"})
        self.assertEqual(resp.status_code, 403)

    def test_sync_then_bind_flow(self):
        reg = self._post("/api/v1/mcp/servers/", {"name": "Acme", "base_url": "https://mcp.acme.test/rpc"})
        server_id = reg.json()["id"]
        catalog = {"result": {"tools": [
            {"name": "search", "description": "Search", "inputSchema": {"type": "object"}},
        ]}}
        with patch("urllib.request.urlopen", lambda req, timeout=None: _FakeResp(catalog)):
            sync = self.client.post(f"/api/v1/mcp/servers/{server_id}/sync/")
        self.assertEqual(sync.status_code, 200)
        self.assertEqual(len(sync.json()["tools"]), 1)

        bind = self._post(
            f"/api/v1/agents/{self.agent.id}/mcp-bindings/",
            {"mcp_server_id": server_id, "mcp_tool_name": "search"},
        )
        self.assertEqual(bind.status_code, 201)
        self.assertEqual(bind.json()["binding_status"], AgentToolBinding.Status.SANDBOX)

    def test_bind_unknown_tool_rejected(self):
        reg = self._post("/api/v1/mcp/servers/", {"name": "Acme", "base_url": "https://mcp.acme.test/rpc"})
        resp = self._post(
            f"/api/v1/agents/{self.agent.id}/mcp-bindings/",
            {"mcp_server_id": reg.json()["id"], "mcp_tool_name": "ghost"},
        )
        self.assertEqual(resp.status_code, 400)
