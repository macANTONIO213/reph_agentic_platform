"""
API v1 views — plain Django JsonResponse, no DRF.
All endpoints require an authenticated session (login_required).
"""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Avg, Count, Max, Q, Sum
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from controlplane.api.interop_auth import bearer_token_matches
from controlplane.models import (
    Agent, AgentFeedback, AgentRun, Approval, AuditLog,
    BusinessUnit, DataConnector, Division, EvalCase, EvalRun, EvalSuite,
    KnowledgeDocument, OrgProcess, WorkStream,
)
from controlplane.security import (
    blueprint_business_unit_id as _blueprint_business_unit_id,
    can_access_agent as _can_access_agent,
    can_access_business_unit as _can_access_business_unit,
    can_access_workflow_run as _can_access_workflow_run,
    is_cross_tenant as _is_cross_tenant,
    package_business_unit_id as _package_business_unit_id,
    require_role_json,
    user_business_unit_id as _user_business_unit_id,
)
from controlplane.services.governance import governance, RegistrationError, TransitionError
from controlplane.services.tools.bindings import promote_to_live, promote_to_sandbox
from controlplane.services.workflow_compiler import workflow_compiler
from .aggregations import (
    agent_catalog_telemetry,
    latency_timeseries,
    low_rated_runs,
    monitoring_summary,
    rating_distribution,
    runs_by_agent,
    runs_by_platform,
    runs_timeseries,
)

logger = logging.getLogger(__name__)


def _window(request):
    return request.GET.get("window", "30d")


def _filters(request, enforced_bu_id=None):
    filters = {
        k: v
        for k, v in {
            "agent_id":        request.GET.get("agent"),
            "platform":        request.GET.get("platform"),
            "business_unit_id": enforced_bu_id or request.GET.get("business_unit"),
            "division_id":     request.GET.get("division"),
            "work_stream_id":  request.GET.get("work_stream"),
            "process_id":      request.GET.get("process"),
        }.items()
        if v
    }
    return filters


_RATE_LIMIT_WINDOW = 60
_WORKFLOW_TRIGGER_LIMIT = 5
_WORKFLOW_BUILD_LIMIT = 5


def _require_role(request, *roles: str):
    return require_role_json(request.user, *roles)


def _is_rate_limited(scope: str, limit: int) -> bool:
    key = f"rl:api:{scope}"
    count = cache.get(key, 0)
    if count >= limit:
        return True
    cache.set(key, count + 1, timeout=_RATE_LIMIT_WINDOW)
    return False


# ── Platform engineering maturity ──────────────────────────────────────────────

@require_GET
def platform_health(request):
    return JsonResponse({
        "status": "ok",
        "service": "agentic-controlplane",
        "timestamp": timezone.now().isoformat(),
    })


@login_required
@require_GET
def platform_readiness(request):
    role_error = _require_role(request, "platform_admin")
    if role_error is not None:
        return role_error

    from controlplane.services.platform_maturity import maturity_snapshot

    payload = maturity_snapshot(window_hours=1)
    status = 503 if payload["summary"]["unready"] else 200
    return JsonResponse(
        {
            "status": "ready" if status == 200 else "degraded",
            "summary": payload["summary"],
        },
        status=status,
    )


@login_required
@require_GET
def platform_maturity(request):
    role_error = _require_role(request, "platform_admin")
    if role_error is not None:
        return role_error
    from controlplane.services.platform_maturity import maturity_snapshot

    try:
        window_hours = max(1, int(request.GET.get("window_hours", "24")))
    except ValueError:
        return JsonResponse({"error": "window_hours must be an integer."}, status=400)
    return JsonResponse(maturity_snapshot(window_hours=window_hours))


@login_required
@require_GET
def platform_success_criteria(request):
    role_error = _require_role(request, "platform_admin")
    if role_error is not None:
        return role_error
    from controlplane.services.platform_maturity import enterprise_success_criteria

    try:
        window_hours = max(1, int(request.GET.get("window_hours", "24")))
    except ValueError:
        return JsonResponse({"error": "window_hours must be an integer."}, status=400)
    return JsonResponse(enterprise_success_criteria(window_hours=window_hours))


# ── Agent options (lightweight, for filter dropdowns) ─────────────────────────

@login_required
@require_GET
def agent_options(request):
    """Return [{id, name}] for populating the monitoring agent filter dropdown.
    Accepts the same org hierarchy params as monitoring endpoints."""
    qs = Agent.objects.all().order_by("name")
    if request.GET.get("business_unit"):
        qs = qs.filter(org_unit_id=request.GET["business_unit"])
    if request.GET.get("division"):
        qs = qs.filter(org_division_id=request.GET["division"])
    if request.GET.get("work_stream"):
        qs = qs.filter(org_work_stream_id=request.GET["work_stream"])
    if request.GET.get("process"):
        qs = qs.filter(org_process_id=request.GET["process"])
    if not _is_cross_tenant(request.user):
        qs = qs.filter(org_unit_id=_user_business_unit_id(request.user))
    return JsonResponse({"agents": [{"id": str(a.id), "name": a.name} for a in qs]})


# ── Agent catalog ─────────────────────────────────────────────────────────────

@login_required
@require_GET
def agents_list(request):
    window = _window(request)
    agents = (
        Agent.objects.all()
        .select_related("org_unit", "org_division", "org_work_stream")
        .order_by("name")
    )

    # Optional filters
    if request.GET.get("status"):
        agents = agents.filter(status=request.GET["status"])
    if request.GET.get("platform"):
        agents = agents.filter(platform=request.GET["platform"])
    if request.GET.get("business_unit"):
        agents = agents.filter(org_unit_id=request.GET["business_unit"])
    if request.GET.get("search"):
        q = request.GET["search"]
        agents = agents.filter(Q(name__icontains=q) | Q(owner__icontains=q) | Q(business_unit__icontains=q))

    # Tenant scoping — non-cross-tenant users only see agents in their own BU.
    profile = getattr(request.user, "profile", None)
    if profile is not None and not profile.is_cross_tenant:
        bu_id = profile.business_unit_id
        if bu_id is None:
            agents = agents.none()
        else:
            bu_name = profile.business_unit.name if profile.business_unit else ""
            agents = agents.filter(
                Q(org_unit_id=bu_id) | (Q(org_unit__isnull=True) & Q(business_unit=bu_name))
            )

    telemetry = agent_catalog_telemetry(window)

    data = []
    for a in agents:
        t = telemetry.get(str(a.id), {})
        data.append({
            "id": str(a.id),
            "slug": a.slug,
            "name": a.name,
            "platform": a.platform,
            "platform_display": a.get_platform_display(),
            "status": a.status,
            "status_display": a.get_status_display(),
            "risk_tier": a.risk_tier,
            "owner": a.owner,
            "version": a.version,
            "model_id": a.model_id,
            "org_unit": a.org_unit.name if a.org_unit else a.business_unit,
            "org_division": a.org_division.name if a.org_division else None,
            "org_work_stream": a.org_work_stream.name if a.org_work_stream else None,
            "runs_period": t.get("runs_period", 0),
            "success_rate": t.get("success_rate", 0),
            "avg_latency_ms": t.get("avg_latency_ms", 0),
            "cost_period": t.get("cost_period", 0),
            "avg_satisfaction": t.get("avg_satisfaction", float(a.satisfaction_score)),
            "last_run_at": t.get("last_run_at"),
        })

    return JsonResponse({"agents": data, "window": window})


@login_required
def agent_detail(request, agent_id):
    from django.shortcuts import get_object_or_404
    agent = get_object_or_404(Agent, id=agent_id)
    if not _can_access_agent(request.user, agent):
        return JsonResponse({"error": "You do not have access to this agent."}, status=403)
    if request.method == "GET":
        window = _window(request)

        recent_runs = (
            AgentRun.objects.filter(agent=agent)
            .order_by("-started_at")[:20]
            .values("id", "status", "latency_ms", "input_tokens", "output_tokens",
                    "cost_usd", "model_id", "user_label", "started_at", "completed_at")
        )
        runs_list = [
            {**r, "id": str(r["id"]),
             "started_at": r["started_at"].isoformat(),
             "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
             "cost_usd": float(r["cost_usd"] or 0)}
            for r in recent_runs
        ]

        summary = monitoring_summary(window, agent_id=str(agent.id))

        return JsonResponse({
            "agent": {
                "id": str(agent.id),
                "name": agent.name,
                "platform": agent.platform,
                "platform_display": agent.get_platform_display(),
                "status": agent.status,
                "risk_tier": agent.risk_tier,
                "owner": agent.owner,
                "version": agent.version,
                "model_id": agent.model_id,
                "purpose": agent.purpose,
            },
            "summary": summary,
            "recent_runs": runs_list,
        })

    if request.method == "DELETE":
        role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
        if role_error is not None:
            return role_error
        if agent.status != Agent.Status.DRAFT:
            return JsonResponse({"error": "Only draft agents can be deleted from the catalog."}, status=400)
        if agent.source_blueprints.exists():
            return JsonResponse(
                {"error": "This draft agent is still linked to a blueprint. Delete the blueprint first."},
                status=400,
            )
        agent_id_str = str(agent.id)
        agent_name = agent.name
        agent.delete()
        AuditLog.objects.create(
            actor=request.user.username,
            action="agent_deleted",
            resource_type="Agent",
            resource_id=agent_id_str,
            payload={"agent_name": agent_name, "source": "catalog"},
        )
        return JsonResponse({"deleted": True, "id": agent_id_str}, status=200)

    return JsonResponse({"error": "Method not allowed."}, status=405)


@login_required
@require_GET
def agent_metrics(request, agent_id):
    from django.shortcuts import get_object_or_404
    agent = get_object_or_404(Agent, id=agent_id)
    if not _can_access_agent(request.user, agent):
        return JsonResponse({"error": "You do not have access to this agent."}, status=403)
    window = _window(request)
    bucket = request.GET.get("bucket", "day")
    return JsonResponse({
        "timeseries": runs_timeseries(window, bucket, agent_id=str(agent.id)),
        "latency": latency_timeseries(window, bucket, agent_id=str(agent.id)),
        "ratings": rating_distribution(window, agent_id=str(agent.id)),
    })


# ── Monitoring ────────────────────────────────────────────────────────────────

@login_required
@require_GET
def monitoring_summary_view(request):
    enforced_bu_id = None if _is_cross_tenant(request.user) else _user_business_unit_id(request.user)
    return JsonResponse(monitoring_summary(_window(request), **_filters(request, enforced_bu_id)))


