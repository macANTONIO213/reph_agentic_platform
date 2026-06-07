from django.contrib import admin
from django.contrib import messages
from django.utils.html import format_html

from .models import (
    Agent, AgentEmbedding, AgentFeedback, AgentRun, AgentToolCall, AgentVersion,
    Approval, AuditLog,
    BusinessUnit, ConversationSession, DataConnector, Division,
    DocumentChunk, EvalCase, EvalRun, EvalSuite,
    GovernanceReview, KnowledgeDocument, OrgProcess,
    TelemetryEvent, UserProfile, WorkStream,
)
from .services.governance import governance, TransitionError

# ──────────────────────────────────────────────────────────────────────────────
# Organisational hierarchy
# ──────────────────────────────────────────────────────────────────────────────

class DivisionInline(admin.TabularInline):
    model = Division
    extra = 1
    fields = ("name", "code", "description", "is_active")
    prepopulated_fields = {"code": ("name",)}


class WorkStreamInline(admin.TabularInline):
    model = WorkStream
    extra = 1
    fields = ("name", "code", "description", "is_active")
    prepopulated_fields = {"code": ("name",)}


class ProcessInline(admin.TabularInline):
    model = OrgProcess
    extra = 1
    fields = ("name", "code", "description", "is_active")
    prepopulated_fields = {"code": ("name",)}


@admin.register(BusinessUnit)
class BusinessUnitAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "division_count", "agent_count", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}
    inlines = [DivisionInline]
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Divisions")
    def division_count(self, obj):
        return obj.divisions.count()

    @admin.display(description="Agents")
    def agent_count(self, obj):
        return obj.agents.count()


@admin.register(Division)
class DivisionAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "business_unit", "workstream_count", "agent_count", "is_active")
    list_filter = ("is_active", "business_unit")
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}
    inlines = [WorkStreamInline]
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Work streams")
    def workstream_count(self, obj):
        return obj.work_streams.count()

    @admin.display(description="Agents")
    def agent_count(self, obj):
        return obj.agents.count()


@admin.register(WorkStream)
class WorkStreamAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "division", "process_count", "agent_count", "is_active")
    list_filter = ("is_active", "division__business_unit", "division")
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}
    inlines = [ProcessInline]
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Processes")
    def process_count(self, obj):
        return obj.processes.count()

    @admin.display(description="Agents")
    def agent_count(self, obj):
        return obj.agents.count()


@admin.register(OrgProcess)
class OrgProcessAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "work_stream", "agent_count", "is_active")
    list_filter = ("is_active", "work_stream__division__business_unit", "work_stream__division", "work_stream")
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Agents")
    def agent_count(self, obj):
        return obj.agents.count()


# ──────────────────────────────────────────────────────────────────────────────
# Agent
# ──────────────────────────────────────────────────────────────────────────────

