import uuid

from django.contrib.auth.models import User
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


# ──────────────────────────────────────────────────────────────────────────────
# Organisational hierarchy  (configurable via Django admin)
# ──────────────────────────────────────────────────────────────────────────────

class BusinessUnit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, unique=True)
    code = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Division(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    business_unit = models.ForeignKey(
        BusinessUnit, on_delete=models.CASCADE,
        related_name="divisions", null=True, blank=True,
        help_text="Leave blank if this division spans all business units.",
    )
    name = models.CharField(max_length=120)
    code = models.SlugField()
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("business_unit", "code")]

    def __str__(self):
        if self.business_unit_id:
            return f"{self.business_unit.name} / {self.name}"
        return self.name


class WorkStream(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    division = models.ForeignKey(
        Division, on_delete=models.CASCADE, related_name="work_streams"
    )
    name = models.CharField(max_length=120)
    code = models.SlugField()
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("division", "code")]

    def __str__(self):
        return f"{self.division.name} / {self.name}"


class OrgProcess(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work_stream = models.ForeignKey(
        WorkStream, on_delete=models.CASCADE, related_name="processes"
    )
    name = models.CharField(max_length=120)
    code = models.SlugField()
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("work_stream", "code")]
        verbose_name = "Process"
        verbose_name_plural = "Processes"

    def __str__(self):
        return f"{self.work_stream.name} / {self.name}"


class Agent(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        REVIEW = "review", "Review"
        PILOT = "pilot", "Pilot"
        PRODUCTION = "production", "Production"
        RETIRED = "retired", "Retired"

    class Kind(models.TextChoices):
        CUSTOM = "custom", "Custom (first-party)"
        EXTERNAL = "external", "External"

    class IntegrationMode(models.TextChoices):
        SDK = "sdk", "SDK / Callback (full governance)"
        PROXY = "proxy", "Proxy / Endpoint (medium governance)"
        ATTESTATION = "attestation", "Attestation-only (registered)"

    class Platform(models.TextChoices):
        DJANGO = "django_runtime", "Django Runtime"
        AZURE_AI = "azure_ai_foundry", "Azure AI Foundry / Azure OpenAI"
        COPILOT = "copilot_studio", "Microsoft Copilot Studio"
        BEDROCK = "bedrock", "AWS Bedrock"
        CUSTOM = "custom_api", "Custom API Agent"
        VENDOR = "vendor", "Vendor Platform"
        EMBEDDED = "embedded", "Internal App Embed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.EXTERNAL)
    integration_mode = models.CharField(
        max_length=20, choices=IntegrationMode.choices, default=IntegrationMode.PROXY,
        help_text="How this agent connects to the control plane (determines governance fidelity).",
    )
    platform = models.CharField(max_length=40, choices=Platform.choices)
    business_unit = models.CharField(max_length=80)
    owner = models.CharField(max_length=120)
    technical_owner = models.CharField(max_length=120)
    purpose = models.TextField()
    system_prompt = models.TextField()
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.REVIEW)
    risk_tier = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(4)]
    )
    version = models.CharField(max_length=20, default="1.0")
    endpoint_url = models.URLField(blank=True, default="")
    model_id = models.CharField(max_length=80, blank=True, default="", help_text="Override the adapter's default model (e.g. gpt-4o, claude-opus-4-8)")
    org_unit = models.ForeignKey(
        "BusinessUnit", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agents", verbose_name="Business unit",
    )
    org_division = models.ForeignKey(
        "Division", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agents", verbose_name="Division",
    )
    org_work_stream = models.ForeignKey(
        "WorkStream", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agents", verbose_name="Work stream",
    )
    org_process = models.ForeignKey(
        "OrgProcess", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="agents", verbose_name="Process",
    )
    data_sources = models.JSONField(default=list, blank=True)
    tool_names = models.JSONField(default=list, blank=True)
    telemetry_enabled = models.BooleanField(default=True)
    guardrail_level = models.CharField(
        max_length=10,
        choices=[
            ("off",   "Off — log only, never block"),
            ("warn",  "Warn — flag finding, continue run"),
            ("block", "Block — abort run on HIGH findings"),
        ],
        default="block",
        help_text="Content guardrail enforcement level for this agent.",
    )
    quality_alert = models.BooleanField(
        default=False,
        help_text="Set by compute_baselines management command when satisfaction drops >20% below 7-day baseline.",
    )
    # D1: Budget controls
    budget_usd_monthly = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Monthly spend cap in USD. Null = no cap. Checked by compute_budgets command.",
    )
    budget_alert = models.BooleanField(
        default=False,
        help_text="Set by compute_budgets when monthly_cost_usd exceeds budget_usd_monthly.",
    )
    monthly_active_users = models.PositiveIntegerField(default=0)
    monthly_runs = models.PositiveIntegerField(default=0)
    monthly_cost_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    satisfaction_score = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    deployed_at = models.DateTimeField(null=True, blank=True)
    next_review_at = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    ALLOWED_TRANSITIONS: dict = {
        "draft": {"review"},
        "review": {"draft", "pilot"},
        "pilot": {"review", "production"},
        "production": {"pilot", "retired"},
        "retired": set(),
    }

    @property
    def is_live(self):
        return self.status in {self.Status.PILOT, self.Status.PRODUCTION}

    @property
    def governance_level(self) -> str:
        return {"sdk": "full", "proxy": "medium", "attestation": "attested"}.get(
            self.integration_mode, "attested"
        )

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, set())

    def transition_to(self, new_status: str, bypass_governance: bool = False) -> None:
        if not self.can_transition_to(new_status):
            allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
            raise ValueError(
                f"Cannot transition '{self.name}' from '{self.status}' to '{new_status}'. "
                f"Allowed next states: {sorted(allowed) or 'none'}."
            )
        if new_status == self.Status.PRODUCTION and not bypass_governance:
            has_approval = self.governance_reviews.filter(
                status=GovernanceReview.Status.APPROVED
            ).exists()
            if not has_approval:
                raise ValueError(
                    f"Cannot promote '{self.name}' to production: "
                    "an approved governance review is required. "
                    "Create and approve a GovernanceReview first."
                )
        self.status = new_status
        if new_status == self.Status.PRODUCTION:
            self.deployed_at = self.deployed_at or timezone.now()
            self.save(update_fields=["status", "deployed_at", "updated_at"])
            self._snapshot_version()
        else:
            self.save(update_fields=["status", "deployed_at", "updated_at"])

    def _snapshot_version(self) -> None:
        AgentVersion.objects.create(
            agent=self,
            version=self.version,
            system_prompt=self.system_prompt,
            tool_names=self.tool_names,
            model_id=self.model_id,
        )

    def mark_deployed(self):
        self.transition_to(self.Status.PRODUCTION)