@login_required
@require_GET
def monitoring_timeseries(request):
    window = _window(request)
    bucket = request.GET.get("bucket", "day")
    enforced_bu_id = None if _is_cross_tenant(request.user) else _user_business_unit_id(request.user)
    filters = _filters(request, enforced_bu_id)
    return JsonResponse({
        "runs": runs_timeseries(window, bucket, **filters),
        "latency": latency_timeseries(window, bucket, **filters),
    })


@login_required
@require_GET
def monitoring_breakdowns(request):
    window = _window(request)
    enforced_bu_id = None if _is_cross_tenant(request.user) else _user_business_unit_id(request.user)
    filters = _filters(request, enforced_bu_id)
    return JsonResponse({
        "by_platform": runs_by_platform(window, **filters),
        "by_agent": runs_by_agent(window, **filters),
        "ratings": rating_distribution(window, **filters),
        "low_rated": low_rated_runs(window, **filters),
    })


# ── Org tree ──────────────────────────────────────────────────────────────────

@login_required
@require_GET
def org_tree(request):
    bus = BusinessUnit.objects.filter(is_active=True).prefetch_related(
        "divisions__work_streams"
    ).order_by("name")
    tree = []
    for bu in bus:
        bu_node = {"id": str(bu.id), "name": bu.name, "code": bu.code, "divisions": []}
        for div in bu.divisions.filter(is_active=True).order_by("name"):
            div_node = {"id": str(div.id), "name": div.name, "code": div.code, "work_streams": []}
            for ws in div.work_streams.filter(is_active=True).order_by("name"):
                div_node["work_streams"].append({"id": str(ws.id), "name": ws.name, "code": ws.code})
            bu_node["divisions"].append(div_node)
        tree.append(bu_node)
    return JsonResponse({"tree": tree})


# ── Low-rated runs ────────────────────────────────────────────────────────────

@login_required
@require_GET
def feedback_low_rated(request):
    window = _window(request)
    # Scope to the caller's business unit — low-rated run content (incl. user
    # labels) must not leak across tenants for non-cross-tenant viewers.
    enforced_bu_id = None if _is_cross_tenant(request.user) else _user_business_unit_id(request.user)
    return JsonResponse({"runs": low_rated_runs(window, **_filters(request, enforced_bu_id))})


# ── Governance review decisions ───────────────────────────────────────────────

@login_required
@require_POST
def governance_decide(request, review_id):
    from django.shortcuts import get_object_or_404
    from controlplane.models import GovernanceReview
    if not (request.user.is_staff or request.user.groups.filter(name__in=["agent_approver", "platform_admin"]).exists()):
        return JsonResponse({"error": "Approver role required."}, status=403)

    review = get_object_or_404(GovernanceReview, id=review_id, status=GovernanceReview.Status.PENDING)
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    decision = body.get("decision")
    if decision not in ("approved", "rejected"):
        return JsonResponse({"error": "decision must be 'approved' or 'rejected'."}, status=400)

    review.status = decision
    review.reviewer = request.user.username
    review.notes = body.get("notes", review.notes)
    review.completed_at = timezone.now()
    review.save(update_fields=["status", "reviewer", "notes", "completed_at"])

    AuditLog.objects.create(
        actor=request.user.username,
        action=f"governance_{decision}",
        resource_type="GovernanceReview",
        resource_id=str(review.id),
        payload={"agent": review.agent.name, "decision": decision, "notes": review.notes},
        ip_address=request.META.get("REMOTE_ADDR"),
    )
    return JsonResponse({"status": decision, "review_id": str(review.id)})


# ── Agent status transition ────────────────────────────────────────────────────

@login_required
@require_POST
def agent_transition(request, agent_id):
    from django.shortcuts import get_object_or_404
    if not (request.user.is_staff or request.user.groups.filter(name="platform_admin").exists()):
        return JsonResponse({"error": "Platform admin role required."}, status=403)

    agent = get_object_or_404(Agent, id=agent_id)
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    new_status = body.get("status")
    if not new_status:
        return JsonResponse({"error": "'status' is required."}, status=400)

    try:
        # Route through GovernanceService so ALL production gates are enforced
        # (approved review + valid Approval token + passing eval). Only staff may
        # break-glass past the gates; platform_admins are still gated.
        governance.transition(
            actor=request.user,
            agent=agent,
            to_status=new_status,
            source="api",
            ip=request.META.get("REMOTE_ADDR"),
            bypass=request.user.is_staff,
        )
    except ValueError as e:
        # TransitionError subclasses ValueError; covers both gate + state-machine failures.
        return JsonResponse({"error": str(e)}, status=400)

    # governance.transition() writes its own audit record (agent.transition[.forced]).
    return JsonResponse({"status": agent.status, "agent_id": str(agent.id)})


# ── Approvals (Phase A governance) ────────────────────────────────────────────

def _is_approver(user) -> bool:
    return user.is_staff or user.groups.filter(name="agent_approver").exists()