def _force_transition_action(target_status):
    """Factory: returns an admin action that force-transitions to target_status."""
    def action_fn(modeladmin, request, queryset):
        reason = request.POST.get("_reason", "").strip()
        if not reason:
            messages.error(request, "Force transition requires a reason. Use the command line or add a reason via URL param ?_reason=…")
            return
        succeeded, failed = [], []
        for agent in queryset:
            try:
                governance.transition(
                    actor=request.user,
                    agent=agent,
                    to_status=target_status,
                    reason=reason,
                    source="admin",
                    ip=request.META.get("REMOTE_ADDR"),
                    bypass=True,
                )
                succeeded.append(agent.name)
            except TransitionError as e:
                failed.append(f"{agent.name}: {e}")
        if succeeded:
            messages.success(request, f"Force-transitioned to '{target_status}': {', '.join(succeeded)}")
        for msg in failed:
            messages.error(request, msg)

    action_fn.short_description = f"Force transition → {target_status} (break-glass, audited)"
    action_fn.__name__ = f"force_transition_to_{target_status}"
    return action_fn


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = (
        "name", "kind", "integration_mode", "platform", "org_unit", "org_division",
        "status", "risk_tier", "version", "telemetry_enabled",
    )
    list_filter = (
        "kind", "integration_mode", "platform", "status", "risk_tier",
        "org_unit", "org_division",
    )
    search_fields = ("name", "owner", "technical_owner", "purpose", "business_unit")
    actions = [
        _force_transition_action("draft"),
        _force_transition_action("review"),
        _force_transition_action("pilot"),
        _force_transition_action("production"),
        _force_transition_action("retired"),
    ]

    # Governance-sensitive fields are read-only: admins change status via
    # force-transition actions (audited) not by direct field edit.
    _GOVERNANCE_RO = ("status", "risk_tier", "tool_names", "model_id", "deployed_at")
    readonly_fields = ("created_at", "updated_at", "deployed_at", "status", "risk_tier",
                       "tool_names", "model_id", "governance_level_display")

    fieldsets = (
        ("Identity", {
            "fields": ("slug", "name", "kind", "integration_mode", "platform",
                       "status", "version", "risk_tier", "governance_level_display"),
            "description": "Status and risk tier are read-only. Use the 'Force transition' action to change status.",
        }),
        ("Organisation", {
            "fields": ("org_unit", "org_division", "org_work_stream", "org_process", "business_unit"),
            "description": "Use the structured fields; business_unit is kept for legacy display.",
        }),
        ("Ownership", {
            "fields": ("owner", "technical_owner"),
        }),
        ("Configuration", {
            "fields": ("purpose", "system_prompt", "model_id", "endpoint_url",
                       "data_sources", "tool_names", "telemetry_enabled", "guardrail_level"),
            "description": "model_id and tool_names are read-only; change via GovernanceService.",
        }),
        ("Metrics", {
            "fields": ("monthly_active_users", "monthly_runs", "monthly_cost_usd", "satisfaction_score"),
        }),
        ("Dates", {
            "fields": ("deployed_at", "next_review_at", "created_at", "updated_at"),
        }),
    )

    @admin.display(description="Governance level")
    def governance_level_display(self, obj):
        level = obj.governance_level
        colour = {"full": "#15803d", "medium": "#b45309", "attested": "#6366f1"}.get(level, "#64748b")
        return format_html('<span style="color:{};font-weight:600">{}</span>', colour, level)

    def save_model(self, request, obj, form, change):
        if not change:
            # New agent: route through GovernanceService so audit + validation run.
            try:
                data = {
                    "name": obj.name,
                    "platform": obj.platform,
                    "owner": obj.owner,
                    "technical_owner": obj.technical_owner,
                    "purpose": obj.purpose,
                    "system_prompt": obj.system_prompt,
                    "kind": obj.kind,
                    "integration_mode": obj.integration_mode,
                    "business_unit": obj.business_unit,
                    "risk_tier": obj.risk_tier,
                    "version": obj.version,
                    "endpoint_url": obj.endpoint_url,
                    "model_id": obj.model_id,
                    "org_unit_id": str(obj.org_unit_id) if obj.org_unit_id else None,
                    "org_division_id": str(obj.org_division_id) if obj.org_division_id else None,
                    "org_work_stream_id": str(obj.org_work_stream_id) if obj.org_work_stream_id else None,
                    "org_process_id": str(obj.org_process_id) if obj.org_process_id else None,
                    "data_sources": obj.data_sources,
                    "tool_names": obj.tool_names,
                    "slug": obj.slug or "",
                }
                created = governance.register_agent(
                    actor=request.user,
                    data=data,
                    source="admin",
                    ip=request.META.get("REMOTE_ADDR"),
                )
                # Copy PK back so Django admin redirects to the right object.
                obj.id = created.id
                obj.slug = created.slug
                # Don't call super().save_model — register_agent already saved it.
                return
            except Exception as e:
                messages.error(request, f"Registration failed: {e}")
                return

        # Editing existing agent: strip governance-sensitive fields so they
        # can't be changed via direct save.  Allowed edits: identity text,
        # ownership, org links, metrics, dates.
        safe_fields = [
            "name", "kind", "integration_mode", "platform",
            "business_unit", "owner", "technical_owner", "purpose", "system_prompt",
            "org_unit", "org_division", "org_work_stream", "org_process",
            "endpoint_url", "telemetry_enabled", "monthly_active_users",
            "monthly_runs", "monthly_cost_usd", "satisfaction_score",
            "version", "next_review_at", "updated_at",
        ]
        AuditLog.objects.create(
            actor=request.user.username,
            action="agent.admin_edit",
            resource_type="Agent",
            resource_id=str(obj.id),
            payload={"source": "admin", "changed_fields": list(form.changed_data)},
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        obj.save(update_fields=safe_fields)


# ──────────────────────────────────────────────────────────────────────────────
# Runs & telemetry
# ──────────────────────────────────────────────────────────────────────────────

class AgentToolCallInline(admin.TabularInline):
    model = AgentToolCall
    extra = 0
    readonly_fields = ("tool_name", "input_payload", "output_payload", "duration_ms", "created_at")


class AgentFeedbackInline(admin.TabularInline):
    model = AgentFeedback
    extra = 0
    readonly_fields = ("rating", "comment", "submitted_by", "created_at")


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = (
        "agent", "status", "user_label", "channel",
        "model_id", "input_tokens", "output_tokens", "latency_ms", "started_at",
    )
    list_filter = ("status", "channel", "agent", "model_id")
    search_fields = ("input_text", "output_text", "user_label")
    readonly_fields = ("started_at", "completed_at")
    inlines = [AgentToolCallInline, AgentFeedbackInline]


@admin.register(TelemetryEvent)
class TelemetryEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "agent", "actor", "business_unit", "created_at")
    list_filter = ("event_type", "business_unit", "agent")
    search_fields = ("event_type", "actor")
    readonly_fields = ("created_at",)


