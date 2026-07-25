"""
Phase 1 step 2 tests — shared SSRF guard + MCP client.

The HTTP layer is mocked (urllib.request.urlopen), so these run offline and
assert normalisation, caching, auth, SSRF rejection, and auditing.
"""
import json
from unittest.mock import patch

from django.test import TestCase

from controlplane.models import AuditLog, RemoteMcpServer
from controlplane.services.interop import mcp_client
from controlplane.services.interop.mcp_client import McpClient, McpClientError
from controlplane.services.interop.net_guard import (
    BlockedDestinationError,
    validate_destination,
)


class _FakeResp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode()

    def read(self, n=None):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_returning(payload, capture=None):
    def _fake(req, timeout=None):
        if capture is not None:
            capture["req"] = req
        return _FakeResp(payload)
    return _fake


# ── net_guard ────────────────────────────────────────────────────────────────

class NetGuardTests(TestCase):
    def test_blocks_dangerous(self):
        for url in (
            "file:///etc/passwd",
            "ftp://example.com/x",
            "http://localhost/x",
            "http://127.0.0.1:8000/admin",
            "http://169.254.169.254/latest/meta-data/",  # link-local (cloud metadata)
            "https:///no-host",
        ):
            with self.assertRaises(BlockedDestinationError, msg=url):
                validate_destination(url)

    def test_allows_hostnames_and_private_ranges(self):
        # Private IPs are permitted by design (enterprise internal endpoints).
        validate_destination("https://mcp.acme.test/rpc")
        validate_destination("http://10.0.0.5:8080/rpc")

    def test_custom_error_class(self):
        with self.assertRaises(McpClientError):
            validate_destination("http://localhost/x", error_cls=McpClientError)


# ── MCP client ───────────────────────────────────────────────────────────────

class McpClientTests(TestCase):
    def setUp(self):
        self.server = RemoteMcpServer.objects.create(
            name="Acme MCP", base_url="https://mcp.acme.test/rpc",
        )

    def test_list_tools_normalises_and_caches(self):
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [
            {"name": "search", "description": "Search", "inputSchema": {"type": "object", "k": 1}},
            {"description": "no name — skipped"},
        ]}}
        with patch("urllib.request.urlopen", _urlopen_returning(payload)):
            tools = mcp_client.list_tools(self.server)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "search")
        self.assertEqual(tools[0]["input_schema"], {"type": "object", "k": 1})
        self.server.refresh_from_db()
        self.assertEqual(self.server.status, RemoteMcpServer.Status.ACTIVE)
        self.assertIsNotNone(self.server.catalog_synced_at)
        self.assertTrue(self.server.is_usable)

    def test_call_tool_normalises_content(self):
        payload = {"result": {"content": [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
            {"type": "image", "data": "..."},
        ], "isError": False}}
        with patch("urllib.request.urlopen", _urlopen_returning(payload)):
            out = mcp_client.call_tool(self.server, "search", {"q": "x"})
        self.assertEqual(out["text"], "hello world")
        self.assertFalse(out["is_error"])
        self.assertEqual(len(out["content"]), 3)

    def test_call_tool_is_error_flag(self):
        payload = {"result": {"content": [{"type": "text", "text": "boom"}], "isError": True}}
        with patch("urllib.request.urlopen", _urlopen_returning(payload)):
            out = mcp_client.call_tool(self.server, "search", {})
        self.assertTrue(out["is_error"])

    def test_rpc_error_raises_and_audits(self):
        payload = {"error": {"code": -32601, "message": "Method not found"}}
        with patch("urllib.request.urlopen", _urlopen_returning(payload)):
            with self.assertRaises(McpClientError):
                mcp_client.call_tool(self.server, "nope", {})
        self.assertTrue(
            AuditLog.objects.filter(action="mcp_tool_call", payload__success=False).exists()
        )

    def test_transport_error_raises_and_audits(self):
        def _boom(req, timeout=None):
            raise OSError("connection refused")
        with patch("urllib.request.urlopen", _boom):
            with self.assertRaises(McpClientError):
                mcp_client.list_tools(self.server)
        self.assertTrue(
            AuditLog.objects.filter(action="mcp_tool_call", payload__success=False).exists()
        )

    def test_ssrf_blocks_localhost_before_network(self):
        bad = RemoteMcpServer.objects.create(name="Local", base_url="http://localhost:9000/rpc")
        called = {"n": 0}

        def _tracker(req, timeout=None):
            called["n"] += 1
            return _FakeResp({"result": {}})

        with patch("urllib.request.urlopen", _tracker):
            with self.assertRaises(McpClientError):
                mcp_client.list_tools(bad)
        self.assertEqual(called["n"], 0)  # never hit the network

    def test_auth_header_from_env_reference(self):
        self.server.auth_ref = "ACME_MCP_TOKEN"
        self.server.save(update_fields=["auth_ref"])
        capture = {}
        payload = {"result": {"tools": []}}
        with patch.dict("os.environ", {"ACME_MCP_TOKEN": "s3cr3t"}, clear=False):
            with patch("urllib.request.urlopen", _urlopen_returning(payload, capture)):
                mcp_client.list_tools(self.server)
        self.assertEqual(capture["req"].headers.get("Authorization"), "Bearer s3cr3t")

    def test_initialize_handshake_threads_session(self):
        class _RespH:
            def __init__(self, payload, session=None):
                self._raw = json.dumps(payload).encode()
                self._session = session

            def read(self, n=None):
                return self._raw

            def getheader(self, k):
                return self._session if k == "Mcp-Session-Id" else None

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        calls = []

        def _fake(req, timeout=None):
            method = json.loads(req.data.decode()).get("method")
            # urllib capitalises header keys: "Mcp-Session-Id" -> "Mcp-session-id"
            calls.append({"method": method, "session": req.headers.get("Mcp-session-id")})
            if method == "initialize":
                return _RespH({"result": {"protocolVersion": "2024-11-05"}}, session="sess-123")
            return _RespH({"result": {"tools": []}})

        with patch("urllib.request.urlopen", _fake):
            mcp_client.list_tools(self.server)

        methods = [c["method"] for c in calls]
        self.assertIn("initialize", methods)            # handshake happened
        self.assertIn("notifications/initialized", methods)  # ack sent (session issued)
        self.assertIn("tools/list", methods)
        # the tools/list request carried the session id from initialize
        tools_call = next(c for c in calls if c["method"] == "tools/list")
        self.assertEqual(tools_call["session"], "sess-123")