class AgentRun(models.Model):
    class Status(models.TextChoices):
        STARTED = "started", "Started"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="runs")
    user_label = models.CharField(max_length=120, default="demo_user")
    channel = models.CharField(max_length=40, default="web")
    input_text = models.TextField()
    output_text = models.TextField(blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.STARTED)
    latency_ms = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    model_id = models.CharField(max_length=60, blank=True, default="")
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0,
                                   help_text="Stored at run completion via pricing.price_run()")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.agent.name} run {self.id}"


class AgentToolCall(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="tool_calls")
    tool_name = models.CharField(max_length=80)
    input_payload = models.JSONField(default=dict, blank=True)
    output_payload = models.JSONField(default=dict, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.tool_name} for {self.run_id}"


class TelemetryEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True, blank=True)
    run = models.ForeignKey(AgentRun, on_delete=models.SET_NULL, null=True, blank=True)
    event_type = models.CharField(max_length=80)
    actor = models.CharField(max_length=120, default="demo_user")
    business_unit = models.CharField(max_length=80, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.event_type


class GovernanceReview(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="governance_reviews")
    reviewer = models.CharField(max_length=120)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.agent.name} review ({self.status})"


class AgentFeedback(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="feedback")
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    comment = models.TextField(blank=True)
    submitted_by = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.rating}/5 for run {self.run_id}"


class AgentVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="versions")
    version = models.CharField(max_length=20)
    system_prompt = models.TextField()
    tool_names = models.JSONField(default=list)
    model_id = models.CharField(max_length=60, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.agent.name} v{self.version}"


class ConversationSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="sessions")
    user_label = models.CharField(max_length=120)
    messages = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"Session {self.id} ({self.agent.name})"


class Approval(models.Model):
    """Server-side approval record required for Tier-4 agent execution.
    Created by a user with the 'agent_approver' group (or staff).
    Single-use: consumed when a Tier-4 run is executed.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="approvals")
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="approvals_granted"
    )
    approved_by_username = models.CharField(max_length=120)
    scope = models.CharField(max_length=120, default="tier4_execution")
    notes = models.TextField(blank=True)
    expires_at = models.DateTimeField(help_text="Approval is invalid after this time.")
    is_consumed = models.BooleanField(default=False, help_text="Set to true once used for a run.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Approval for {self.agent.name} by {self.approved_by_username}"

    @property
    def is_valid(self) -> bool:
        return not self.is_consumed and timezone.now() < self.expires_at


class UserProfile(models.Model):
    """
    Extends Django's built-in User with tenant scoping.

    Every user is assigned to a home BusinessUnit (their "tenant").
    Platform admins and staff are cross-tenant — they see all agents.
    Builders are scoped: they may only register and manage agents within
    their own business unit.

    Created automatically via post_save signal when a new User is saved.
    """
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile"
    )
    business_unit = models.ForeignKey(
        "BusinessUnit", on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="members",
        help_text="Home business unit for tenant scoping. Leave blank for cross-tenant (staff/admin) users.",
    )
    role = models.CharField(
        max_length=30,
        choices=[
            ("viewer",          "Viewer — read-only across assigned BU"),
            ("agent_builder",   "Agent Builder — register/edit agents in own BU"),
            ("agent_approver",  "Agent Approver — approve governance reviews"),
            ("platform_admin",  "Platform Admin — full cross-tenant access"),
        ],
        default="viewer",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username"]

    def __str__(self):
        bu = self.business_unit.name if self.business_unit else "cross-tenant"
        return f"{self.user.username} ({self.role} @ {bu})"

    @property
    def is_cross_tenant(self) -> bool:
        """Staff, superusers, and platform_admins bypass BU scoping."""
        # Refresh role from DB to avoid Django related-object cache stale reads
        current_role = (
            UserProfile.objects.filter(pk=self.pk).values_list("role", flat=True).first()
            if self.pk else self.role
        )
        return (
            self.user.is_staff
            or self.user.is_superuser
            or current_role == "platform_admin"
            or self.user.groups.filter(name="platform_admin").exists()
        )

    def can_access_agent(self, agent: "Agent") -> bool:
        """Return True if this user's tenant scope covers the given agent."""
        if self.is_cross_tenant:
            return True
        # Refresh BU from DB to avoid cache staleness
        current_bu_id = (
            UserProfile.objects.filter(pk=self.pk).values_list("business_unit_id", flat=True).first()
            if self.pk else self.business_unit_id
        )
        if current_bu_id is None:
            return False
        # Agent scoped via FK — compare PKs
        agent_bu_id = agent.org_unit_id or (agent.org_unit.pk if agent.org_unit else None)
        if agent_bu_id is not None:
            return agent_bu_id == current_bu_id
        # Fall back to legacy text field — load BU name
        bu_name = BusinessUnit.objects.filter(pk=current_bu_id).values_list("name", flat=True).first()
        return agent.business_unit == bu_name

    def can_register_in(self, business_unit: "BusinessUnit") -> bool:
        """Return True if this user may register agents in the given BU."""
        if self.is_cross_tenant:
            return True
        if self.role not in {"agent_builder", "agent_approver"}:
            return False
        return self.business_unit_id == business_unit.pk


# ── Signal: auto-create UserProfile on new User ──────────────────────────────

from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=User)
def _create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


# ──────────────────────────────────────────────────────────────────────────────
# C1 — Agent Embeddings  (semantic search)
# ──────────────────────────────────────────────────────────────────────────────