# ──────────────────────────────────────────────────────────────────────────────
# Governance
# ──────────────────────────────────────────────────────────────────────────────

@admin.register(GovernanceReview)
class GovernanceReviewAdmin(admin.ModelAdmin):
    list_display = ("agent", "reviewer", "status", "created_at", "completed_at")
    list_filter = ("status",)
    search_fields = ("agent__name", "reviewer", "notes")
    readonly_fields = ("created_at",)


@admin.register(AgentVersion)
class AgentVersionAdmin(admin.ModelAdmin):
    list_display = ("agent", "version", "model_id", "created_at")
    list_filter = ("agent",)
    readonly_fields = ("created_at",)


@admin.register(ConversationSession)
class ConversationSessionAdmin(admin.ModelAdmin):
    list_display = ("agent", "user_label", "message_count", "created_at", "updated_at")
    list_filter = ("agent",)
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Messages")
    def message_count(self, obj):
        return len(obj.messages)


# ──────────────────────────────────────────────────────────────────────────────
# Phase A governance: Approvals + Audit log
# ──────────────────────────────────────────────────────────────────────────────

@admin.register(Approval)
class ApprovalAdmin(admin.ModelAdmin):
    list_display = ("agent", "approved_by_username", "scope", "expires_at", "is_consumed", "is_valid_display", "created_at")
    list_filter = ("is_consumed", "agent", "scope")
    search_fields = ("agent__name", "approved_by_username", "notes")
    readonly_fields = ("created_at", "approved_by")
    fields = ("agent", "approved_by", "approved_by_username", "scope", "notes", "expires_at", "is_consumed", "created_at")

    @admin.display(description="Valid?", boolean=True)
    def is_valid_display(self, obj):
        return obj.is_valid


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "resource_type", "resource_id", "ip_address")
    list_filter = ("action", "resource_type")
    search_fields = ("actor", "action", "resource_id")
    readonly_fields = ("created_at", "actor", "action", "resource_type", "resource_id", "payload", "ip_address")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ── B3: Eval Suites ───────────────────────────────────────────────────────────

class EvalCaseInline(admin.TabularInline):
    model = EvalCase
    extra = 1
    fields = ("name", "input_message", "expected_keywords", "must_not_contain",
              "max_latency_ms", "weight")


class EvalRunInline(admin.TabularInline):
    model = EvalRun
    extra = 0
    readonly_fields = ("status", "pass_rate", "passed", "passed_cases",
                       "total_cases", "triggered_by", "executed_at")
    fields = readonly_fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(EvalSuite)