@login_required
def agent_approvals(request, agent_id):
    from django.shortcuts import get_object_or_404
    agent = get_object_or_404(Agent, id=agent_id)
    if not _can_access_agent(request.user, agent):
        return JsonResponse({"error": "You do not have access to this agent."}, status=403)
    role_error = _require_role(request, "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error

    if request.method == "GET":
        approvals = Approval.objects.filter(agent=agent).order_by("-created_at")[:20]
        return JsonResponse({
            "approvals": [
                {
                    "id": str(a.id),
                    "approved_by": a.approved_by_username,
                    "scope": a.scope,
                    "notes": a.notes,
                    "expires_at": a.expires_at.isoformat(),
                    "is_consumed": a.is_consumed,
                    "is_valid": a.is_valid,
                    "created_at": a.created_at.isoformat(),
                }
                for a in approvals
            ]
        })

    if request.method == "POST":
        if not _is_approver(request.user):
            return JsonResponse(
                {"error": "You need the 'agent_approver' role to create approvals."},
                status=403,
            )
        try:
            body = json.loads(request.body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON."}, status=400)

        ttl_hours = max(1, min(int(body.get("ttl_hours", 8)), 72))
        expires_at = timezone.now() + timedelta(hours=ttl_hours)

        approval = Approval.objects.create(
            agent=agent,
            approved_by=request.user,
            approved_by_username=request.user.username,
            scope=body.get("scope", "tier4_execution"),
            notes=body.get("notes", ""),
            expires_at=expires_at,
        )
        AuditLog.objects.create(
            actor=request.user.username,
            action="create_tier4_approval",
            resource_type="Agent",
            resource_id=str(agent.id),
            payload={
                "approval_id": str(approval.id),
                "expires_at": expires_at.isoformat(),
                "ttl_hours": ttl_hours,
            },
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        return JsonResponse(
            {
                "id": str(approval.id),
                "expires_at": approval.expires_at.isoformat(),
                "message": f"Approval granted for {ttl_hours}h. Next Tier-4 run on '{agent.name}' will consume it.",
            },
            status=201,
        )

    return JsonResponse({"error": "Method not allowed."}, status=405)


# ── Agent registration ────────────────────────────────────────────────────────

@login_required
@require_POST
def agent_register(request):
    """
    POST /api/v1/agents/register/
    Creates a new Agent in status=draft via GovernanceService.
    Requires agent_builder, agent_approver, or platform_admin role (enforced by
    GovernanceService.register_agent); unauthorized callers receive HTTP 403.
    """
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    # Coerce tool_names / data_sources if submitted as comma-separated strings.
    for list_field in ("tool_names", "data_sources"):
        val = body.get(list_field, [])
        if isinstance(val, str):
            body[list_field] = [t.strip() for t in val.split(",") if t.strip()]

    try:
        agent = governance.register_agent(
            actor=request.user,
            data=body,
            source="api",
            ip=request.META.get("REMOTE_ADDR"),
        )
    except RegistrationError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except PermissionError as e:
        return JsonResponse({"error": str(e)}, status=403)
    except Exception as e:
        logger.exception("Unexpected error during agent registration")
        return JsonResponse({"error": "Unexpected server error."}, status=500)

    return JsonResponse(
        {
            "id": str(agent.id),
            "slug": agent.slug,
            "name": agent.name,
            "status": agent.status,
            "risk_tier": agent.risk_tier,
            "message": f"Agent '{agent.name}' registered as draft. Next step: request a governance review.",
        },
        status=201,
    )


# ── Org children (cascading selects for registration form) ──────────────────

@login_required
@require_GET
def org_divisions(request):
    """Return divisions for a business unit (for cascading registration form)."""
    bu_id = request.GET.get("business_unit")
    qs = Division.objects.filter(is_active=True).order_by("name")
    if bu_id:
        qs = qs.filter(business_unit_id=bu_id)
    return JsonResponse({"items": [{"id": str(d.id), "name": d.name} for d in qs]})


@login_required
@require_GET
def org_work_streams(request):
    div_id = request.GET.get("division")
    qs = WorkStream.objects.filter(is_active=True).order_by("name")
    if div_id:
        qs = qs.filter(division_id=div_id)
    return JsonResponse({"items": [{"id": str(w.id), "name": w.name} for w in qs]})


@login_required
@require_GET
def org_processes(request):
    ws_id = request.GET.get("work_stream")
    qs = OrgProcess.objects.filter(is_active=True).order_by("name")
    if ws_id:
        qs = qs.filter(work_stream_id=ws_id)
    return JsonResponse({"items": [{"id": str(p.id), "name": p.name} for p in qs]})


# ── B3: Eval endpoints ────────────────────────────────────────────────────────

@login_required
@require_GET
def eval_suites(request, agent_id):
    """GET /api/v1/agents/<id>/evals/ — list suites + latest run for an agent."""
    from django.shortcuts import get_object_or_404
    agent = get_object_or_404(Agent, id=agent_id)
    if not _can_access_agent(request.user, agent):
        return JsonResponse({"error": "You do not have access to this agent."}, status=403)
    suites = EvalSuite.objects.filter(agent=agent).prefetch_related("runs").order_by("-created_at")
    data = []
    for s in suites:
        latest = s.runs.order_by("-executed_at").first()
        data.append({
            "id": str(s.id),
            "name": s.name,
            "pass_threshold": float(s.pass_threshold),
            "is_active": s.is_active,
            "case_count": s.cases.count(),
            "latest_run": {
                "id": str(latest.id),
                "passed": latest.passed,
                "pass_rate": float(latest.pass_rate),
                "total_cases": latest.total_cases,
                "passed_cases": latest.passed_cases,
                "executed_at": latest.executed_at.isoformat(),
                "status": latest.status,
            } if latest else None,
        })
    return JsonResponse({"suites": data})


@login_required
@require_POST
def eval_run_suite(request, suite_id):
    """POST /api/v1/evals/<suite_id>/run/ — trigger an eval run."""
    from django.shortcuts import get_object_or_404
    from controlplane.services.eval_service import eval_service
    if not (request.user.is_staff or request.user.groups.filter(
            name__in=["platform_admin", "agent_approver"]).exists()):
        # Also allow via UserProfile role
        from controlplane.models import UserProfile as _UP
        role = _UP.objects.filter(user=request.user).values_list("role", flat=True).first()
        if role not in ("platform_admin", "agent_approver"):
            return JsonResponse({"error": "Approver or admin role required to run evals."}, status=403)

    suite = get_object_or_404(EvalSuite, id=suite_id)
    # IDOR guard: an approver in one BU must not run evals against an agent in
    # another BU by guessing the suite UUID.
    if not _can_access_agent(request.user, suite.agent):
        return JsonResponse({"error": "You do not have access to this eval suite."}, status=403)
    run = eval_service.run_suite(suite=suite, triggered_by=request.user.username)
    return JsonResponse({
        "run_id": str(run.id),
        "suite": suite.name,
        "agent": suite.agent.name,
        "passed": run.passed,
        "pass_rate": float(run.pass_rate),
        "total_cases": run.total_cases,
        "passed_cases": run.passed_cases,
        "status": run.status,
        "case_results": run.case_results,
    }, status=201)


@login_required
@require_GET
def eval_run_detail(request, run_id):
    """GET /api/v1/evals/runs/<run_id>/ — fetch a single run result."""
    from django.shortcuts import get_object_or_404
    run = get_object_or_404(EvalRun, id=run_id)
    if not _can_access_agent(request.user, run.suite.agent):
        return JsonResponse({"error": "You do not have access to this eval run."}, status=403)
    return JsonResponse({
        "id": str(run.id),
        "suite": run.suite.name,
        "agent": run.suite.agent.name,
        "passed": run.passed,
        "pass_rate": float(run.pass_rate),
        "total_cases": run.total_cases,
        "passed_cases": run.passed_cases,
        "status": run.status,
        "case_results": run.case_results,
        "error_detail": run.error_detail,
        "executed_at": run.executed_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    })


# ── C1: Semantic agent search ─────────────────────────────────────────────────

@login_required
@require_GET
def semantic_search(request):
    """GET /api/v1/agents/search/?q=<query>&top_k=5"""
    query = request.GET.get("q", "").strip()
    if not query:
        return JsonResponse({"error": "q parameter required."}, status=400)
    top_k = min(int(request.GET.get("top_k", 5)), 20)
    bu_id = request.GET.get("business_unit") or None
    if not _is_cross_tenant(request.user):
        bu_id = str(_user_business_unit_id(request.user)) if _user_business_unit_id(request.user) else None

    from controlplane.services.embeddings import embedding_service
    results = embedding_service.search_agents(query, top_k=top_k, business_unit_id=bu_id)
    return JsonResponse({"query": query, "results": results, "count": len(results)})


# ── C2: Knowledge base ────────────────────────────────────────────────────────

@login_required
@require_GET
def knowledge_documents(request):
    """GET /api/v1/knowledge/ — list documents accessible to the user."""
    qs = KnowledgeDocument.objects.filter(status="ready").order_by("-created_at")
    bu_id = request.GET.get("business_unit")
    if not _is_cross_tenant(request.user):
        bu_id = str(_user_business_unit_id(request.user)) if _user_business_unit_id(request.user) else None
    if bu_id:
        from django.db.models import Q as _Q
        qs = qs.filter(_Q(business_unit_id=bu_id) | _Q(business_unit__isnull=True))
    return JsonResponse({
        "documents": [
            {
                "id": str(d.id),
                "title": d.title,
                "description": d.description,
                "file_type": d.file_type,
                "chunk_count": d.chunk_count,
                "business_unit": d.business_unit.name if d.business_unit else None,
                "uploaded_by": d.uploaded_by,
                "created_at": d.created_at.isoformat(),
            }
            for d in qs[:50]
        ]
    })


@login_required
@require_POST
def knowledge_retrieve(request):
    """POST /api/v1/knowledge/retrieve/ — retrieve passages for a query."""
    try:
        body = json.loads(request.body.decode() or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    query = body.get("query", "").strip()
    if not query:
        return JsonResponse({"error": "query required."}, status=400)

    agent_id = body.get("agent_id")
    top_k = min(int(body.get("top_k", 4)), 8)

    from controlplane.services.rag import rag_service
    from django.shortcuts import get_object_or_404

    agent = get_object_or_404(Agent, id=agent_id) if agent_id else None
    if agent is not None and not _can_access_agent(request.user, agent):
        return JsonResponse({"error": "You do not have access to this agent's knowledge scope."}, status=403)

    class _AnonAgent:
        org_unit_id = _user_business_unit_id(request.user)
    passages = rag_service.retrieve(
        query=query,
        agent=agent or _AnonAgent(),
        top_k=top_k,
    )
    return JsonResponse({"query": query, "passages": passages})


@login_required
@require_POST
def knowledge_ingest(request):
    """POST /api/v1/knowledge/ingest/ — ingest a text document."""
    if not request.user.is_staff:
        from controlplane.models import UserProfile as _UP
        role = _UP.objects.filter(user=request.user).values_list("role", flat=True).first()
        if role not in ("platform_admin", "agent_approver", "agent_builder"):
            return JsonResponse({"error": "Builder role or above required."}, status=403)

    try:
        body = json.loads(request.body.decode() or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    title = body.get("title", "").strip()
    text  = body.get("text", "").strip()
    if not title or not text:
        return JsonResponse({"error": "title and text are required."}, status=400)

    bu_id = body.get("business_unit_id")
    bu = None
    if bu_id:
        bu = BusinessUnit.objects.filter(pk=bu_id).first()
    if not _is_cross_tenant(request.user):
        own_bu_id = _user_business_unit_id(request.user)
        if own_bu_id is None:
            return JsonResponse({"error": "No business unit assigned to your account."}, status=403)
        if bu is not None and str(bu.id) != str(own_bu_id):
            return JsonResponse({"error": "You cannot ingest knowledge into another business unit."}, status=403)
        if bu is None:
            bu = BusinessUnit.objects.filter(pk=own_bu_id).first()

    from controlplane.services.rag import rag_service
    doc = rag_service.ingest_text(
        title=title,
        text=text,
        uploaded_by=request.user.username,
        business_unit=bu,
        description=body.get("description", ""),
        source_url=body.get("source_url", ""),
        file_type=body.get("file_type", "txt"),
    )
    return JsonResponse({
        "id": str(doc.id),
        "title": doc.title,
        "status": doc.status,
        "chunk_count": doc.chunk_count,
    }, status=201)


# ── C3: Data connectors ───────────────────────────────────────────────────────

@login_required
@require_GET
def connectors_list(request):
    """GET /api/v1/connectors/ — list active connectors for the user's BU."""
    qs = DataConnector.objects.filter(is_active=True).order_by("name")
    bu_id = request.GET.get("business_unit")
    if not _is_cross_tenant(request.user):
        bu_id = str(_user_business_unit_id(request.user)) if _user_business_unit_id(request.user) else None
    if bu_id:
        from django.db.models import Q as _Q
        qs = qs.filter(_Q(business_unit_id=bu_id) | _Q(business_unit__isnull=True))
    return JsonResponse({
        "connectors": [
            {
                "id": str(c.id),
                "name": c.name,
                "connector_type": c.connector_type,
                "description": c.description,
                "business_unit": c.business_unit.name if c.business_unit else None,
            }
            for c in qs
        ]
    })


# ── Phase 1: MCP interop (register / sync / bind) ──────────────────────────────

def _mcp_server_dict(s, *, include_catalog: bool = False) -> dict:
    data = {
        "id": str(s.id),
        "name": s.name,
        "base_url": s.base_url,
        "transport": s.transport,
        "status": s.status,
        "source": s.source,
        "business_unit": s.business_unit.name if s.business_unit else None,
        "tool_count": len(s.tool_catalog or []),
        "catalog_synced_at": s.catalog_synced_at.isoformat() if s.catalog_synced_at else None,
        "is_usable": s.is_usable,
    }
    if include_catalog:
        data["tool_catalog"] = s.tool_catalog or []
    return data


def _can_access_mcp_server(user, server) -> bool:
    if _is_cross_tenant(user):
        return True
    if server.business_unit_id is None:
        return True
    return str(server.business_unit_id) == str(_user_business_unit_id(user))


@login_required
@require_http_methods(["GET", "POST"])
def mcp_servers(request):
    """
    GET  /api/v1/mcp/servers/  — list registered MCP servers for the user's BU.
    POST /api/v1/mcp/servers/  — register a server {name, base_url, transport?, auth_ref?, business_unit?}
    """
    from controlplane.models import RemoteMcpServer

    if request.method == "GET":
        qs = RemoteMcpServer.objects.filter(is_active=True).order_by("name")
        if not _is_cross_tenant(request.user):
            bu_id = _user_business_unit_id(request.user)
            if bu_id:
                qs = qs.filter(Q(business_unit_id=bu_id) | Q(business_unit__isnull=True))
        return JsonResponse({"servers": [_mcp_server_dict(s) for s in qs]})

    # POST — register
    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error
    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}
    name = (body.get("name") or "").strip()
    base_url = (body.get("base_url") or "").strip()
    if not name or not base_url:
        return JsonResponse({"error": "name and base_url are required."}, status=400)

    from controlplane.services.interop.net_guard import (
        validate_destination, BlockedDestinationError,
    )
    try:
        validate_destination(base_url)
    except BlockedDestinationError as exc:
        return JsonResponse({"error": f"base_url rejected: {exc}"}, status=400)

    transport = (body.get("transport") or "http").strip().lower()
    if transport not in {"http", "sse"}:
        return JsonResponse({"error": "transport must be 'http' or 'sse'."}, status=400)

    bu = None
    bu_id = body.get("business_unit")
    if not bu_id and not _is_cross_tenant(request.user):
        bu_id = _user_business_unit_id(request.user)
    if bu_id:
        bu = BusinessUnit.objects.filter(id=bu_id).first()

    from django.db import IntegrityError
    try:
        server = RemoteMcpServer.objects.create(
            name=name, base_url=base_url, transport=transport,
            auth_ref=(body.get("auth_ref") or "").strip(),
            business_unit=bu, source="manual", created_by=request.user.username,
        )
    except IntegrityError:
        return JsonResponse(
            {"error": "A server with this name already exists in this business unit."},
            status=409,
        )
    AuditLog.objects.create(
        actor=request.user.username, action="mcp_server_registered",
        resource_type="RemoteMcpServer", resource_id=str(server.id),
        payload={"name": name, "base_url": base_url},
    )
    return JsonResponse(_mcp_server_dict(server), status=201)


@login_required
@require_http_methods(["GET", "DELETE"])
def mcp_server_detail(request, server_id):
    """GET detail (with catalog) / DELETE = disable a registered MCP server."""
    from controlplane.models import RemoteMcpServer
    try:
        server = RemoteMcpServer.objects.get(id=server_id)
    except RemoteMcpServer.DoesNotExist:
        return JsonResponse({"error": "MCP server not found."}, status=404)
    if not _can_access_mcp_server(request.user, server):
        return JsonResponse({"error": "You do not have access to this server."}, status=403)

    if request.method == "GET":
        return JsonResponse(_mcp_server_dict(server, include_catalog=True))

    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error
    server.is_active = False
    server.status = RemoteMcpServer.Status.DISABLED
    server.save(update_fields=["is_active", "status", "updated_at"])
    from controlplane.services.interop import federation
    federation.deactivate_mcp_server(server)
    AuditLog.objects.create(
        actor=request.user.username, action="mcp_server_disabled",
        resource_type="RemoteMcpServer", resource_id=str(server.id),
        payload={"name": server.name},
    )
    return JsonResponse({"status": "disabled", "id": str(server.id)})


@login_required
@require_POST
def mcp_server_sync(request, server_id):
    """POST /api/v1/mcp/servers/<id>/sync/ — introspect + cache the tool catalog."""
    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error
    from controlplane.models import RemoteMcpServer
    try:
        server = RemoteMcpServer.objects.get(id=server_id)
    except RemoteMcpServer.DoesNotExist:
        return JsonResponse({"error": "MCP server not found."}, status=404)
    if not _can_access_mcp_server(request.user, server):
        return JsonResponse({"error": "You do not have access to this server."}, status=403)

    from controlplane.services.interop import mcp_client
    from controlplane.services.interop.mcp_client import McpClientError
    try:
        tools = mcp_client.list_tools(server, actor=request.user.username)
    except McpClientError as exc:
        return JsonResponse({"error": f"Catalog sync failed: {exc}"}, status=502)
    # Project the now-active server into the federated registry (Phase 2).
    from controlplane.services.interop import federation
    federation.project_mcp_server(server)
    return JsonResponse({"server": _mcp_server_dict(server), "tools": tools})


@login_required
@require_POST
def agent_mcp_bindings(request, agent_id):
    """
    POST /api/v1/agents/<id>/mcp-bindings/ — bind an MCP tool to an agent.

    Body: { "mcp_server_id": "...", "mcp_tool_name": "...", "tool_name": "optional" }
    Creates a SANDBOX (or PROPOSED) binding — never live.
    """
    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error
    try:
        agent = Agent.objects.get(id=agent_id)
    except Agent.DoesNotExist:
        return JsonResponse({"error": "Agent not found."}, status=404)
    if not _can_access_agent(request.user, agent):
        return JsonResponse({"error": "You do not have access to this agent."}, status=403)

    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}
    server_id = body.get("mcp_server_id")
    mcp_tool_name = (body.get("mcp_tool_name") or "").strip()
    if not server_id or not mcp_tool_name:
        return JsonResponse({"error": "mcp_server_id and mcp_tool_name are required."}, status=400)

    from controlplane.models import RemoteMcpServer
    try:
        server = RemoteMcpServer.objects.get(id=server_id)
    except RemoteMcpServer.DoesNotExist:
        return JsonResponse({"error": "MCP server not found."}, status=404)
    if not _can_access_mcp_server(request.user, server):
        return JsonResponse({"error": "You do not have access to this server."}, status=403)
    if server.tool_schema(mcp_tool_name) is None:
        return JsonResponse(
            {"error": f"Tool '{mcp_tool_name}' not in server catalog — sync the server first."},
            status=400,
        )

    from controlplane.services.tools.bindings import create_mcp_binding
    binding = create_mcp_binding(
        agent, server, mcp_tool_name,
        tool_name=body.get("tool_name"), created_by=request.user.username,
    )
    AuditLog.objects.create(
        actor=request.user.username, action="mcp_binding_created",
        resource_type="AgentToolBinding", resource_id=str(binding.id),
        payload={
            "agent_id": str(agent.id), "server": server.name,
            "mcp_tool": mcp_tool_name, "status": binding.binding_status,
        },
    )
    return JsonResponse({
        "id": str(binding.id),
        "tool_name": binding.tool_name,
        "mcp_tool_name": binding.mcp_tool_name,
        "binding_status": binding.binding_status,
        "server": server.name,
    }, status=201)


# ── Phase 1: A2A card preview / publish ────────────────────────────────────────

@login_required
@require_GET
def agent_a2a_card_preview(request, agent_id):
    """GET /api/v1/agents/<id>/a2a-card/ — preview the projected card + publish state."""
    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error
    try:
        agent = Agent.objects.get(id=agent_id)
    except Agent.DoesNotExist:
        return JsonResponse({"error": "Agent not found."}, status=404)
    if not _can_access_agent(request.user, agent):
        return JsonResponse({"error": "You do not have access to this agent."}, status=403)

    from controlplane.services.interop.a2a_cards import build_card
    base_url = getattr(settings, "A2A_PUBLIC_BASE_URL", "") or request.build_absolute_uri("/")
    card = build_card(agent, base_url=base_url)
    existing = getattr(agent, "a2a_card", None)
    return JsonResponse({
        "card": card,
        "is_published": bool(existing and existing.is_published),
        "publishable": agent.status in {Agent.Status.PILOT, Agent.Status.PRODUCTION},
    })


@login_required
@require_POST
def agent_a2a_card_publish(request, agent_id):
    """
    POST /api/v1/agents/<id>/a2a-card/publish/ — publish or unpublish the card.

    Body: { "publish": true | false }  (publishing requires agent_approver)
    """
    role_error = _require_role(request, "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error
    try:
        agent = Agent.objects.get(id=agent_id)
    except Agent.DoesNotExist:
        return JsonResponse({"error": "Agent not found."}, status=404)
    if not _can_access_agent(request.user, agent):
        return JsonResponse({"error": "You do not have access to this agent."}, status=403)

    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}
    publish = bool(body.get("publish", True))

    from controlplane.services.interop.a2a_cards import (
        publish_card, unpublish_card, CardPublishError,
    )
    if publish:
        base_url = getattr(settings, "A2A_PUBLIC_BASE_URL", "") or request.build_absolute_uri("/")
        try:
            card = publish_card(agent, base_url=base_url, by=request.user.username)
        except CardPublishError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"status": "published", "card": card.card_json})

    unpublish_card(agent, by=request.user.username)
    return JsonResponse({"status": "unpublished"})


# ── Phase 2: Federated registry (discovery catalog) ────────────────────────────

def _registry_entry_dict(e, *, include_card: bool = False) -> dict:
    d = {
        "id": str(e.id),
        "kind": e.kind,
        "identifier": e.identifier,
        "name": e.name,
        "description": e.description,
        "protocol": e.protocol,
        "endpoint_url": e.endpoint_url,
        "domain": e.domain,
        "provider_org": e.provider_org,
        "capabilities": e.capabilities,
        "governance": e.governance,
        "visibility": e.visibility,
        "review_status": e.review_status,
        "source": e.source,
        "is_active": e.is_active,
        "last_synced_at": e.last_synced_at.isoformat() if e.last_synced_at else None,
    }
    if include_card:
        d["card_json"] = e.card_json
    return d


@login_required
@require_GET
def registry_list(request):
    """
    GET /api/v1/registry/ — the federated catalog of any agent/tool endpoint.

    Filters: ?kind= &domain= &visibility= &capability= &q= &review_status=
    (default review_status=approved; pass 'discovered' to review scan results,
    or 'all' for everything).
    """
    from controlplane.services.interop import federation
    entries = federation.search_entries(
        q=(request.GET.get("q") or "").strip(),
        kind=(request.GET.get("kind") or "").strip(),
        domain=(request.GET.get("domain") or "").strip(),
        capability=(request.GET.get("capability") or "").strip(),
        visibility=(request.GET.get("visibility") or "").strip(),
        review_status=(request.GET.get("review_status") or "approved").strip(),
    )
    return JsonResponse({
        "entries": [_registry_entry_dict(e) for e in entries],
        "count": len(entries),
        "total": len(entries),
    })


@login_required
@require_http_methods(["GET", "DELETE"])
def registry_detail(request, entry_id):
    """GET one catalog entry (with card) / DELETE = deactivate it."""
    from controlplane.models import RegistryEntry
    try:
        e = RegistryEntry.objects.get(id=entry_id)
    except RegistryEntry.DoesNotExist:
        return JsonResponse({"error": "Registry entry not found."}, status=404)

    if request.method == "GET":
        return JsonResponse(_registry_entry_dict(e, include_card=True))

    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error
    e.is_active = False
    e.save(update_fields=["is_active", "updated_at"])
    AuditLog.objects.create(
        actor=request.user.username, action="registry_entry_deactivated",
        resource_type="RegistryEntry", resource_id=str(e.id),
        payload={"kind": e.kind, "identifier": e.identifier},
    )
    # Note: a projected entry (first-party agent / MCP server) re-appears on the
    # next sync while its source is still published/active — deactivation is most
    # useful for manually-registered external entries.
    return JsonResponse({"status": "deactivated", "id": str(e.id)})


@login_required
@require_POST
def registry_register_external(request):
    """
    POST /api/v1/registry/external/ — register an external A2A agent by card URL.

    Body: { "card_url": "...", "domain": "optional", "visibility": "private|public" }
    """
    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error
    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}
    card_url = (body.get("card_url") or "").strip()
    if not card_url:
        return JsonResponse({"error": "card_url is required."}, status=400)
    visibility = (body.get("visibility") or "private").strip().lower()
    if visibility not in {"private", "public"}:
        return JsonResponse({"error": "visibility must be 'private' or 'public'."}, status=400)

    from controlplane.services.interop import federation
    from controlplane.services.interop.a2a_client import A2AClientError
    try:
        entry = federation.register_external_agent(
            card_url, domain=(body.get("domain") or "").strip(),
            visibility=visibility, by=request.user.username,
        )
    except A2AClientError as exc:
        return JsonResponse({"error": f"Could not register agent: {exc}"}, status=400)
    return JsonResponse(_registry_entry_dict(entry, include_card=True), status=201)


