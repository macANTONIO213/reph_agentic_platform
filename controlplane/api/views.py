"""
API v1 views — plain Django JsonResponse, no DRF.
All endpoints require an authenticated session (login_required).
"""
import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Count, Max, Q, Sum
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from controlplane.models import (
    Agent, AgentFeedback, AgentRun, Approval, AuditLog,
    BusinessUnit, DataConnector, Division, EvalCase, EvalRun, EvalSuite,
    KnowledgeDocument, OrgProcess, WorkStream,
)
from controlplane.services.governance import governance, RegistrationError, TransitionError
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


def _window(request):
    return request.GET.get("window", "30d")


def _filters(request):
    return {
        k: v
        for k, v in {
            "agent_id":        request.GET.get("agent"),
            "platform":        request.GET.get("platform"),
            "business_unit_id": request.GET.get("business_unit"),
            "division_id":     request.GET.get("division"),
            "work_stream_id":  request.GET.get("work_stream"),
            "process_id":      request.GET.get("process"),
        }.items()
        if v
    }


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
@require_GET
def agent_detail(request, agent_id):
    from django.shortcuts import get_object_or_404
    agent = get_object_or_404(Agent, id=agent_id)
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


@login_required
@require_GET
def agent_metrics(request, agent_id):
    window = _window(request)
    bucket = request.GET.get("bucket", "day")
    return JsonResponse({
        "timeseries": runs_timeseries(window, bucket, agent_id=agent_id),
        "latency": latency_timeseries(window, bucket, agent_id=agent_id),
        "ratings": rating_distribution(window, agent_id=agent_id),
    })


# ── Monitoring ────────────────────────────────────────────────────────────────

@login_required
@require_GET
def monitoring_summary_view(request):
    return JsonResponse(monitoring_summary(_window(request), **_filters(request)))


@login_required
@require_GET
def monitoring_timeseries(request):
    window = _window(request)
    bucket = request.GET.get("bucket", "day")
    filters = _filters(request)
    return JsonResponse({
        "runs": runs_timeseries(window, bucket, **filters),
        "latency": latency_timeseries(window, bucket, **filters),
    })


@login_required
@require_GET
def monitoring_breakdowns(request):
    window = _window(request)
    return JsonResponse({
        "by_platform": runs_by_platform(window),
        "by_agent": runs_by_agent(window),
        "ratings": rating_distribution(window),
        "low_rated": low_rated_runs(window),
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
    return JsonResponse({"runs": low_rated_runs(window)})


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
        # Staff can bypass the governance gate for admin overrides
        agent.transition_to(new_status, bypass_governance=request.user.is_staff)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)

    AuditLog.objects.create(
        actor=request.user.username,
        action="agent_status_change",
        resource_type="Agent",
        resource_id=str(agent.id),
        payload={"new_status": new_status, "agent": agent.name},
        ip_address=request.META.get("REMOTE_ADDR"),
    )
    return JsonResponse({"status": agent.status, "agent_id": str(agent.id)})


# ── Approvals (Phase A governance) ────────────────────────────────────────────

def _is_approver(user) -> bool:
    return user.is_staff or user.groups.filter(name="agent_approver").exists()


@login_required
def agent_approvals(request, agent_id):
    from django.shortcuts import get_object_or_404
    agent = get_object_or_404(Agent, id=agent_id)

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
    Any authenticated user may register (builder role not yet enforced — noted in DECISIONS.md).
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
    except Exception as e:
        return JsonResponse({"error": f"Unexpected error: {e}"}, status=500)

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

    class _AnonAgent:
        org_unit_id = None
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


# ── D1: Prometheus metrics ────────────────────────────────────────────────────

@login_required
@require_GET
def prometheus_metrics(request):
    """
    GET /api/v1/metrics/

    Returns Prometheus text exposition format.
    Protect with HTTP Basic Auth or restrict to internal IPs in production.
    Render: allow Grafana Cloud scraper to hit this endpoint.
    """
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