class AgentEmbedding(models.Model):
    """
    Stores a vector embedding of an agent's searchable metadata.
    Regenerated whenever name / purpose / business_unit / tool_names change.

    On Postgres + pgvector: stored as a native vector column for fast cosine search.
    On SQLite (local dev): stored as JSON list; cosine similarity computed in Python.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.OneToOneField(
        Agent, on_delete=models.CASCADE, related_name="embedding"
    )
    # Embedding stored as JSON array — works on both SQLite and Postgres.
    # On Postgres with pgvector enabled, EmbeddingService uses raw SQL for ANN search.
    vector = models.JSONField(
        default=list,
        help_text="Serialised float list from text-embedding-3-small (1536 dims).",
    )
    model_id = models.CharField(max_length=80, default="text-embedding-3-small")
    text_hash = models.CharField(
        max_length=64, blank=True,
        help_text="SHA-256 of the embedded text — used to skip unchanged agents.",
    )
    embedded_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-embedded_at"]

    def __str__(self):
        return f"Embedding for {self.agent.name}"


# ──────────────────────────────────────────────────────────────────────────────
# C2 — Knowledge Documents  (RAG pipeline)
# ──────────────────────────────────────────────────────────────────────────────

class KnowledgeDocument(models.Model):
    """
    An enterprise document ingested into the knowledge base.
    Chunked and embedded for retrieval-augmented generation.
    """
    class Status(models.TextChoices):
        PENDING    = "pending",    "Pending ingestion"
        PROCESSING = "processing", "Processing"
        READY      = "ready",      "Ready"
        ERROR      = "error",      "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    source_url = models.URLField(blank=True)
    # Tenant scoping — only agents in the same BU can retrieve this doc.
    business_unit = models.ForeignKey(
        "BusinessUnit", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="knowledge_documents",
        help_text="Leave blank for platform-wide documents accessible to all agents.",
    )
    uploaded_by = models.CharField(max_length=120)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    file_type = models.CharField(
        max_length=10, blank=True,
        help_text="pdf / docx / txt / md",
    )
    raw_text = models.TextField(
        blank=True,
        help_text="Full extracted text (set during ingestion).",
    )
    chunk_count = models.PositiveIntegerField(default=0)
    error_detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class DocumentChunk(models.Model):
    """
    A single chunk of a KnowledgeDocument with its embedding vector.
    Chunks are the unit of retrieval in the RAG pipeline.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        KnowledgeDocument, on_delete=models.CASCADE, related_name="chunks"
    )
    chunk_index = models.PositiveIntegerField()
    text = models.TextField()
    # Same dual-mode storage as AgentEmbedding
    vector = models.JSONField(default=list)
    token_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document", "chunk_index"]
        unique_together = [("document", "chunk_index")]

    def __str__(self):
        return f"{self.document.title} chunk {self.chunk_index}"


# ──────────────────────────────────────────────────────────────────────────────
# C3 — Data Connectors
# ──────────────────────────────────────────────────────────────────────────────

class DataConnector(models.Model):
    """
    A registered data source an agent can query at runtime.
    Connection config is encrypted-at-rest in production (env-var key recommended).
    """
    class ConnectorType(models.TextChoices):
        SQL      = "sql",      "SQL Database"
        REST     = "rest",     "REST API"
        GRAPHQL  = "graphql",  "GraphQL API"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120, unique=True)
    connector_type = models.CharField(max_length=12, choices=ConnectorType.choices)
    # Tenant scoping
    business_unit = models.ForeignKey(
        "BusinessUnit", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="data_connectors",
    )
    # Connection config — stored as JSON.
    # SQL:     {"url": "postgresql://...", "schema": "public"}
    # REST:    {"base_url": "https://api.example.com", "auth_header": "Bearer {token}"}
    # GraphQL: {"endpoint": "https://api.example.com/graphql", "auth_header": "..."}
    config = models.JSONField(
        default=dict,
        help_text="Connection configuration (do not store raw secrets — use env var references).",
    )
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.connector_type})"


# ──────────────────────────────────────────────────────────────────────────────
# B3 — Eval Suite / Eval Run  (production gate)
# ──────────────────────────────────────────────────────────────────────────────