@login_required
@require_POST
def registry_sync(request):
    """POST /api/v1/registry/sync/ — backfill the catalog from current sources."""
    role_error = _require_role(request, "platform_admin")
    if role_error is not None:
        return role_error
    from controlplane.services.interop import federation
    result = federation.sync_all()
    AuditLog.objects.create(
        actor=request.user.username, action="registry_synced",
        resource_type="RegistryEntry", resource_id="all", payload=result,
    )
    return JsonResponse({"status": "synced", **result})


# ── Phase 3: Scanners (auto-discovery) ─────────────────────────────────────────

@login_required
@require_GET
def scanners_list(request):
    """GET /api/v1/scanners/ — available scanner platforms."""
    from controlplane.services.scanners import service as scanner_service
    return JsonResponse({"platforms": scanner_service.available_platforms()})


@login_required
@require_POST
def scanner_scan(request, platform):
    """POST /api/v1/scanners/<platform>/scan/ — crawl a platform into the registry."""
    role_error = _require_role(request, "platform_admin")
    if role_error is not None:
        return role_error
    from controlplane.services.scanners import service as scanner_service
    from controlplane.services.scanners.base import ScannerError
    try:
        result = scanner_service.run_scan(platform, by=request.user.username)
    except ScannerError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"status": "scanned", **result})


