import json
import re

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Avg, Count, Sum
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from django.utils import timezone

from .models import (
    Agent, AgentFeedback, AgentRun, Approval, AuditLog, BusinessUnit,
    ConversationSession, Division, GovernanceReview, OrgProcess, TelemetryEvent, WorkStream,
)
from .security import is_cross_tenant as _is_cross_tenant, user_business_unit_id as _user_business_unit_id
from .services.agent_runtime import PlatformAgentRuntime


@login_required
@ensure_csrf_cookie
def dashboard(request):
    agents = Agent.objects.all().prefetch_related("runs").annotate(
        source_blueprint_count=Count("source_blueprints", distinct=True)
    )
    if not _is_cross_tenant(request.user):
        agents = agents.filter(org_unit_id=_user_business_unit_id(request.user))
    live_agent = Agent.objects.filter(slug="agent-deployment-advisor").first()
    if live_agent is not None and not _is_cross_tenant(request.user):
        if str(live_agent.org_unit_id or "") != str(_user_business_unit_id(request.user) or ""):
            live_agent = None
    runs = AgentRun.objects.select_related("agent").order_by("-started_at")[:8]
    events = TelemetryEvent.objects.select_related("agent").order_by("-created_at")[:10]
    if not _is_cross_tenant(request.user):
        runs = runs.filter(agent__org_unit_id=_user_business_unit_id(request.user))
        events = events.filter(agent__org_unit_id=_user_business_unit_id(request.user))

    totals = agents.aggregate(
        total_mau=Sum("monthly_active_users"),
        total_runs=Sum("monthly_runs"),
        total_cost=Sum("monthly_cost_usd"),
    )
    status_counts = {
        item["status"]: item["count"]
        for item in agents.values("status").annotate(count=Count("id")).order_by("status")
    }
    risk_counts = {
        item["risk_tier"]: item["count"]
        for item in agents.values("risk_tier").annotate(count=Count("id")).order_by("risk_tier")
    }

    pending_reviews = GovernanceReview.objects.filter(status=GovernanceReview.Status.PENDING)
    if not _is_cross_tenant(request.user):
        pending_reviews = pending_reviews.filter(agent__org_unit_id=_user_business_unit_id(request.user))
    pending_reviews = pending_reviews.count()
    business_units = BusinessUnit.objects.filter(is_active=True)
    if not _is_cross_tenant(request.user):
        business_units = business_units.filter(id=_user_business_unit_id(request.user))

    # Monitoring stats from the last 20 runs
    recent_runs_qs = AgentRun.objects.order_by("-started_at")
    if not _is_cross_tenant(request.user):
        recent_runs_qs = recent_runs_qs.filter(agent__org_unit_id=_user_business_unit_id(request.user))
    recent_runs = list(recent_runs_qs[:20])
    latencies = sorted(r.latency_ms for r in recent_runs if r.latency_ms > 0)
    monitoring = {
        "p50_ms": latencies[len(latencies) // 2] if latencies else 0,
        "p99_ms": latencies[max(0, int(len(latencies) * 0.99) - 1)] if latencies else 0,
        "failed": sum(1 for r in recent_runs if r.status == AgentRun.Status.FAILED),
        "succeeded": sum(1 for r in recent_runs if r.status == AgentRun.Status.COMPLETED),
        "cost_usd": round(sum(r.cost_usd for r in recent_runs), 4),
    }

    context = {
        "agents": agents,
        "live_agent": live_agent,
        "runs": runs,
        "events": events,
        "pending_reviews": pending_reviews,
        "business_units": business_units,
        "agent_platform_choices": Agent.Platform.choices,
        "agent_kind_choices": Agent.Kind.choices,
        "agent_mode_choices": Agent.IntegrationMode.choices,
        "monitoring": monitoring,
        "metrics": {
            "registered_agents": agents.count(),
            "production_agents": status_counts.get(Agent.Status.PRODUCTION, 0),
            "total_mau": totals["total_mau"] or 0,
            "total_runs": totals["total_runs"] or 0,
            "total_cost": totals["total_cost"] or 0,
            "risk_counts": risk_counts,
            "status_counts": status_counts,
        },
    }
    return render(request, "controlplane/dashboard.html", context)


_MAX_MESSAGE_LENGTH = 4000
_USER_LABEL_RE = re.compile(r"^[\w@.\-]{1,120}$")
_RESERVED_USER_LABELS = frozenset({"admin", "system", "root", "platform", "seed_demo", "superuser"})
_RATE_LIMIT_RUNS = 10
_RATE_LIMIT_WINDOW = 60


def _sanitise_user_label(raw: str) -> str:
    label = str(raw).strip()[:120] or "anonymous"
    if not _USER_LABEL_RE.match(label) or label.lower() in _RESERVED_USER_LABELS:
        return "anonymous"
    return label


def _is_rate_limited(ip: str, agent_id: str) -> bool:
    key = f"rl:run:{ip}:{agent_id}"
    count = cache.get(key, 0)
    if count >= _RATE_LIMIT_RUNS:
        return True
    cache.set(key, count + 1, timeout=_RATE_LIMIT_WINDOW)
    return False


@login_required
@require_POST
def run_agent(request, agent_id):
    agent = get_object_or_404(Agent, id=agent_id, status__in=[Agent.Status.PILOT, Agent.Status.PRODUCTION])

    # Tenant scoping — a user may only run agents within their own business unit
    # (staff / platform_admins are cross-tenant and bypass this check).
    profile = getattr(request.user, "profile", None)
    if profile is not None and not profile.can_access_agent(agent):
        return JsonResponse(
            {"error": "You do not have access to this agent."}, status=403
        )

    ip = request.META.get("REMOTE_ADDR", "unknown")
    if _is_rate_limited(ip, str(agent_id)):
        return JsonResponse({"error": "Rate limit exceeded. Try again in a minute."}, status=429)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    message = str(payload.get("message", "")).strip()
    if not message:
        return JsonResponse({"error": "Message is required."}, status=400)
    if len(message) > _MAX_MESSAGE_LENGTH:
        return JsonResponse({"error": f"Message exceeds {_MAX_MESSAGE_LENGTH} character limit."}, status=400)

    if agent.risk_tier >= 4:
        approval = (
            Approval.objects.filter(
                agent=agent,
                is_consumed=False,
                expires_at__gt=timezone.now(),
            )
            .order_by("-created_at")
            .first()
        )
        if not approval:
            return JsonResponse(
                {
                    "error": (
                        f"'{agent.name}' is a Tier 4 (high-risk) agent. "
                        "A platform approver must create a server-side approval "
                        "in the Admin → Approvals panel before execution."
                    ),
                    "requires_approval": True,
                },
                status=403,
            )
        # Consume the approval (single-use) and write an audit record.
        approval.is_consumed = True
        approval.save(update_fields=["is_consumed"])
        AuditLog.objects.create(
            actor=request.user.username,
            action="consume_tier4_approval",
            resource_type="Agent",
            resource_id=str(agent.id),
            payload={
                "approval_id": str(approval.id),
                "approver": approval.approved_by_username,
            },
            ip_address=request.META.get("REMOTE_ADDR"),
        )

    # Use the authenticated username as the user label.
    user_label = request.user.username or _sanitise_user_label(payload.get("user", "demo_user"))

    # Resolve or create a conversation session for multi-turn context.
    session = _resolve_session(payload.get("session_id"), agent, user_label)

    runtime = PlatformAgentRuntime(agent=agent, user_label=user_label)
    response = StreamingHttpResponse(
        runtime.stream(message, session=session), content_type="text/event-stream"
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _resolve_session(
    session_id: str | None, agent: Agent, user_label: str
) -> ConversationSession:
    if session_id:
        try:
            # Scope by user_label — reject sessions owned by a different user.
            return ConversationSession.objects.get(
                id=session_id, agent=agent, user_label=user_label
            )
        except (ConversationSession.DoesNotExist, ValueError):
            pass
    return ConversationSession.objects.create(agent=agent, user_label=user_label)



@login_required
@require_GET
def telemetry_feed(request):
    events = TelemetryEvent.objects.select_related("agent").order_by("-created_at")
    if not _is_cross_tenant(request.user):
        events = events.filter(agent__org_unit_id=_user_business_unit_id(request.user))
    events = events[:15]
    data = [
        {
            "event_type": event.event_type,
            "agent": event.agent.name if event.agent else "Unknown",
            "business_unit": event.business_unit,
            "actor": event.actor,
            "created_at": event.created_at.isoformat(),
            "payload": event.payload,
        }
        for event in events
    ]
    return JsonResponse({"events": data})


@login_required
@require_POST
def submit_feedback(request, run_id):
    run = get_object_or_404(AgentRun, id=run_id)

    # Only the user who submitted the run (or staff) may rate it.
    if not request.user.is_staff and run.user_label != request.user.username:
        return JsonResponse({"error": "You can only rate runs you submitted."}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    rating = payload.get("rating")
    if not isinstance(rating, int) or not (1 <= rating <= 5):
        return JsonResponse({"error": "Rating must be an integer 1–5."}, status=400)

    comment = str(payload.get("comment", ""))[:1000]

    feedback = AgentFeedback.objects.create(
        run=run,
        rating=rating,
        comment=comment,
        submitted_by=request.user.username,
    )

    # Recompute satisfaction score as the mean rating for this agent.
    avg = AgentFeedback.objects.filter(run__agent=run.agent).aggregate(avg=Avg("rating"))["avg"]
    if avg is not None:
        run.agent.satisfaction_score = round(avg, 2)
        run.agent.save(update_fields=["satisfaction_score", "updated_at"])

    return JsonResponse({"id": str(feedback.id), "rating": feedback.rating})


@login_required
@require_GET
def org_children(request):
    """Return child nodes for a given parent — powers the cascading dropdowns."""
    level = request.GET.get("level")
    parent_id = request.GET.get("parent_id", "")

    if level == "divisions":
        qs = Division.objects.filter(is_active=True)
        if parent_id:
            qs = qs.filter(business_unit_id=parent_id)
        data = [{"id": str(o.id), "name": o.name} for o in qs]

    elif level == "workstreams":
        qs = WorkStream.objects.filter(is_active=True)
        if parent_id:
            qs = qs.filter(division_id=parent_id)
        data = [{"id": str(o.id), "name": o.name} for o in qs]

    elif level == "processes":
        qs = OrgProcess.objects.filter(is_active=True)
        if parent_id:
            qs = qs.filter(work_stream_id=parent_id)
        data = [{"id": str(o.id), "name": o.name} for o in qs]

    else:
        data = []

    return JsonResponse({"items": data})


@login_required
@require_GET
def monitoring_data(request):
    recent_runs_qs = AgentRun.objects.order_by("-started_at")
    if not _is_cross_tenant(request.user):
        recent_runs_qs = recent_runs_qs.filter(agent__org_unit_id=_user_business_unit_id(request.user))
    recent_runs = list(recent_runs_qs[:20])
    latencies = sorted(r.latency_ms for r in recent_runs if r.latency_ms > 0)
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p99 = latencies[max(0, int(len(latencies) * 0.99) - 1)] if latencies else 0

    failed = sum(1 for r in recent_runs if r.status == AgentRun.Status.FAILED)
    succeeded = sum(1 for r in recent_runs if r.status == AgentRun.Status.COMPLETED)
    cost_usd = round(sum(r.cost_usd for r in recent_runs), 4)

    return JsonResponse(
        {
            "latency_p50_ms": p50,
            "latency_p99_ms": p99,
            "succeeded_last_20": succeeded,
            "failed_last_20": failed,
            "cost_last_20_usd": cost_usd,
        }
    )


# ── Management panel (custom admin replacement) ────────────────────────────

def _staff_required(view_fn):
    """Decorator: login_required + staff/platform_admin check."""
    from functools import wraps
    from django.contrib.auth.decorators import login_required as _lr

    @_lr
    @wraps(view_fn)
    def wrapper(request, *args, **kwargs):
        from controlplane.security import has_role
        if not has_role(request.user, "platform_admin"):
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden("Platform admin or staff access required.")
        return view_fn(request, *args, **kwargs)
    return wrapper


@_staff_required
@ensure_csrf_cookie
def manage_panel(request):
    agents = (
        Agent.objects.all()
        .select_related("org_unit", "org_division", "org_work_stream")
        .prefetch_related("governance_reviews", "approvals")
        .order_by("name")
    )

    pending_reviews = (
        GovernanceReview.objects.filter(status=GovernanceReview.Status.PENDING)
        .select_related("agent")
        .order_by("-created_at")
    )
    recent_reviews = (
        GovernanceReview.objects.exclude(status=GovernanceReview.Status.PENDING)
        .select_related("agent")
        .order_by("-created_at")[:20]
    )

    now = timezone.now()
    active_approvals = (
        Approval.objects.filter(is_consumed=False, expires_at__gt=now)
        .select_related("agent")
        .order_by("-created_at")
    )
    recent_approvals = (
        Approval.objects.exclude(is_consumed=False, expires_at__gt=now)
        .select_related("agent")
        .order_by("-created_at")[:30]
    )

    audit_entries = AuditLog.objects.order_by("-created_at")[:100]

    # Overview stats — build (value, label, count) triples so the template
    # never needs dict[variable] lookups, which Django templates don't support.
    _status_raw = {i["status"]: i["c"] for i in agents.values("status").annotate(c=Count("id"))}
    _kind_raw   = {i["kind"]: i["c"]   for i in agents.values("kind").annotate(c=Count("id"))}
    _mode_raw   = {i["integration_mode"]: i["c"] for i in agents.values("integration_mode").annotate(c=Count("id"))}

    status_rows = [(v, label, _status_raw.get(v, 0)) for v, label in Agent.Status.choices]
    kind_rows   = [(v, label, _kind_raw.get(v, 0))   for v, label in Agent.Kind.choices]
    mode_rows   = [(v, label, _mode_raw.get(v, 0))   for v, label in Agent.IntegrationMode.choices]

    context = {
        "agents": agents,
        "pending_reviews": pending_reviews,
        "recent_reviews": recent_reviews,
        "active_approvals": active_approvals,
        "recent_approvals": recent_approvals,
        "audit_entries": audit_entries,
        "status_rows": status_rows,
        "kind_rows": kind_rows,
        "mode_rows": mode_rows,
        "pending_count": pending_reviews.count(),
        "active_approval_count": active_approvals.count(),
        "tier4_agents": [a for a in agents if a.risk_tier >= 4],
        "now": now,
    }
    return render(request, "controlplane/manage.html", context)


# ──────────────────────────────────────────────────────────────────────────────
# UX-1 wave 1: Unified operations console (/console/)
# ──────────────────────────────────────────────────────────────────────────────

@login_required
@ensure_csrf_cookie
def console(request):
    """
    Server-rendered ops console: approvals inbox (with in-flow review request),
    workflows (run), evals (run), costs, notifications. Role- and tenant-aware.
    """
    from django.contrib import messages
    from django.shortcuts import redirect

    from .models import (
        AgentBlueprint, EvalSuite, KnowledgeDocument, Notification,
        RegistryEntry, Workflow, WorkflowRun,
    )
    from .security import can_access_agent, can_access_business_unit, has_role

    is_approver = has_role(request.user, "agent_approver", "platform_admin")
    is_builder = has_role(request.user, "agent_builder", "agent_approver", "platform_admin")
    cross = _is_cross_tenant(request.user)
    bu_id = None if cross else _user_business_unit_id(request.user)

    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "request_review" and is_builder:
                agent = get_object_or_404(Agent, id=request.POST["agent_id"])
                if not can_access_agent(request.user, agent):
                    raise PermissionError("No access to this agent.")
                if agent.governance_reviews.filter(status="pending").exists():
                    raise ValueError("A review is already pending for this agent.")
                GovernanceReview.objects.create(
                    agent=agent, reviewer="(unassigned)",
                    notes=f"Review requested by {request.user.username} via console.",
                )
                AuditLog.objects.create(
                    actor=request.user.username, action="governance_review_requested",
                    resource_type="Agent", resource_id=str(agent.id), payload={},
                )
                messages.success(request, f"Review requested for {agent.name}.")
            elif action == "decide_review" and is_approver:
                review = get_object_or_404(
                    GovernanceReview, id=request.POST["review_id"], status="pending"
                )
                decision = request.POST.get("decision")
                if decision not in ("approved", "rejected"):
                    raise ValueError("Invalid decision.")
                review.status = decision
                review.reviewer = request.user.username
                review.notes = request.POST.get("notes", review.notes)
                review.completed_at = timezone.now()
                review.save(update_fields=["status", "reviewer", "notes", "completed_at"])
                AuditLog.objects.create(
                    actor=request.user.username, action=f"governance_{decision}",
                    resource_type="GovernanceReview", resource_id=str(review.id),
                    payload={"agent": review.agent.name, "via": "console"},
                )
                messages.success(request, f"Review {decision}: {review.agent.name}.")
            elif action == "run_workflow" and is_builder:
                from .services.workflow_queue import workflow_queue

                w = get_object_or_404(Workflow, id=request.POST["workflow_id"], status="active")
                if not can_access_business_unit(request.user, w.business_unit_id):
                    raise PermissionError("No access to this workflow.")
                run = workflow_queue.enqueue(w, inputs={}, triggered_by=request.user.username)
                messages.success(request, f"Workflow '{w.name}' queued (run {run.id}).")
            elif action == "run_eval" and is_approver:
                from .services.eval_service import eval_service

                suite = get_object_or_404(EvalSuite, id=request.POST["suite_id"])
                if not can_access_agent(request.user, suite.agent):
                    raise PermissionError("No access to this suite.")
                run = eval_service.run_suite(suite=suite, triggered_by=request.user.username)
                messages.success(
                    request,
                    f"Eval '{suite.name}': {run.passed_cases}/{run.total_cases} passed.",
                )
            elif action == "approve_blueprint" and is_approver:
                bp = get_object_or_404(AgentBlueprint, id=request.POST["blueprint_id"])
                if not bp.can_transition_to(AgentBlueprint.Status.APPROVED):
                    raise ValueError(f"Cannot approve a blueprint in '{bp.status}' status.")
                bp.status = AgentBlueprint.Status.APPROVED
                bp.approved_by = request.user
                bp.approved_at = timezone.now()
                bp.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
                AuditLog.objects.create(
                    actor=request.user.username, action="blueprint_approved",
                    resource_type="AgentBlueprint", resource_id=str(bp.id),
                    payload={"via": "console"},
                )
                messages.success(request, f"Blueprint approved: {bp.agent_name}.")
            elif action == "build_blueprint" and is_builder:
                from .services.factory import build_compiler

                bp = get_object_or_404(AgentBlueprint, id=request.POST["blueprint_id"])
                agent = build_compiler.build(bp, built_by=request.user.username)
                messages.success(request, f"Built sandbox agent '{agent.slug}' from blueprint.")
            elif action == "approve_registry" and is_approver:
                entry = get_object_or_404(RegistryEntry, id=request.POST["entry_id"])
                entry.review_status = "approved"
                entry.save(update_fields=["review_status", "updated_at"])
                AuditLog.objects.create(
                    actor=request.user.username, action="registry_entry_approved",
                    resource_type="RegistryEntry", resource_id=str(entry.id),
                    payload={"via": "console"},
                )
                messages.success(request, f"Registry entry approved: {entry.name}.")
            elif action == "mark_read":
                Notification.objects.filter(
                    user=request.user, read_at__isnull=True
                ).update(read_at=timezone.now())
            else:
                messages.error(request, "Unknown action or insufficient role.")
        except (PermissionError, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("controlplane:console")

    agents_qs = Agent.objects.all()
    reviews_qs = GovernanceReview.objects.filter(status="pending").select_related("agent")
    workflows_qs = Workflow.objects.filter(status="active")
    suites_qs = EvalSuite.objects.filter(is_active=True).select_related("agent")
    if not cross:
        agents_qs = agents_qs.filter(org_unit_id=bu_id)
        reviews_qs = reviews_qs.filter(agent__org_unit_id=bu_id)
        workflows_qs = workflows_qs.filter(business_unit_id=bu_id)
        suites_qs = suites_qs.filter(agent__org_unit_id=bu_id)

    month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cost_rows = (
        AgentRun.objects.filter(started_at__gte=month_start, status="completed",
                                agent__in=agents_qs)
        .values("agent__name")
        .annotate(runs=Count("id"), cost=Sum("cost_usd"))
        .order_by("-cost")[:10]
    )

    blueprints_qs = AgentBlueprint.objects.exclude(
        status__in=[AgentBlueprint.Status.RETIRED, AgentBlueprint.Status.DEPLOYED]
    ).select_related("built_agent")
    knowledge_qs = KnowledgeDocument.objects.all()
    if not cross:
        blueprints_qs = blueprints_qs.filter(insight__business_unit_id=bu_id)
        knowledge_qs = knowledge_qs.filter(business_unit_id=bu_id) | knowledge_qs.filter(business_unit__isnull=True)

    persona = {
        "agents_total": agents_qs.count(),
        "agents_production": agents_qs.filter(status="production").count(),
        "pending_reviews": reviews_qs.count(),
    }

    notifications = Notification.objects.filter(user=request.user)[:20]
    context = {
        "persona": persona,
        "blueprints": blueprints_qs[:25],
        "knowledge_docs": knowledge_qs.order_by("-created_at")[:15],
        "registry_discovered": RegistryEntry.objects.filter(review_status="discovered")[:15]
                               if is_approver else [],
        "is_approver": is_approver,
        "is_builder": is_builder,
        "pending_reviews": reviews_qs[:25],
        "reviewable_agents": agents_qs.filter(status__in=["draft", "review"])[:25] if is_builder else [],
        "valid_approvals": Approval.objects.filter(
            is_consumed=False, expires_at__gt=timezone.now(),
            agent__in=agents_qs,
        ).select_related("agent")[:25],
        "workflows": workflows_qs[:25],
        "recent_runs": WorkflowRun.objects.filter(workflow__in=workflows_qs)
                       .select_related("workflow").order_by("-updated_at")[:10],
        "suites": suites_qs[:25],
        "cost_rows": cost_rows,
        "cost_total": AgentRun.objects.filter(
            started_at__gte=month_start, status="completed", agent__in=agents_qs
        ).aggregate(c=Sum("cost_usd"))["c"] or 0,
        "notifications": notifications,
        "unread": Notification.objects.filter(user=request.user, read_at__isnull=True).count(),
    }
    return render(request, "controlplane/console.html", context)