class EvalSuiteAdmin(admin.ModelAdmin):
    list_display  = ("name", "agent", "pass_threshold", "is_active",
                     "latest_run_status", "created_at")
    list_filter   = ("is_active", "agent")
    search_fields = ("name", "agent__name")
    inlines       = [EvalCaseInline, EvalRunInline]
    actions       = ["run_suite_action"]

    @admin.display(description="Latest run")
    def latest_run_status(self, obj):
        run = obj.runs.order_by("-executed_at").first()
        if not run:
            return "—"
        icon = "✓" if run.passed else "✗"
        return f"{icon} {run.pass_rate}% ({run.executed_at:%Y-%m-%d})"

    @admin.action(description="▶ Run eval suite now")
    def run_suite_action(self, request, queryset):
        from controlplane.services.eval_service import eval_service
        for suite in queryset:
            run = eval_service.run_suite(suite=suite, triggered_by=request.user.username)
            level = messages.SUCCESS if run.passed else messages.WARNING
            self.message_user(
                request,
                f"Suite '{suite.name}': {run.pass_rate}% "
                f"({'PASSED' if run.passed else 'FAILED'}) — {run.passed_cases}/{run.total_cases} cases",
                level=level,
            )


@admin.register(EvalRun)
class EvalRunAdmin(admin.ModelAdmin):
    list_display  = ("suite", "status", "pass_rate", "passed", "passed_cases",
                     "total_cases", "triggered_by", "executed_at")
    list_filter   = ("passed", "status", "suite__agent")
    readonly_fields = ("suite", "status", "pass_rate", "passed", "passed_cases",
                       "total_cases", "case_results", "error_detail",
                       "triggered_by", "executed_at", "completed_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ── B1: User profiles (tenant scoping) ────────────────────────────────────────

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display  = ("user", "role", "business_unit", "is_cross_tenant_display", "created_at")
    list_filter   = ("role", "business_unit")
    search_fields = ("user__username", "user__email", "business_unit__name")
    autocomplete_fields = ("business_unit",)
    fields        = ("user", "role", "business_unit")

    @admin.display(boolean=True, description="Cross-tenant?")
    def is_cross_tenant_display(self, obj):
        return obj.is_cross_tenant


# ── C1: Agent embeddings ──────────────────────────────────────────────────────

@admin.register(AgentEmbedding)
class AgentEmbeddingAdmin(admin.ModelAdmin):
    list_display   = ("agent", "model_id", "has_vector", "embedded_at")
    list_filter    = ("model_id",)
    search_fields  = ("agent__name",)
    readonly_fields = ("agent", "model_id", "text_hash", "embedded_at")
    actions        = ["re_embed_action"]

    @admin.display(boolean=True, description="Has vector?")
    def has_vector(self, obj):
        return bool(obj.vector)

    @admin.action(description="↻ Re-embed selected agents")
    def re_embed_action(self, request, queryset):
        from controlplane.services.embeddings import embedding_service
        count = 0
        for emb in queryset:
            embedding_service.embed_agent(emb.agent)
            count += 1
        self.message_user(request, f"Re-embedded {count} agent(s).")


# ── C2: Knowledge documents ───────────────────────────────────────────────────

class DocumentChunkInline(admin.TabularInline):
    model = DocumentChunk
    extra = 0
    readonly_fields = ("chunk_index", "token_count", "has_vector_display")
    fields = ("chunk_index", "token_count", "has_vector_display")
    can_delete = False

    @admin.display(description="Embedded?")
    def has_vector_display(self, obj):
        return "✓" if obj.vector else "✗"

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(KnowledgeDocument)
class KnowledgeDocumentAdmin(admin.ModelAdmin):
    list_display   = ("title", "business_unit", "file_type", "status",
                      "chunk_count", "uploaded_by", "created_at")
    list_filter    = ("status", "file_type", "business_unit")
    search_fields  = ("title", "description", "uploaded_by")
    readonly_fields = ("status", "chunk_count", "error_detail", "created_at", "updated_at")
    inlines        = [DocumentChunkInline]
    actions        = ["reindex_action"]

    @admin.action(description="↻ Re-index selected documents")
    def reindex_action(self, request, queryset):
        from controlplane.services.rag import rag_service
        for doc in queryset:
            rag_service.reindex_document(doc)
        self.message_user(request, f"Re-indexed {queryset.count()} document(s).")


# ── C3: Data connectors ───────────────────────────────────────────────────────

@admin.register(DataConnector)
class DataConnectorAdmin(admin.ModelAdmin):
    list_display  = ("name", "connector_type", "business_unit", "is_active", "created_at")
    list_filter   = ("connector_type", "is_active", "business_unit")
    search_fields = ("name", "description")
    fields        = ("name", "connector_type", "business_unit", "description",
                     "config", "is_active", "created_by")