class EvalSuite(models.Model):
    """
    A named collection of test cases for an agent.
    An agent must have at least one passing EvalRun against its active suite
    before it can be promoted to production.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(
        Agent, on_delete=models.CASCADE, related_name="eval_suites"
    )
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    pass_threshold = models.DecimalField(
        max_digits=4, decimal_places=1, default=80.0,
        help_text="Minimum pass rate (%) required for the suite to be considered passing.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Only one active suite per agent is checked at gate time.",
    )
    created_by = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.agent.name} / {self.name}"

    @property
    def latest_passing_run(self):
        return self.runs.filter(passed=True).order_by("-executed_at").first()


class EvalCase(models.Model):
    """A single test case (input + expected output criteria) within a suite."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    suite = models.ForeignKey(
        EvalSuite, on_delete=models.CASCADE, related_name="cases"
    )
    name = models.CharField(max_length=200)
    input_message = models.TextField(help_text="User message to send to the agent.")
    expected_keywords = models.JSONField(
        default=list, blank=True,
        help_text="List of strings that must appear in the response (case-insensitive).",
    )
    must_not_contain = models.JSONField(
        default=list, blank=True,
        help_text="List of strings that must NOT appear in the response.",
    )
    max_latency_ms = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Optional maximum acceptable latency in milliseconds.",
    )
    weight = models.PositiveSmallIntegerField(
        default=1,
        help_text="Relative weight of this case in the overall pass rate.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.suite.name} / {self.name}"


class EvalRun(models.Model):
    """
    A scored execution of an EvalSuite.
    Records pass/fail per case and an overall pass rate.
    """
    class Status(models.TextChoices):
        PENDING  = "pending",  "Pending"
        RUNNING  = "running",  "Running"
        COMPLETE = "complete", "Complete"
        ERROR    = "error",    "Error"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    suite = models.ForeignKey(
        EvalSuite, on_delete=models.CASCADE, related_name="runs"
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.PENDING
    )
    triggered_by = models.CharField(max_length=120, blank=True)
    # Aggregate results
    total_cases = models.PositiveIntegerField(default=0)
    passed_cases = models.PositiveIntegerField(default=0)
    pass_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Pass rate as a percentage (0–100).",
    )
    passed = models.BooleanField(
        default=False,
        help_text="True when pass_rate >= suite.pass_threshold.",
    )
    # Per-case results stored as JSON: [{case_id, name, passed, reason, latency_ms}]
    case_results = models.JSONField(default=list, blank=True)
    error_detail = models.TextField(blank=True)
    executed_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-executed_at"]

    def __str__(self):
        return f"EvalRun {self.id} — {self.suite.name} {'✓' if self.passed else '✗'}"

    def compute_pass_rate(self) -> None:
        """Recalculate pass_rate and passed from case_results; save."""
        if not self.case_results:
            self.pass_rate = 0
            self.passed = False
        else:
            total_weight = sum(r.get("weight", 1) for r in self.case_results)
            passed_weight = sum(
                r.get("weight", 1) for r in self.case_results if r.get("passed")
            )
            self.pass_rate = round(
                (passed_weight / total_weight * 100) if total_weight else 0, 2
            )
            self.passed = self.pass_rate >= float(self.suite.pass_threshold)
        self.save(update_fields=["pass_rate", "passed"])


# ──────────────────────────────────────────────────────────────────────────────
# Phase D — Observability & Cost controls
# ──────────────────────────────────────────────────────────────────────────────

