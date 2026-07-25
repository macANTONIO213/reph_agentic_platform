"""
MCP server surface — Phase 1 stretch (expose our governed tools).

Turns the platform into an MCP server: external agents (including MuleSoft Agent
Fabric) can discover and call an **allowlisted** set of our builtin tools over
JSON-RPC 2.0 at ``/a2a/mcp/``.  This is the mirror of the MCP *client* in
``services/interop/mcp_client.py`` — there we consume external tools; here we
provide ours.

Governance:
  - only tools in ``MCP_SERVER_EXPOSED_TOOLS`` are listed or callable;
  - calls dispatch through the same ``tool_registry`` gate (risk tier / binding)
    as any tool, with an agent-less ``ToolContext``;
  - the surface is OFF unless ``MCP_SERVER_ENABLED``; when on, a caller must be a
    session user or present a bearer token from ``MCP_SERVER_TOKENS``;
  - every ``tools/call`` is audited.
"""
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from controlplane.api.interop_auth import bearer_token_matches, session_csrf_failure

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"


def _enabled() -> bool:
    return getattr(settings, "MCP_SERVER_ENABLED", False)


def _exposed_tools() -> set:
    return set(getattr(settings, "MCP_SERVER_EXPOSED_TOOLS", []) or [])


def _authenticate(request):
    if not _enabled():
        return False, JsonResponse({"error": "MCP server is disabled."}, status=404)
    if request.user.is_authenticated:
        return True, None
    tokens = getattr(settings, "MCP_SERVER_TOKENS", []) or []
    if bearer_token_matches(request, tokens):  # constant-time compare
        return True, None
    return False, JsonResponse({"error": "Unauthorized."}, status=401)


def _err(rpc_id, code, message, *, http_status=200):
    return JsonResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}},
        status=http_status,
    )


def _ok(rpc_id, result):
    return JsonResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _list_tools() -> list[dict]:
    from controlplane.services.tools import tool_registry  # import triggers builtin registration
    allow = _exposed_tools()
    tools = []
    for name in tool_registry.names():
        if name not in allow:
            continue
        spec = tool_registry.get(name)
        if spec is None:
            continue
        s = spec.anthropic_schema()  # {name, description, input_schema}
        tools.append({
            "name": s["name"],
            "description": s["description"],
            "inputSchema": s["input_schema"],  # MCP camelCase
        })
    return tools


@csrf_exempt
@require_http_methods(["GET", "POST"])
def mcp_endpoint(request):
    """JSON-RPC 2.0 MCP server. GET returns server info; POST handles methods."""
    ok, err = _authenticate(request)
    if not ok:
        return err
    # Session browser callers must pass CSRF; csrf_exempt is only for bearer-token
    # machine callers (see interop_auth.session_csrf_failure).
    csrf_err = session_csrf_failure(request)
    if csrf_err is not None:
        return JsonResponse({"error": "CSRF verification failed."}, status=403)

    if request.method == "GET":
        return JsonResponse({
            "server": "agentic-platform",
            "protocolVersion": PROTOCOL_VERSION,
            "exposed_tools": sorted(_exposed_tools()),
        })

    try:
        payload = json.loads(request.body) if request.body else {}
    except Exception:
        payload = {}
    method = payload.get("method")
    rpc_id = payload.get("id")
    params = payload.get("params") or {}

    # JSON-RPC notifications carry no id and expect no response body.
    if isinstance(method, str) and method.startswith("notifications/"):
        return HttpResponse(status=202)

    if method == "initialize":
        return _ok(rpc_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "agentic-platform", "version": "1.0"},
        })

    if method == "ping":
        return _ok(rpc_id, {})

    if method == "tools/list":
        return _ok(rpc_id, {"tools": _list_tools()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in _exposed_tools():
            return _err(rpc_id, -32602, f"Tool not exposed: {name}")

        from controlplane.models import AuditLog
        from controlplane.services.tools import tool_registry
        from controlplane.services.tools.registry import ToolContext

        ctx = ToolContext(agent=None, actor="mcp:external", mode="live", message=str(arguments))
        result = tool_registry.dispatch(name, arguments, ctx)
        is_error = isinstance(result, dict) and "error" in result
        AuditLog.objects.create(
            actor="mcp:external", action="mcp_server_tool_call",
            resource_type="Tool", resource_id=str(name),
            payload={"tool": name, "is_error": is_error},
        )
        return _ok(rpc_id, {
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": is_error,
        })

    return _err(rpc_id, -32601, f"Method not supported: {method}")
