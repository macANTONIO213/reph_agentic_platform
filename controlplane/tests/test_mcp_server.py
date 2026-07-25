"""
Phase 1 stretch tests — MCP server (expose our governed tools).

Verifies the JSON-RPC handshake, that only allowlisted tools are listed/callable,
that a call dispatches through the tool registry and is audited, and that the
surface is off + auth-gated by default.
"""
import json

from django.test import TestCase, override_settings

from controlplane.models import AuditLog


@override_settings(
    MCP_SERVER_ENABLED=True,
    MCP_SERVER_TOKENS=["tok-x"],
    MCP_SERVER_EXPOSED_TOOLS=["registry_search"],
)
class McpServerTests(TestCase):
    URL = "/a2a/mcp/"
    AUTH = {"HTTP_AUTHORIZATION": "Bearer tok-x"}

    def _rpc(self, method, params=None, rpc_id=1):
        body = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}
        return self.client.post(self.URL, data=json.dumps(body), content_type="application/json", **self.AUTH)

    def test_initialize(self):
        resp = self._rpc("initialize")
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertIn("protocolVersion", result)
        self.assertIn("tools", result["capabilities"])

    def test_ping(self):
        self.assertEqual(self._rpc("ping").json()["result"], {})

    def test_tools_list_only_exposed(self):
        result = self._rpc("tools/list").json()["result"]
        names = {t["name"] for t in result["tools"]}
        self.assertEqual(names, {"registry_search"})
        # exposed tool advertises an MCP-shaped schema
        self.assertIn("inputSchema", result["tools"][0])

    def test_tools_call_dispatches_and_audits(self):
        resp = self._rpc("tools/call", {"name": "registry_search", "arguments": {"query": "finance"}})
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertIn("content", result)
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertTrue(
            AuditLog.objects.filter(action="mcp_server_tool_call", resource_id="registry_search").exists()
        )

    def test_tools_call_non_exposed_rejected(self):
        resp = self._rpc("tools/call", {"name": "memory_write", "arguments": {}})
        self.assertEqual(resp.json()["error"]["code"], -32602)

    def test_notification_acked_202(self):
        resp = self.client.post(
            self.URL,
            data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            content_type="application/json", **self.AUTH,
        )
        self.assertEqual(resp.status_code, 202)

    def test_unknown_method(self):
        self.assertEqual(self._rpc("resources/list").json()["error"]["code"], -32601)

    def test_auth_required(self):
        resp = self.client.post(
            self.URL, data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)


class McpServerDisabledTests(TestCase):
    @override_settings(MCP_SERVER_ENABLED=False)
    def test_disabled_returns_404(self):
        resp = self.client.get("/a2a/mcp/", HTTP_AUTHORIZATION="Bearer anything")
        self.assertEqual(resp.status_code, 404)