class OtelSpan(models.Model):
    """
    Lightweight OpenTelemetry-compatible span record.

    Each AgentRun produces a root span plus child spans for tool calls,
    guardrail scans, and eval gates.  Spans are written by TelemetryService
    and can be exported to any OTLP collector via the export_spans command.
    """
    class Kind(models.TextChoices):
        SERVER   = "SERVER",   "Server"
        CLIENT   = "CLIENT",   "Client"
        INTERNAL = "INTERNAL", "Internal"
        CONSUMER = "CONSUMER", "Consumer"
        PRODUCER = "PRODUCER", "Producer"

    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    trace_id      = models.CharField(max_length=32, db_index=True)
    span_id       = models.CharField(max_length=16)
    parent_span_id= models.CharField(max_length=16, blank=True, default="")
    name          = models.CharField(max_length=120)
    kind          = models.CharField(max_length=12, choices=Kind.choices, default=Kind.INTERNAL)
    # Wall-clock times stored as ISO strings so they survive SQLite→Postgres migration
    start_time    = models.DateTimeField()
    end_time      = models.DateTimeField(null=True, blank=True)
    duration_ms   = models.PositiveIntegerField(default=0)
    status_code   = models.CharField(max_length=12, default="OK")   # OK | ERROR | UNSET
    status_message= models.TextField(blank=True, default="")
    attributes    = models.JSONField(default=dict, blank=True)
    # Foreign-key shortcuts for querying
    agent         = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="spans")
    run           = models.ForeignKey("AgentRun", on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="spans")
    exported      = models.BooleanField(default=False,
                                        help_text="True once forwarded to an OTLP collector.")
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_time"]
        indexes = [
            models.Index(fields=["trace_id", "start_time"]),
            models.Index(fields=["agent", "start_time"]),
            models.Index(fields=["exported"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.duration_ms} ms)"


class BudgetAlert(models.Model):
    """
    Records a budget breach event for an agent.  Created by compute_budgets
    management command.  One record per calendar month per agent breach.
    """
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent         = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="budget_alerts")
    period_month  = models.CharField(max_length=7,
                                     help_text="YYYY-MM of the breached month.")
    budget_usd    = models.DecimalField(max_digits=10, decimal_places=2)
    actual_usd    = models.DecimalField(max_digits=10, decimal_places=2)
    overage_usd   = models.DecimalField(max_digits=10, decimal_places=2)
    resolved      = models.BooleanField(default=False)
    created_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("agent", "period_month")]

    def __str__(self):
        return f"{self.agent.name} budget breach {self.period_month} (+${self.overage_usd})"


# ──────────────────────────────────────────────────────────────────────────────
# Phase E — Multi-Agent Orchestration
# ──────────────────────────────────────────────────────────────────────────────

class Workflow(models.Model):
    """
    A named multi-agent pipeline definition.

    The DAG is defined by the tasks attached to this workflow via
    WorkflowTask.  Each task declares which upstream step names it depends on
    (depends_on JSON list of step_name strings).
    """
    class Status(models.TextChoices):
        DRAFT     = "draft",     "Draft"
        ACTIVE    = "active",    "Active"
        ARCHIVED  = "archived",  "Archived"

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name        = models.CharField(max_length=160)
    slug        = models.SlugField(unique=True)
    description = models.TextField(blank=True)
    business_unit = models.ForeignKey(
        "BusinessUnit", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="workflows",
    )
    status      = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    owner       = models.CharField(max_length=120, blank=True)
    created_by  = models.CharField(max_length=120, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class WorkflowTask(models.Model):
    """
    A single node in a Workflow DAG.

    step_name must be unique within a workflow.
    depends_on is a list of step_name strings that must complete before
    this task can start (e.g. ["step_a", "step_b"]).
    input_template is a Jinja2-style string where {{outputs.step_name.key}}
    tokens are substituted from upstream task outputs before the agent call.
    """
    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow        = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="tasks")
    step_name       = models.SlugField(max_length=80)
    agent           = models.ForeignKey(Agent, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="workflow_tasks")
    # Inline agent spec (used when agent FK is null — spawns an ephemeral agent call)
    model_override  = models.CharField(max_length=80, blank=True, default="",
                                       help_text="Override model for this step (empty = use agent default or model router).")
    system_prompt   = models.TextField(blank=True, default="",
                                       help_text="System prompt for this step (overrides agent's prompt).")
    input_template  = models.TextField(blank=True, default="",
                                       help_text="Message template. Use {{outputs.STEP.key}} for upstream refs.")
    depends_on      = models.JSONField(default=list, blank=True,
                                       help_text="List of step_name strings this step depends on.")
    timeout_seconds = models.PositiveIntegerField(default=120,
                                                  help_text="Max seconds before this task is marked as timed out.")
    retry_limit     = models.PositiveSmallIntegerField(default=0,
                                                       help_text="Number of automatic retries on failure.")
    order           = models.PositiveSmallIntegerField(default=0,
                                                       help_text="Display order (not execution order — use depends_on).")
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "step_name"]
        unique_together = [("workflow", "step_name")]

    def __str__(self):
        return f"{self.workflow.slug}.{self.step_name}"


