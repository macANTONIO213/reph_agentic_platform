"""
MCP client — Phase 1 interop (inbound tools).

A dependency-light JSON-RPC 2.0 client for external MCP (Model Context Protocol)
servers, mirroring the governed style of ``RestConnector``:

  - ``list_tools(server)``  → MCP ``tools/list``; normalises + caches the catalog
                              on the ``RemoteMcpServer`` and marks it ACTIVE.
  - ``call_tool(server, name, arguments)`` → MCP ``tools/call``; normalises the
                              content result to a dict (mirrors connector returns).

Governance/safety, all reused from the connector precedent:
  - every destination is SSRF-guarded via ``interop.net_guard`` before any call;
  - auth is resolved from ``server.auth_ref`` as an **env-var reference**, never a
    stored secret;
  - responses are size-capped and time-limited;
  - every call writes an AuditLog row.

Transport note: this implements the plain JSON-RPC-over-HTTP shape.  The MCP
``initialize`` handshake and streamable-HTTP session negotiation are a later
refinement (isolated to this module so a protocol change is a one-file edit).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

from django.utils import timezone

from controlplane.services.interop.net_guard import validate_destination

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 2_097_152  # 2 MB
_PROTOCOL_VERSION = "2024-11-05"


class McpClientError(RuntimeError):
    """Transport- or protocol-level failure talking to an MCP server."""


class McpClient:
    def __init__(self, server):
        self.server = server

    # ── public API ────────────────────────────────────────────────────────────

    def list_tools(self, *, actor: str = "system") -> list[dict]:
        """Introspect the server, cache the normalised catalog, mark it ACTIVE."""
        from controlplane.models import RemoteMcpServer

        session_id = self._establish_session(actor)
        result, _ = self._rpc("tools/list", {}, actor=actor, session_id=session_id)
        tools = []
        for t in result.get("tools") or []:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            tools.append({
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                # MCP uses camelCase inputSchema; normalise to our snake_case.
                "input_schema": t.get("inputSchema") or t.get("input_schema") or {"type": "object"},
            })

        self.server.tool_catalog = tools
        self.server.status = RemoteMcpServer.Status.ACTIVE
        self.server.catalog_synced_at = timezone.now()
        self.server.save(update_fields=[
            "tool_catalog", "status", "catalog_synced_at", "updated_at",
        ])
        return tools

    def call_tool(self, mcp_tool_name: str, arguments: dict | None, *, actor: str = "agent") -> dict:
        """Invoke a remote tool; return a normalised result dict."""
        session_id = self._establish_session(actor)
        result, _ = self._rpc(
            "tools/call",
            {"name": mcp_tool_name, "arguments": arguments or {}},
            actor=actor, session_id=session_id,
        )
        return self._normalise_call_result(result)

    # ── MCP handshake ───────────────────────────────────────────────────────────

    def _establish_session(self, actor: str) -> str | None:
        """
        Perform the MCP ``initialize`` handshake and return a session id when the
        server issues one (streamable-HTTP session), else None.

        Best-effort and never fatal: a server that doesn't implement ``initialize``
        (plain JSON-RPC) or is unreachable simply yields None, and the caller
        proceeds session-less (the tools request will surface any real error).
        """
        try:
            _result, session_id = self._rpc(
                "initialize",
                {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "RELX-AgentPlatform", "version": "1.0"},
                },
                actor=actor,
            )
        except McpClientError:
            return None
        if session_id:
            # Acknowledge readiness for the session (notification — no response).
            try:
                self._rpc("notifications/initialized", {}, actor=actor,
                          session_id=session_id, is_notification=True)
            except McpClientError:
                pass
        return session_id

    # ── internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_call_result(result: dict) -> dict:
        content = result.get("content") or []
        text_parts = [
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        return {
            "content": content,
            "text": "".join(text_parts),
            "is_error": bool(result.get("isError", False)),
        }

    def _resolve_auth(self) -> str:
        """Resolve the bearer token from the env var named by ``auth_ref``."""
        ref = (self.server.auth_ref or "").strip()
        return os.environ.get(ref, "") if ref else ""

    @staticmethod
    def _session_from(resp) -> str | None:
        """Read the Mcp-Session-Id response header, defensively (tolerant of fakes)."""
        try:
            getter = getattr(resp, "getheader", None)
            if callable(getter):
                return getter("Mcp-Session-Id")
            hdrs = getattr(resp, "headers", None)
            if hdrs is not None and hasattr(hdrs, "get"):
                return hdrs.get("Mcp-Session-Id")
        except Exception:  # noqa: BLE001
            return None
        return None

    def _rpc(self, method: str, params: dict, *, actor: str,
             session_id: str | None = None, is_notification: bool = False) -> tuple:
        """POST a JSON-RPC request/notification. Returns (result, response_session_id)."""
        # SSRF guard BEFORE any network activity.
        validate_destination(self.server.base_url, error_cls=McpClientError)

        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if not is_notification:
            payload["id"] = 1
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "RELX-AgentPlatform/1.0",
        }
        token = self._resolve_auth()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        req = urllib.request.Request(
            self.server.base_url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
                raw = resp.read(_MAX_RESPONSE_BYTES)
                resp_session = self._session_from(resp)
        except Exception as exc:  # noqa: BLE001 — surface as a domain error
            if not is_notification:
                self._audit(method, actor, success=False, error=str(exc))
            raise McpClientError(f"MCP {method} request failed: {exc}") from exc

        # Notifications carry no response body to parse.
        if is_notification:
            return {}, resp_session

        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError) as exc:
            self._audit(method, actor, success=False, error="invalid JSON")
            raise McpClientError(f"MCP {method} returned invalid JSON.") from exc

        if isinstance(envelope, dict) and envelope.get("error"):
            err = envelope["error"]
            self._audit(method, actor, success=False, error=str(err)[:200])
            raise McpClientError(f"MCP {method} error: {err}")

        result = envelope.get("result", {}) if isinstance(envelope, dict) else {}
        self._audit(method, actor, success=True)
        return (result if isinstance(result, dict) else {}), resp_session

    def _audit(self, method: str, actor: str, *, success: bool, error: str = "") -> None:
        try:
            from controlplane.models import AuditLog
            AuditLog.objects.create(
                actor=actor,
                action="mcp_tool_call",
                resource_type="RemoteMcpServer",
                resource_id=str(self.server.id),
                payload={
                    "server": self.server.name,
                    "method": method,
                    "success": success,
                    "error": error[:200],
                },
            )
        except Exception:  # noqa: BLE001 — auditing must never break a call
            pass


# ── module-level convenience wrappers ───────────────────────────────────────────

def list_tools(server, *, actor: str = "system") -> list[dict]:
    return McpClient(server).list_tools(actor=actor)


def call_tool(server, mcp_tool_name: str, arguments: dict | None = None, *, actor: str = "agent") -> dict:
    return McpClient(server).call_tool(mcp_tool_name, arguments, actor=actor)
