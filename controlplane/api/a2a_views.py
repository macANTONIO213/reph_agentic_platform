"""
A2A server surface — Phase 1 interop (outbound).

Served under ``/a2a/`` (kept separate from the frozen ``/api/v1/`` contract).  For
external agent/fabric consumers: discover published agents and fetch their cards.
Agent invocation (``message/send``) is wired here as a stub and completed in
Phase 1 step 5, where it will run through ``PlatformAgentRuntime`` so guardrails
and audit apply with no bypass.

Access: the whole surface is OFF unless ``A2A_SERVER_ENABLED`` is set.  When on, a
caller must be either a session-authenticated internal user or present a valid
bearer token from ``A2A_ACCESS_TOKENS``.
"""
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from controlplane.api.interop_auth import bearer_token_matches, session_csrf_failure

logger = logging.getLogger(__name__)


def _authenticate(request):
    """Return (ok, error_response). Gates the whole A2A surface."""
    if not getattr(settings, "A2A_SERVER_ENABLED", False):
        return False, JsonResponse({"error": "A2A server is disabled."}, status=404)
    if request.user.is_authenticated:
        return True, None
    tokens = getattr(settings, "A2A_ACCESS_TOKENS", []) or []
    if bearer_token_matches(request, tokens):  # constant-time compare
        return True, None
    return False, JsonResponse({"error": "Unauthorized."}, status=401)


@require_GET
def discovery(request):
    """GET /a2a/agents/ — list published agent cards."""
    ok, err = _authenticate(request)
    if not ok:
        return err
    from controlplane.models import AgentCard
    cards = [
        c.card_json for c in
        AgentCard.objects.filter(is_published=True).select_related("agent")
    ]
    return JsonResponse({"agents": cards, "count": len(cards)})


@require_GET
def registry(request):
    """
    GET /a2a/registry/ — agent-facing federated discovery.

    Lets an external agent (or the future broker) query the catalog of ANY known
    agent/tool endpoint. Filters: ?kind= &domain= &capability= &q=
    """
    ok, err = _authenticate(request)
    if not ok:
        return err
    from controlplane.services.interop import federation
    entries = federation.search_entries(
        q=(request.GET.get("q") or "").strip(),
        kind=(request.GET.get("kind") or "").strip(),
        domain=(request.GET.get("domain") or "").strip(),
        capability=(request.GET.get("capability") or "").strip(),
    )
    return JsonResponse({
        "entries": [federation.to_public_dict(e) for e in entries],
        "count": len(entries),
    })


@require_GET
def agent_card(request, slug):
    """GET /a2a/agents/<slug>/card/ — one published agent's card."""
    ok, err = _authenticate(request)
    if not ok:
        return err
    from controlplane.models import AgentCard
    card = (
        AgentCard.objects
        .filter(agent__slug=slug, is_published=True)
        .select_related("agent")
        .first()
    )
    if card is None:
        return JsonResponse({"error": "Agent card not found or not published."}, status=404)
    return JsonResponse(card.card_json)


_A2A_STATE = {
    "submitted": "submitted",
    "working": "working",
    "completed": "completed",
    "failed": "failed",
    "canceled": "canceled",
}


def _rpc_error(rpc_id, code, message, *, http_status=200):
    return JsonResponse(
        {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}},
        status=http_status,
    )


def _rpc_result(rpc_id, result):
    return JsonResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _extract_text(message: dict) -> str:
    """Pull the text from an A2A message's parts."""
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    chunks = []
    for p in parts:
        if isinstance(p, dict) and (p.get("kind") in (None, "text")) and p.get("text"):
            chunks.append(p["text"])
    return "".join(chunks)


def _task_to_a2a(task) -> dict:
    """Map an AsyncAgentTask row onto an A2A Task object."""
    state = _A2A_STATE.get(task.state, "unknown")
    status = {"state": state, "timestamp": task.updated_at.isoformat()}
    if task.state == task.State.FAILED and task.error:
        status["message"] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": task.error}],
        }
    obj = {
        "id": str(task.id),
        "contextId": task.context_id or "",
        "kind": "task",
        "status": status,
    }
    if task.state == task.State.COMPLETED:
        obj["artifacts"] = [{
            "artifactId": str(task.id),
            "parts": [{"kind": "text", "text": task.output_text}],
        }]
    return obj


def _caller_label(request) -> str:
    if request.user.is_authenticated:
        return f"a2a:user:{request.user.username}"
    return "a2a:external"


@csrf_exempt
@require_POST
def rpc(request, slug):
    """
    POST /a2a/agents/<slug>/rpc/ — JSON-RPC 2.0 invocation (A2A).

    Supported methods:
      - ``message/send`` → submit a durable AsyncAgentTask and return an A2A Task.
      - ``tasks/get``    → poll a previously submitted task by id.

    Invocation ALWAYS goes through ``agent_tasks.submit`` → ``PlatformAgentRuntime``,
    so guardrails, telemetry, pricing, the AgentRun record and audit all apply.
    This view has no direct adapter access — there is no path that skips the
    governed runtime.
    """
    ok, err = _authenticate(request)
    if not ok:
        return err
    # A session-authenticated browser caller must still pass CSRF (this POST is
    # csrf_exempt only so bearer-token machine callers can reach it). Without
    # this, a malicious page could force message/send with the victim's cookie.
    csrf_err = session_csrf_failure(request)
    if csrf_err is not None:
        return JsonResponse({"error": "CSRF verification failed."}, status=403)
    try:
        payload = json.loads(request.body) if request.body else {}
    except Exception:
        payload = {}
    rpc_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}

    from controlplane.models import AgentCard, AsyncAgentTask, AuditLog
    from controlplane.services.agent_tasks import agent_tasks

    # Only a published agent is invocable over A2A.
    card = (
        AgentCard.objects.filter(agent__slug=slug, is_published=True)
        .select_related("agent").first()
    )
    if card is None:
        return _rpc_error(rpc_id, -32004, "Agent not found or not published.", http_status=404)
    agent = card.agent

    if method == "message/send":
        text = _extract_text(params.get("message") or {})
        if not text:
            return _rpc_error(rpc_id, -32602, "params.message must contain text.")
        context_id = (params.get("message") or {}).get("contextId", "") or params.get("contextId", "")
        task = agent_tasks.submit(
            agent, text,
            submitted_by=_caller_label(request),
            context_id=context_id,
            channel="a2a",
        )
        AuditLog.objects.create(
            actor=_caller_label(request), action="a2a_inbound_invoke",
            resource_type="Agent", resource_id=str(agent.id),
            payload={"slug": slug, "task_id": str(task.id), "state": task.state},
        )
        return _rpc_result(rpc_id, _task_to_a2a(task))

    if method == "tasks/get":
        task_id = params.get("id")
        task = AsyncAgentTask.objects.filter(id=task_id, agent=agent).first() if task_id else None
        if task is None:
            return _rpc_error(rpc_id, -32004, "Task not found.", http_status=404)
        return _rpc_result(rpc_id, _task_to_a2a(task))

    return _rpc_error(rpc_id, -32601, f"Method not supported: {method}")