class WorkflowRun(models.Model):
    """An execution instance of a Workflow."""
    class Status(models.TextChoices):
        PENDING   = "pending",   "Pending"
        RUNNING   = "running",   "Running"
        COMPLETED = "completed", "Completed"
        FAILED    = "failed",    "Failed"
        CANCELLED = "cancelled", "Cancelled"

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow    = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="runs")
    status      = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    triggered_by = models.CharField(max_length=120, default="system")
    inputs      = models.JSONField(default=dict, blank=True,
                                   help_text="Initial inputs passed to the workflow (available as {{inputs.key}}).")
    outputs     = models.JSONField(default=dict, blank=True,
                                   help_text="Accumulated step outputs: {step_name: {key: value}}.")
    error       = models.TextField(blank=True, default="")
    started_at  = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.workflow.name} run {self.id}"

    @property
    def duration_ms(self) -> int | None:
        if self.completed_at and self.started_at:
            return int((self.completed_at - self.started_at).total_seconds() * 1000)
        return None


class WorkflowTaskRun(models.Model):
    """Execution record for a single WorkflowTask within a WorkflowRun."""
    class Status(models.TextChoices):
        PENDING   = "pending",   "Pending"
        RUNNING   = "running",   "Running"
        COMPLETED = "completed", "Completed"
        FAILED    = "failed",    "Failed"
        SKIPPED   = "skipped",   "Skipped"
        TIMED_OUT = "timed_out", "Timed out"

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow_run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE, related_name="task_runs")
    task         = models.ForeignKey(WorkflowTask, on_delete=models.CASCADE, related_name="runs")
    agent_run    = models.OneToOneField(
        "AgentRun", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="workflow_task_run",
        help_text="The AgentRun created by the orchestrator for this step.",
    )
    status       = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempt      = models.PositiveSmallIntegerField(default=1)
    resolved_input  = models.TextField(blank=True, default="",
                                        help_text="Input message after template substitution.")
    output       = models.JSONField(default=dict, blank=True,
                                    help_text="Structured output extracted from the agent response.")
    raw_output   = models.TextField(blank=True, default="",
                                    help_text="Raw agent output text.")
    error        = models.TextField(blank=True, default="")
    started_at   = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["started_at"]

    def __str__(self):
        return f"{self.task.step_name} [{self.status}]"


class SharedMemory(models.Model):
    """
    Key-value store for cross-agent context sharing within a workflow run.

    Scoped to a WorkflowRun (workflow-local) or to an Agent (agent-global).
    Agents can read/write via the built-in memory_read / memory_write tools.
    """
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workflow_run = models.ForeignKey(WorkflowRun, on_delete=models.CASCADE,
                                     null=True, blank=True, related_name="memory_entries")
    agent        = models.ForeignKey(Agent, on_delete=models.CASCADE,
                                     null=True, blank=True, related_name="memory_entries")
    key          = models.CharField(max_length=200, db_index=True)
    value        = models.JSONField(default=dict)
    written_by   = models.CharField(max_length=120, blank=True,
                                    help_text="Agent slug or user label that last wrote this entry.")
    expires_at   = models.DateTimeField(null=True, blank=True,
                                        help_text="If set, entry is stale after this time.")
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["workflow_run", "key"]),
            models.Index(fields=["agent", "key"]),
        ]

    def __str__(self):
        scope = f"run:{self.workflow_run_id}" if self.workflow_run_id else f"agent:{self.agent_id}"
        return f"[{scope}] {self.key}"

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone
        return bool(self.expires_at and self.expires_at <= timezone.now())


class AuditLog(models.Model):
    """Append-only record of every privileged action on the platform."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.CharField(max_length=120)
    action = models.CharField(max_length=80)
    resource_type = models.CharField(max_length=60, blank=True)
    resource_id = models.CharField(max_length=60, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.actor} · {self.action} · {self.created_at:%Y-%m-%d %H:%M}"