@login_required
@require_POST
def scanner_scan_all(request):
    """POST /api/v1/scanners/scan-all/ — run every registered scanner (partial-tolerant)."""
    role_error = _require_role(request, "platform_admin")
    if role_error is not None:
        return role_error
    from controlplane.services.scanners import service as scanner_service
    result = scanner_service.run_all_scans(by=request.user.username)
    return JsonResponse({"status": "scanned", **result})


@login_required
@require_POST
def registry_approve(request, entry_id):
    """
    POST /api/v1/registry/<id>/approve/ — approve/reject a discovered catalog entry.

    Body: { "status": "approved" | "rejected" }  (requires agent_approver)
    """
    role_error = _require_role(request, "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error
    from controlplane.models import RegistryEntry
    try:
        entry = RegistryEntry.objects.get(id=entry_id)
    except RegistryEntry.DoesNotExist:
        return JsonResponse({"error": "Registry entry not found."}, status=404)
    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}
    status = (body.get("status") or "approved").strip().lower()
    if status not in {"approved", "rejected"}:
        return JsonResponse({"error": "status must be 'approved' or 'rejected'."}, status=400)

    from controlplane.services.interop import federation
    federation.set_review_status(entry, status, by=request.user.username)
    return JsonResponse({"id": str(entry.id), "review_status": status})


# ── Phase 4: Broker (intent → agent routing) ───────────────────────────────────

@login_required
@require_POST
def broker_route(request):
    """POST /api/v1/broker/route — routing decision + ranked candidates (no execution)."""
    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}
    intent = (body.get("intent") or "").strip()
    if not intent:
        return JsonResponse({"error": "intent is required."}, status=400)
    from controlplane.services.interop import broker
    return JsonResponse(broker.route(intent, domain=(body.get("domain") or "").strip()))


@login_required
@require_POST
def broker_execute(request):
    """
    POST /api/v1/broker/execute — route to the best first-party agent and run it
    through the governed runtime. Returns the routing decision + resulting task.
    """
    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error
    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}
    intent = (body.get("intent") or "").strip()
    if not intent:
        return JsonResponse({"error": "intent is required."}, status=400)
    from controlplane.services.interop import broker
    result = broker.route_and_execute(
        intent, domain=(body.get("domain") or "").strip(),
        submitted_by=f"broker:{request.user.username}",
    )
    return JsonResponse(result, status=200 if result.get("routed") else 404)


# ── Phase 4: Visualizer (agent interaction graph) ──────────────────────────────

@login_required
@require_GET
def visualizer_graph(request):
    """GET /api/v1/visualizer/graph/?window=30 — agent interaction map (nodes + edges)."""
    try:
        window_days = int(request.GET.get("window", "30"))
    except (TypeError, ValueError):
        window_days = 30
    window_days = max(1, min(window_days, 365))
    from controlplane.services.visualizer import build_graph
    return JsonResponse(build_graph(window_days=window_days))


# ── D1: Prometheus metrics ────────────────────────────────────────────────────

@require_GET
def prometheus_metrics(request):
    """
    GET /api/v1/metrics/

    Returns Prometheus text exposition format. Two auth paths:
      - a session-authenticated platform_admin (human/dashboard); or
      - a scraper presenting ``Authorization: Bearer <METRICS_SCRAPE_TOKEN>``
        (constant-time compared) — a Prometheus/Grafana agent can't hold a
        Django session, so this is how it scrapes.
    When no scrape token is configured, only the session path is accepted.
    """
    from django.conf import settings as _settings

    scrape_tokens = getattr(_settings, "METRICS_SCRAPE_TOKENS", []) or []
    if scrape_tokens and bearer_token_matches(request, scrape_tokens):
        authorized = True
    elif request.user.is_authenticated:
        role_error = _require_role(request, "platform_admin")
        authorized = role_error is None
        if not authorized:
            return role_error
    else:
        return JsonResponse({"error": "Unauthorized."}, status=401)

    from controlplane.services.metrics import render_metrics
    from django.http import HttpResponse
    payload = render_metrics()
    return HttpResponse(payload, content_type="text/plain; version=0.0.4; charset=utf-8")


# ── D2: OTel spans ────────────────────────────────────────────────────────────

@login_required
@require_GET
def otel_spans(request):
    """
    GET /api/v1/spans/?run_id=&agent_id=&limit=50

    Returns spans for a specific run or agent (most recent first).
    Used by the dashboard trace viewer.
    """
    from controlplane.models import OtelSpan
    qs = OtelSpan.objects.select_related("agent").order_by("start_time")

    run_id = request.GET.get("run_id")
    agent_id = request.GET.get("agent_id")
    trace_id = request.GET.get("trace_id")

    if run_id:
        qs = qs.filter(run_id=run_id)
    if agent_id:
        qs = qs.filter(agent_id=agent_id)
    if trace_id:
        qs = qs.filter(trace_id=trace_id)
    if not _is_cross_tenant(request.user):
        qs = qs.filter(agent__org_unit_id=_user_business_unit_id(request.user))

    limit = min(int(request.GET.get("limit", 100)), 500)
    spans = list(qs[:limit])

    return JsonResponse({
        "spans": [
            {
                "span_id":       s.span_id,
                "trace_id":      s.trace_id,
                "parent_span_id":s.parent_span_id,
                "name":          s.name,
                "kind":          s.kind,
                "start_time":    s.start_time.isoformat() if s.start_time else None,
                "end_time":      s.end_time.isoformat() if s.end_time else None,
                "duration_ms":   s.duration_ms,
                "status_code":   s.status_code,
                "status_message":s.status_message,
                "attributes":    s.attributes,
                "agent_slug":    s.agent.slug if s.agent else None,
            }
            for s in spans
        ],
        "count": len(spans),
    })


# ── D3: Budget alerts ─────────────────────────────────────────────────────────

@login_required
@require_GET
def budget_alerts(request):
    """
    GET /api/v1/budget-alerts/?resolved=false

    Returns active (or all) budget breach alerts.
    """
    from controlplane.models import BudgetAlert
    qs = BudgetAlert.objects.select_related("agent").order_by("-created_at")
    if not _is_cross_tenant(request.user):
        qs = qs.filter(agent__org_unit_id=_user_business_unit_id(request.user))
    if request.GET.get("resolved", "false").lower() == "false":
        qs = qs.filter(resolved=False)
    limit = min(int(request.GET.get("limit", 50)), 200)
    alerts = list(qs[:limit])
    return JsonResponse({
        "alerts": [
            {
                "id":           str(a.id),
                "agent_slug":   a.agent.slug,
                "agent_name":   a.agent.name,
                "period_month": a.period_month,
                "budget_usd":   float(a.budget_usd),
                "actual_usd":   float(a.actual_usd),
                "overage_usd":  float(a.overage_usd),
                "resolved":     a.resolved,
                "created_at":   a.created_at.isoformat(),
            }
            for a in alerts
        ],
        "count": len(alerts),
    })


# ── E1: Workflows ─────────────────────────────────────────────────────────────

@login_required
@require_GET
def workflows_list(request):
    """GET /api/v1/workflows/ — list workflows."""
    from controlplane.models import Workflow
    qs = Workflow.objects.select_related("business_unit").order_by("name")
    if not _is_cross_tenant(request.user):
        qs = qs.filter(business_unit_id=_user_business_unit_id(request.user))
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)
    return JsonResponse({
        "workflows": [
            {
                "id":           str(w.id),
                "slug":         w.slug,
                "name":         w.name,
                "description":  w.description,
                "status":       w.status,
                "owner":        w.owner,
                "business_unit": w.business_unit.name if w.business_unit else None,
                "task_count":   w.tasks.count(),
                "created_at":   w.created_at.isoformat(),
            }
            for w in qs
        ]
    })


@login_required
def workflow_detail(request, workflow_id):
    """GET /api/v1/workflows/<id>/ — workflow + tasks."""
    from controlplane.models import Workflow
    try:
        w = Workflow.objects.prefetch_related("tasks__agent").get(id=workflow_id)
    except Workflow.DoesNotExist:
        return JsonResponse({"error": "Workflow not found."}, status=404)
    if not _can_access_business_unit(request.user, w.business_unit_id):
        return JsonResponse({"error": "You do not have access to this workflow."}, status=403)

    return JsonResponse({
        "id":           str(w.id),
        "slug":         w.slug,
        "name":         w.name,
        "description":  w.description,
        "status":       w.status,
        "owner":        w.owner,
        "business_unit": w.business_unit.name if w.business_unit else None,
        "tasks": [
            {
                "id":             str(t.id),
                "step_name":      t.step_name,
                "agent_slug":     t.agent.slug if t.agent else None,
                "model_override": t.model_override,
                "depends_on":     t.depends_on,
                "input_template": t.input_template,
                "timeout_seconds": t.timeout_seconds,
                "retry_limit":    t.retry_limit,
                "order":          t.order,
            }
            for t in w.tasks.all()
        ],
        "created_at": w.created_at.isoformat(),
    })


@login_required
@require_POST
def workflow_trigger(request, workflow_id):
    """POST /api/v1/workflows/<id>/run/ — trigger a workflow run."""
    from controlplane.models import Workflow
    from controlplane.services.workflow_queue import workflow_queue

    try:
        w = Workflow.objects.get(id=workflow_id, status=Workflow.Status.ACTIVE)
    except Workflow.DoesNotExist:
        return JsonResponse({"error": "Workflow not found or not active."}, status=404)
    if not _can_access_business_unit(request.user, w.business_unit_id):
        return JsonResponse({"error": "You do not have access to this workflow."}, status=403)
    rl_scope = f"workflow-trigger:{request.user.id}:{workflow_id}"
    if _is_rate_limited(rl_scope, _WORKFLOW_TRIGGER_LIMIT):
        return JsonResponse({"error": "Rate limit exceeded. Try again shortly."}, status=429)

    try:
        body = json.loads(request.body or "{}")
    except Exception:
        body = {}

    inputs = body.get("inputs", {})
    run = workflow_queue.enqueue(w, inputs=inputs, triggered_by=request.user.username)

    return JsonResponse({
        "workflow_run_id": str(run.id),
        "status":          run.status,
        "message":         "Workflow run queued.",
    }, status=202)


# ── E2: Workflow runs ─────────────────────────────────────────────────────────

@login_required
@require_GET
def workflow_run_detail(request, run_id):
    """GET /api/v1/workflow-runs/<id>/ — run status + outputs."""
    from controlplane.models import WorkflowRun
    try:
        r = WorkflowRun.objects.select_related("workflow").get(id=run_id)
    except WorkflowRun.DoesNotExist:
        return JsonResponse({"error": "Workflow run not found."}, status=404)
    if not _can_access_workflow_run(request.user, r):
        return JsonResponse({"error": "You do not have access to this workflow run."}, status=403)

    return JsonResponse({
        "id":           str(r.id),
        "workflow":     {"id": str(r.workflow.id), "slug": r.workflow.slug, "name": r.workflow.name},
        "status":       r.status,
        "triggered_by": r.triggered_by,
        "inputs":       r.inputs,
        "outputs":      r.outputs,
        "error":        r.error,
        "duration_ms":  r.duration_ms,
        "started_at":   r.started_at.isoformat(),
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    })


@login_required
@require_GET
def workflow_run_tasks(request, run_id):
    """GET /api/v1/workflow-runs/<id>/tasks/ — per-step status."""
    from controlplane.models import WorkflowRun
    try:
        r = WorkflowRun.objects.get(id=run_id)
    except WorkflowRun.DoesNotExist:
        return JsonResponse({"error": "Workflow run not found."}, status=404)
    if not _can_access_workflow_run(request.user, r):
        return JsonResponse({"error": "You do not have access to this workflow run."}, status=403)

    task_runs = r.task_runs.select_related("task__agent").order_by("started_at")
    return JsonResponse({
        "workflow_run_id": str(r.id),
        "tasks": [
            {
                "step_name":      tr.task.step_name,
                "agent_slug":     tr.task.agent.slug if tr.task.agent else None,
                "status":         tr.status,
                "attempt":        tr.attempt,
                "resolved_input": tr.resolved_input[:200] if tr.resolved_input else "",
                "output":         tr.output,
                "error":          tr.error,
                "started_at":     tr.started_at.isoformat(),
                "completed_at":   tr.completed_at.isoformat() if tr.completed_at else None,
            }
            for tr in task_runs
        ],
    })


# ── E3: Model router explain ──────────────────────────────────────────────────

@login_required
@require_GET
def model_route_explain(request, agent_id):
    """GET /api/v1/agents/<id>/model-route/ — explain model routing for this agent."""
    from controlplane.models import Agent
    from controlplane.services.model_router import model_router
    try:
        agent = Agent.objects.get(id=agent_id)
    except Agent.DoesNotExist:
        return JsonResponse({"error": "Agent not found."}, status=404)
    if not _can_access_agent(request.user, agent):
        return JsonResponse({"error": "You do not have access to this agent."}, status=403)
    return JsonResponse(model_router.explain(agent))


# ── E4: Shared memory ─────────────────────────────────────────────────────────

@login_required
def shared_memory(request, run_id):
    """
    GET  /api/v1/workflow-runs/<id>/memory/ — list memory entries
    POST /api/v1/workflow-runs/<id>/memory/ — write a memory entry
    """
    from controlplane.models import WorkflowRun, SharedMemory
    from controlplane.services.memory import memory_service

    try:
        run = WorkflowRun.objects.select_related("workflow").get(id=run_id)
    except WorkflowRun.DoesNotExist:
        return JsonResponse({"error": "Workflow run not found."}, status=404)

    # Access control — only the run's triggerer, same-BU members, or cross-tenant
    # staff/admins may read or write a workflow run's shared memory.
    profile = getattr(request.user, "profile", None)
    is_cross = profile.is_cross_tenant if profile is not None else request.user.is_staff
    wf_bu_id = run.workflow.business_unit_id
    allowed = (
        is_cross
        or run.triggered_by == request.user.username
        or (profile is not None and wf_bu_id is not None
            and profile.business_unit_id == wf_bu_id)
    )
    if not allowed:
        return JsonResponse(
            {"error": "You do not have access to this workflow run."}, status=403
        )

    if request.method == "GET":
        entries = SharedMemory.objects.filter(workflow_run=run).order_by("-updated_at")
        return JsonResponse({
            "entries": [
                {
                    "key":        e.key,
                    "value":      e.value,
                    "written_by": e.written_by,
                    "expires_at": e.expires_at.isoformat() if e.expires_at else None,
                    "updated_at": e.updated_at.isoformat(),
                }
                for e in entries
                if not e.is_expired
            ]
        })

    if request.method == "POST":
        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"error": "Invalid JSON."}, status=400)
        key = body.get("key")
        value = body.get("value")
        if not key:
            return JsonResponse({"error": "key is required."}, status=400)
        ttl = body.get("ttl_seconds")
        memory_service.write(
            key=key, value=value, workflow_run=run,
            written_by=request.user.username,
            ttl_seconds=int(ttl) if ttl else None,
        )
        return JsonResponse({"written": True, "key": key})

    return JsonResponse({"error": "Method not allowed."}, status=405)


# ── Agent Factory — Phase F ───────────────────────────────────────────────────

def _insight_dict(insight) -> dict:
    return {
        "id":                 str(insight.id),
        "source_reference":   insight.source_reference,
        "process_name":       insight.process_name,
        "business_unit":      insight.business_unit.name if insight.business_unit else None,
        "finding_type":       insight.finding_type,
        "summary":            insight.summary,
        "impact":             insight.impact,
        "frequency":          insight.frequency,
        "systems_involved":   insight.systems_involved,
        "recommended_action": insight.recommended_action,
        "risk_notes":         insight.risk_notes,
        "blueprint_count":    insight.blueprints.count(),
        "created_at":         insight.created_at.isoformat(),
        "updated_at":         insight.updated_at.isoformat(),
    }


def _blueprint_dict(bp) -> dict:
    return {
        "id":                    str(bp.id),
        "insight_id":            str(bp.insight_id) if bp.insight_id else None,
        "version":               bp.version,
        "agent_name":            bp.agent_name,
        "mission":               bp.mission,
        "trigger":               bp.trigger,
        "inputs":                bp.inputs,
        "outputs":               bp.outputs,
        "tools":                 bp.tools,
        "workflow_steps":        bp.workflow_steps,
        "guardrails":            bp.guardrails,
        "human_approval_points": bp.human_approval_points,
        "success_metrics":       bp.success_metrics,
        "business_value_score":  bp.business_value_score,
        "automation_fit_score":  bp.automation_fit_score,
        "complexity_score":      bp.complexity_score,
        "risk_score":            bp.risk_score,
        "opportunity_score":     bp.opportunity_score,
        "status":                bp.status,
        "risk_level":            bp.risk_level,
        "missing_tools":         bp.missing_tools,
        "missing_data":          bp.missing_data,
        "approved_by":           bp.approved_by.username if bp.approved_by else None,
        "approved_at":           bp.approved_at.isoformat() if bp.approved_at else None,
        "approval_notes":        bp.approval_notes,
        "built_agent_id":        str(bp.built_agent_id) if bp.built_agent_id else None,
        "created_at":            bp.created_at.isoformat(),
        "updated_at":            bp.updated_at.isoformat(),
    }


def _workflow_dict(workflow) -> dict:
    return {
        "id": str(workflow.id),
        "slug": workflow.slug,
        "name": workflow.name,
        "description": workflow.description,
        "status": workflow.status,
        "owner": workflow.owner,
        "business_unit": workflow.business_unit.name if workflow.business_unit else None,
        "task_count": workflow.tasks.count(),
        "created_at": workflow.created_at.isoformat(),
        "updated_at": workflow.updated_at.isoformat(),
    }


def _workflow_run_dict(run) -> dict:
    if run is None:
        return None
    return {
        "id": str(run.id),
        "workflow_id": str(run.workflow_id),
        "status": run.status,
        "triggered_by": run.triggered_by,
        "inputs": run.inputs,
        "outputs": run.outputs,
        "error": run.error,
        "duration_ms": run.duration_ms,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


@login_required
def factory_insights_list(request):
    """
    GET  /api/v1/factory/insights/  — list all process insights
    POST /api/v1/factory/insights/  — ingest (upsert) a process insight
    """
    from controlplane.models import ProcessInsight, BusinessUnit

    if request.method == "GET":
        qs = ProcessInsight.objects.select_related("business_unit").order_by("-created_at")
        if not _is_cross_tenant(request.user):
            qs = qs.filter(business_unit_id=_user_business_unit_id(request.user))
        finding_type = request.GET.get("finding_type")
        if finding_type:
            qs = qs.filter(finding_type=finding_type)
        return JsonResponse({"insights": [_insight_dict(i) for i in qs[:200]]})

    if request.method == "POST":
        role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
        if role_error is not None:
            return role_error
        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"error": "Invalid JSON."}, status=400)

        ref = body.get("source_reference", "").strip()
        if not ref:
            return JsonResponse({"error": "source_reference is required."}, status=400)
        process_name = body.get("process_name", "").strip()
        if not process_name:
            return JsonResponse({"error": "process_name is required."}, status=400)
        summary = body.get("summary", "").strip()
        if not summary:
            return JsonResponse({"error": "summary is required."}, status=400)

        bu = None
        bu_val = body.get("business_unit")
        if bu_val:
            try:
                bu = BusinessUnit.objects.get(name=bu_val)
            except BusinessUnit.DoesNotExist:
                try:
                    bu = BusinessUnit.objects.get(id=bu_val)
                except (BusinessUnit.DoesNotExist, Exception):
                    pass
        if not _is_cross_tenant(request.user):
            own_bu_id = _user_business_unit_id(request.user)
            if own_bu_id is None:
                return JsonResponse({"error": "No business unit assigned to your account."}, status=403)
            if bu is not None and str(bu.id) != str(own_bu_id):
                return JsonResponse({"error": "You cannot create insights in another business unit."}, status=403)
            if bu is None:
                bu = BusinessUnit.objects.filter(pk=own_bu_id).first()

        defaults = {
            "process_name":       process_name,
            "summary":            summary,
            "business_unit":      bu,
            "finding_type":       body.get("finding_type",       "other"),
            "impact":             body.get("impact",             ""),
            "frequency":          body.get("frequency",          ""),
            "systems_involved":   body.get("systems_involved",   []),
            "recommended_action": body.get("recommended_action", ""),
            "risk_notes":         body.get("risk_notes",         ""),
            "evidence":           body.get("evidence",           {}),
        }

        insight, created = ProcessInsight.objects.update_or_create(
            source_reference=ref,
            defaults=defaults,
        )
        return JsonResponse(_insight_dict(insight), status=201 if created else 200)

    return JsonResponse({"error": "Method not allowed."}, status=405)


@login_required
def factory_insight_detail(request, insight_id):
    """
    GET   /api/v1/factory/insights/<id>/  — retrieve
    PATCH /api/v1/factory/insights/<id>/  — update editable fields
    DELETE /api/v1/factory/insights/<id>/ — delete
    """
    from controlplane.models import ProcessInsight, AuditLog

    try:
        insight = ProcessInsight.objects.select_related("business_unit").get(id=insight_id)
    except ProcessInsight.DoesNotExist:
        return JsonResponse({"error": "Not found."}, status=404)
    if not _can_access_business_unit(request.user, insight.business_unit_id):
        return JsonResponse({"error": "You do not have access to this insight."}, status=403)

    if request.method == "GET":
        return JsonResponse(_insight_dict(insight))

    if request.method in ("PATCH", "PUT"):
        role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
        if role_error is not None:
            return role_error
        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"error": "Invalid JSON."}, status=400)

        editable = [
            "process_name", "finding_type", "summary", "impact",
            "frequency", "systems_involved", "recommended_action",
            "risk_notes", "evidence",
        ]
        for field in editable:
            if field in body:
                setattr(insight, field, body[field])
        insight.save()
        return JsonResponse(_insight_dict(insight))

    if request.method == "DELETE":
        role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
        if role_error is not None:
            return role_error
        insight_id_str = str(insight.id)
        AuditLog.objects.create(
            actor=request.user.username,
            action="factory_insight_deleted",
            resource_type="ProcessInsight",
            resource_id=insight_id_str,
            payload={"source_reference": insight.source_reference, "process_name": insight.process_name},
        )
        insight.delete()
        return JsonResponse({"deleted": True, "id": insight_id_str}, status=200)

    return JsonResponse({"error": "Method not allowed."}, status=405)


@login_required
@require_POST
def factory_insight_generate_blueprint(request, insight_id):
    """
    POST /api/v1/factory/insights/<id>/generate-blueprint/
    Generates a new AgentBlueprint from this insight.
    """
    from controlplane.models import ProcessInsight
    from controlplane.services.factory import blueprint_generator

    try:
        insight = ProcessInsight.objects.select_related("business_unit").get(id=insight_id)
    except ProcessInsight.DoesNotExist:
        return JsonResponse({"error": "Not found."}, status=404)
    if not _can_access_business_unit(request.user, insight.business_unit_id):
        return JsonResponse({"error": "You do not have access to this insight."}, status=403)
    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error

    blueprint = blueprint_generator.generate(insight)
    return JsonResponse(_blueprint_dict(blueprint), status=201)


@login_required
def factory_blueprints_list(request):
    """GET /api/v1/factory/blueprints/  — list blueprints, optional ?status= filter"""
    from controlplane.models import AgentBlueprint

    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    qs = AgentBlueprint.objects.select_related("insight", "approved_by", "built_agent")
    if not _is_cross_tenant(request.user):
        bu_id = _user_business_unit_id(request.user)
        qs = qs.filter(Q(insight__business_unit_id=bu_id) | Q(built_agent__org_unit_id=bu_id))
    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)
    insight_filter = request.GET.get("insight_id")
    if insight_filter:
        qs = qs.filter(insight_id=insight_filter)

    return JsonResponse({"blueprints": [_blueprint_dict(bp) for bp in qs[:200]]})


@login_required
def factory_blueprint_detail(request, blueprint_id):
    """
    GET   /api/v1/factory/blueprints/<id>/  — retrieve
    PATCH /api/v1/factory/blueprints/<id>/  — update editable fields (draft/needs_* only)
    DELETE /api/v1/factory/blueprints/<id>/ — delete
    """
    from controlplane.models import AgentBlueprint, AuditLog

    try:
        bp = AgentBlueprint.objects.select_related(
            "insight", "approved_by", "built_agent"
        ).get(id=blueprint_id)
    except AgentBlueprint.DoesNotExist:
        return JsonResponse({"error": "Not found."}, status=404)
    if not _can_access_business_unit(request.user, _blueprint_business_unit_id(bp)):
        return JsonResponse({"error": "You do not have access to this blueprint."}, status=403)

    if request.method == "GET":
        return JsonResponse(_blueprint_dict(bp))

    if request.method in ("PATCH", "PUT"):
        role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
        if role_error is not None:
            return role_error
        if bp.status not in (
            AgentBlueprint.Status.DRAFT,
            AgentBlueprint.Status.NEEDS_DATA,
            AgentBlueprint.Status.NEEDS_TOOL,
        ):
            return JsonResponse(
                {"error": f"Cannot edit blueprint in '{bp.status}' status."},
                status=400,
            )
        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"error": "Invalid JSON."}, status=400)

        editable = [
            "agent_name", "mission", "trigger", "inputs", "outputs",
            "tools", "workflow_steps", "guardrails", "human_approval_points",
            "success_metrics", "missing_tools", "missing_data", "risk_level",
        ]
        for field in editable:
            if field in body:
                setattr(bp, field, body[field])

        if "missing_data" in body or "missing_tools" in body:
            if bp.missing_data:
                bp.status = AgentBlueprint.Status.NEEDS_DATA
            elif bp.missing_tools:
                bp.status = AgentBlueprint.Status.NEEDS_TOOL
            else:
                bp.status = AgentBlueprint.Status.DRAFT

        bp.save()
        return JsonResponse(_blueprint_dict(bp))

    if request.method == "DELETE":
        role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
        if role_error is not None:
            return role_error
        built_agent = bp.built_agent
        bp_id_str = str(bp.id)
        AuditLog.objects.create(
            actor=request.user.username,
            action="factory_blueprint_deleted",
            resource_type="AgentBlueprint",
            resource_id=bp_id_str,
            payload={
                "agent_name": bp.agent_name,
                "status": bp.status,
                "built_agent_id": str(bp.built_agent_id) if bp.built_agent_id else None,
            },
        )
        bp.delete()
        deleted_agent_id = None
        if (
            built_agent is not None
            and built_agent.status == Agent.Status.DRAFT
            and not built_agent.source_blueprints.exists()
            and not built_agent.source_packages.exists()
        ):
            deleted_agent_id = str(built_agent.id)
            built_agent.delete()
            AuditLog.objects.create(
                actor=request.user.username,
                action="agent_deleted",
                resource_type="Agent",
                resource_id=deleted_agent_id,
                payload={"source": "factory_blueprint_delete"},
            )
        return JsonResponse({"deleted": True, "id": bp_id_str, "deleted_agent_id": deleted_agent_id}, status=200)

    return JsonResponse({"error": "Method not allowed."}, status=405)


@login_required
@require_POST
def factory_blueprint_approve(request, blueprint_id):
    """
    POST /api/v1/factory/blueprints/<id>/approve/
    Body: {"notes": "optional approval notes"}
    """
    from controlplane.models import AgentBlueprint, AuditLog

    try:
        bp = AgentBlueprint.objects.get(id=blueprint_id)
    except AgentBlueprint.DoesNotExist:
        return JsonResponse({"error": "Not found."}, status=404)
    if not _can_access_business_unit(request.user, _blueprint_business_unit_id(bp)):
        return JsonResponse({"error": "You do not have access to this blueprint."}, status=403)
    role_error = _require_role(request, "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error

    if bp.status == AgentBlueprint.Status.APPROVED:
        return JsonResponse({"error": "Blueprint is already approved."}, status=400)

    if not bp.can_transition_to(AgentBlueprint.Status.APPROVED):
        return JsonResponse(
            {"error": f"Cannot approve blueprint in '{bp.status}' status."},
            status=400,
        )

    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}

    bp.status         = AgentBlueprint.Status.APPROVED
    bp.approved_by    = request.user
    bp.approved_at    = timezone.now()
    bp.approval_notes = body.get("notes", "")
    bp.save(update_fields=["status", "approved_by", "approved_at", "approval_notes", "updated_at"])

    AuditLog.objects.create(
        actor         = request.user.username,
        action        = "blueprint_approved",
        resource_type = "AgentBlueprint",
        resource_id   = str(bp.id),
        payload       = {"agent_name": bp.agent_name, "notes": bp.approval_notes},
    )

    return JsonResponse(_blueprint_dict(bp))


@login_required
@require_POST
def factory_blueprint_build(request, blueprint_id):
    """
    POST /api/v1/factory/blueprints/<id>/build/
    Compiles an approved blueprint into a runnable Agent.
    """
    from controlplane.models import AgentBlueprint
    from controlplane.services.factory import build_compiler

    try:
        bp = AgentBlueprint.objects.select_related("insight__business_unit").get(id=blueprint_id)
    except AgentBlueprint.DoesNotExist:
        return JsonResponse({"error": "Not found."}, status=404)
    if not _can_access_business_unit(request.user, _blueprint_business_unit_id(bp)):
        return JsonResponse({"error": "You do not have access to this blueprint."}, status=403)
    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error

    try:
        agent = build_compiler.build(bp, built_by=request.user.username)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({
        "blueprint": _blueprint_dict(bp),
        "agent": {
            "id":     str(agent.id),
            "slug":   agent.slug,
            "name":   agent.name,
            "status": agent.status,
        },
    }, status=201)


@login_required
@require_POST
def factory_blueprint_build_workflow(request, blueprint_id):
    """
    POST /api/v1/factory/blueprints/<id>/build-workflow/
    Compiles a built blueprint into a Workflow DAG and optionally runs it.
    """
    from controlplane.models import AgentBlueprint, AuditLog

    try:
        bp = AgentBlueprint.objects.select_related("built_agent", "insight__business_unit").get(id=blueprint_id)
    except AgentBlueprint.DoesNotExist:
        return JsonResponse({"error": "Not found."}, status=404)
    if not _can_access_business_unit(request.user, _blueprint_business_unit_id(bp)):
        return JsonResponse({"error": "You do not have access to this blueprint."}, status=403)
    role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
    if role_error is not None:
        return role_error

    if bp.built_agent_id is None:
        return JsonResponse(
            {"error": "Blueprint must be built before compiling a workflow."},
            status=400,
        )

    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}

    inputs = body.get("inputs") or {}
    run_in_sandbox = body.get("run", True)
    if isinstance(run_in_sandbox, str):
        run_in_sandbox = run_in_sandbox.lower() not in {"false", "0", "no"}
    else:
        run_in_sandbox = bool(run_in_sandbox)
    if _is_rate_limited(f"build-workflow:{request.user.id}:{blueprint_id}", _WORKFLOW_BUILD_LIMIT):
        return JsonResponse({"error": "Rate limit exceeded. Try again shortly."}, status=429)

    if run_in_sandbox:
        workflow, run = workflow_compiler.compile_and_run(
            bp,
            inputs=inputs,
            triggered_by=request.user.username,
            agent=bp.built_agent,
            activate=False,
        )
    else:
        workflow = workflow_compiler.compile(
            bp,
            agent=bp.built_agent,
            built_by=request.user.username,
            activate=False,
        )
        run = None

    AuditLog.objects.create(
        actor=request.user.username,
        action="workflow_compiled",
        resource_type="Workflow",
        resource_id=str(workflow.id),
        payload={
            "blueprint_id": str(bp.id),
            "run_id": str(run.id) if run is not None else None,
            "run_requested": bool(run_in_sandbox),
        },
    )

    return JsonResponse(
        {
            "blueprint": _blueprint_dict(bp),
            "workflow": _workflow_dict(workflow),
            "run": _workflow_run_dict(run),
        },
        status=201,
    )


@login_required
@require_POST
def factory_tool_bindings_promote(request, agent_id):
    """
    POST /api/v1/factory/agents/<id>/tool-bindings/promote/

    Body:
      { "mode": "sandbox"|"live", "tool_name": "optional single binding" }
    """
    from controlplane.models import Agent, AgentToolBinding
    from controlplane.models import AgentFactoryPackage, AuditLog

    try:
        agent = Agent.objects.get(id=agent_id)
    except Agent.DoesNotExist:
        return JsonResponse({"error": "Agent not found."}, status=404)
    if not _can_access_agent(request.user, agent):
        return JsonResponse({"error": "You do not have access to this agent."}, status=403)

    try:
        body = json.loads(request.body) if request.body else {}
    except Exception:
        body = {}

    mode = str(body.get("mode", "sandbox")).lower()
    if mode not in {"sandbox", "live"}:
        return JsonResponse({"error": "mode must be 'sandbox' or 'live'."}, status=400)
    required_roles = ("agent_approver", "platform_admin") if mode == "live" else (
        "agent_builder", "agent_approver", "platform_admin"
    )
    role_error = _require_role(request, *required_roles)
    if role_error is not None:
        return role_error
    tool_name = (body.get("tool_name") or "").strip()

    bindings = AgentToolBinding.objects.filter(agent=agent).select_related("connector")
    if tool_name:
        bindings = bindings.filter(tool_name=tool_name)
    bindings = list(bindings)
    if not bindings:
        return JsonResponse({"error": "No bindings found for this agent."}, status=404)

    package = agent.source_packages.order_by("-created_at").first()
    promoted = []
    from django.db import transaction
    try:
        with transaction.atomic():
            for binding in bindings:
                if mode == "live":
                    promoted.append(promote_to_live(binding, approver=request.user, package=package))
                else:
                    promoted.append(promote_to_sandbox(binding, by=request.user.username))
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    AuditLog.objects.create(
        actor=request.user.username,
        action="tool_bindings_promoted",
        resource_type="Agent",
        resource_id=str(agent.id),
        payload={
            "mode": mode,
            "tool_name": tool_name or None,
            "binding_count": len(promoted),
            "package_id": package.package_id if isinstance(package, AgentFactoryPackage) else None,
        },
    )

    return JsonResponse({
        "agent_id": str(agent.id),
        "mode": mode,
        "bindings": [
            {
                "id": str(binding.id),
                "tool_name": binding.tool_name,
                "binding_status": binding.binding_status,
                "approved_at": binding.approved_at.isoformat() if binding.approved_at else None,
                "approved_by": binding.approved_by.username if binding.approved_by else None,
            }
            for binding in promoted
        ],
    }, status=200)


# ── Agent Factory — Package ingestion (canonical handoff) ─────────────────────

def _package_dict(pkg) -> dict:
    return {
        "id":                    str(pkg.id),
        "package_id":            pkg.package_id,
        "blueprint_id":          pkg.external_blueprint_id,
        "package_version":       pkg.package_version,
        "package_type":          pkg.package_type,
        "status":                pkg.status,
        "risk_tier":             pkg.risk_tier,
        "validation_report":     pkg.validation_report,
        "safety_boundary":       pkg.safety_boundary,
        "approval_route":        pkg.approval_route,
        "approval_progress":     pkg.approval_progress,
        "tool_binding_plan":     pkg.tool_binding_plan,
        "telemetry_contract":    pkg.telemetry_contract,
        # Enforced safety posture (restrictive defaults applied)
        "can_build_sandbox_agent":          pkg.can_build_sandbox_agent,
        "can_bind_production_tools":        pkg.can_bind_production_tools,
        "can_deploy_to_production":         pkg.can_deploy_to_production,
        "requires_human_or_policy_approval": pkg.requires_human_or_policy_approval,
        # Traceability links
        "insight_id":            str(pkg.insight_id) if pkg.insight_id else None,
        "blueprint_db_id":       str(pkg.blueprint_id) if pkg.blueprint_id else None,
        "sandbox_agent_id":      str(pkg.sandbox_agent_id) if pkg.sandbox_agent_id else None,
        "sandbox_agent_slug":    pkg.sandbox_agent.slug if pkg.sandbox_agent_id else None,
        "sandbox_agent_status":  pkg.sandbox_agent.status if pkg.sandbox_agent_id else None,
        "ingested_by":           pkg.ingested_by,
        "created_at":            pkg.created_at.isoformat(),
        "updated_at":            pkg.updated_at.isoformat(),
    }


@login_required
def factory_packages_list(request):
    """
    GET  /api/v1/factory/packages/   — list ingested packages
    POST /api/v1/factory/packages/   — ingest an agent_factory_package
    """
    from controlplane.models import AgentFactoryPackage
    from controlplane.services.package_ingestor import (
        package_ingestor, PackageValidationError,
    )

    if request.method == "GET":
        qs = (AgentFactoryPackage.objects
              .select_related("insight", "blueprint", "sandbox_agent")
              .order_by("-created_at"))
        if not _is_cross_tenant(request.user):
            bu_id = _user_business_unit_id(request.user)
            qs = qs.filter(
                Q(insight__business_unit_id=bu_id)
                | Q(blueprint__insight__business_unit_id=bu_id)
                | Q(sandbox_agent__org_unit_id=bu_id)
            )
        status_filter = request.GET.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return JsonResponse({"packages": [_package_dict(p) for p in qs[:200]]})

    if request.method == "POST":
        role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
        if role_error is not None:
            return role_error
        try:
            body = json.loads(request.body)
        except Exception:
            return JsonResponse({"error": "Invalid JSON."}, status=400)

        try:
            pkg = package_ingestor.ingest(body, ingested_by=request.user.username)
        except PackageValidationError as exc:
            # Validation failed — report missing/invalid sections clearly.
            return JsonResponse(
                {"error": "Package validation failed.", "validation_report": exc.report},
                status=422,
            )

        return JsonResponse(_package_dict(pkg), status=201)

    return JsonResponse({"error": "Method not allowed."}, status=405)


@login_required
def factory_package_detail(request, package_id):
    """
    GET    /api/v1/factory/packages/<uuid:id>/ — retrieve a package
    DELETE /api/v1/factory/packages/<uuid:id>/ — delete a package
    """
    from controlplane.models import AgentFactoryPackage, AuditLog

    try:
        pkg = (AgentFactoryPackage.objects
               .select_related("insight", "blueprint", "sandbox_agent")
               .get(id=package_id))
    except AgentFactoryPackage.DoesNotExist:
        return JsonResponse({"error": "Not found."}, status=404)
    if not _can_access_business_unit(request.user, _package_business_unit_id(pkg)):
        return JsonResponse({"error": "You do not have access to this package."}, status=403)

    if request.method == "GET":
        return JsonResponse(_package_dict(pkg))

    if request.method == "DELETE":
        role_error = _require_role(request, "agent_builder", "agent_approver", "platform_admin")
        if role_error is not None:
            return role_error
        pkg_id_str = str(pkg.id)
        AuditLog.objects.create(
            actor=request.user.username,
            action="factory_package_deleted",
            resource_type="AgentFactoryPackage",
            resource_id=pkg_id_str,
            payload={
                "package_id": pkg.package_id,
                "status": pkg.status,
                "sandbox_agent_id": str(pkg.sandbox_agent_id) if pkg.sandbox_agent_id else None,
            },
        )
        pkg.delete()
        return JsonResponse({"deleted": True, "id": pkg_id_str}, status=200)

    return JsonResponse({"error": "Method not allowed."}, status=405)
