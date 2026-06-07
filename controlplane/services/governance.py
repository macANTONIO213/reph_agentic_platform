"""
GovernanceService — single choke point for all lifecycle / governance writes.

No code path may write Agent.status, risk_tier, tool_names, model_id, or
deployed_at by calling Agent.save() directly.  All callers (UI, API, admin,
management commands) must go through this service so that gate logic,
state-machine enforcement, and audit logging happen exactly once.

EvalRun gate: not yet implemented (EvalRun model not yet migrated).
Tracked as deviation in DECISIONS.md.
"""
import re
import uuid
from datetime import timedelta

from django.utils import timezone

from controlplane.models import Agent, AgentVersion, Approval, AuditLog, GovernanceReview


class RegistrationError(ValueError):
    """Raised when register_agent() validation fails."""


class TransitionError(ValueError):
    """Raised when a requested status transition is not permitted."""


# Models the platform accepts.  Empty string ⇒ adapter's default.
ALLOWED_MODEL_IDS: frozenset[str] = frozenset({
    "",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "gpt-4o",
    "gpt-4o-mini",
    "azure/gpt-4o",
    "azure/gpt-4o-mini",
    "bedrock/claude-3-5-sonnet",
    "bedrock/claude-3-haiku",
})

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _audit(actor, action, resource_type, resource_id, payload, source, ip):
    AuditLog.objects.create(
        actor=actor,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id),
        payload={"source": source, **payload},
        ip_address=ip,
    )


class GovernanceService:
    """Stateless.  Instantiate per-request or as a singleton."""

    # ── Registration ────────────────────────────────────────────────────────

    def register_agent(
        self,
        *,
        actor,
        data: dict,
        source: str = "ui",
        ip: str | None = None,
    ) -> Agent:
        """
        Create a new Agent in status=draft.

        ``actor`` — Django User instance (not username string).
        ``data``  — validated dict; must include at minimum: name, platform,
                    owner, technical_owner, purpose, system_prompt.
        Raises RegistrationError on validation failure.
        """
        self._validate_registration(data)

        slug = data.get("slug") or self._make_slug(data["name"])
        if Agent.objects.filter(slug=slug).exists():
            slug = f"{slug}-{uuid.uuid4().hex[:6]}"

        model_id = data.get("model_id", "")
        if model_id and model_id not in ALLOWED_MODEL_IDS:
            raise RegistrationError(
                f"Model '{model_id}' is not on the platform allowlist. "
                f"Allowed: {sorted(m for m in ALLOWED_MODEL_IDS if m)}"
            )

        agent = Agent.objects.create(
            slug=slug,
            name=data["name"].strip(),
            kind=data.get("kind", Agent.Kind.EXTERNAL),
            integration_mode=data.get("integration_mode", Agent.IntegrationMode.PROXY),
            platform=data["platform"],
            business_unit=data.get("business_unit", ""),
            owner=data["owner"].strip(),
            technical_owner=data.get("technical_owner", data["owner"]).strip(),
            purpose=data["purpose"].strip(),
            system_prompt=data.get("system_prompt", ""),
            status=Agent.Status.DRAFT,
            risk_tier=int(data.get("risk_tier", 1)),
            version=data.get("version", "1.0"),
            endpoint_url=data.get("endpoint_url", ""),
            model_id=model_id,
            org_unit_id=data.get("org_unit_id") or None,
            org_division_id=data.get("org_division_id") or None,
            org_work_stream_id=data.get("org_work_stream_id") or None,
            org_process_id=data.get("org_process_id") or None,
            data_sources=data.get("data_sources", []),
            tool_names=data.get("tool_names", []),
        )
        _audit(
            actor=actor.username,
            action="agent.registered",
            resource_type="Agent",
            resource_id=agent.id,
            payload={"name": agent.name, "platform": agent.platform, "risk_tier": agent.risk_tier},
            source=source,
            ip=ip,
        )
        return agent

    # ── Version snapshot ────────────────────────────────────────────────────

    def create_version(
        self,
        *,
        actor,
        agent: Agent,
        manifest: dict,
        source: str = "ui",
        ip: str | None = None,
    ) -> AgentVersion:
        version = AgentVersion.objects.create(
            agent=agent,
            version=manifest.get("version", agent.version),
            system_prompt=manifest.get("system_prompt", agent.system_prompt),
            tool_names=manifest.get("tool_names", agent.tool_names),
            model_id=manifest.get("model_id", agent.model_id),
        )
        _audit(
            actor=actor.username,
            action="agent.version_created",
            resource_type="AgentVersion",
            resource_id=version.id,
            payload={"agent": agent.name, "version": version.version},
            source=source,
            ip=ip,
        )
        return version

    # ── Approval ────────────────────────────────────────────────────────────

    def record_approval(
        self,
        *,
        actor,
        agent: Agent,
        scope: str = "tier4_execution",
        expires_at=None,
        ttl_hours: int = 8,
        notes: str = "",
        source: str = "ui",
        ip: str | None = None,
    ) -> Approval:
        self._require_role(actor, {"agent_approver", "platform_admin"})
        if expires_at is None:
            expires_at = timezone.now() + timedelta(hours=max(1, min(ttl_hours, 72)))

        approval = Approval.objects.create(
            agent=agent,
            approved_by=actor,
            approved_by_username=actor.username,
            scope=scope,
            notes=notes,
            expires_at=expires_at,
        )
        _audit(
            actor=actor.username,
            action="agent.approved",
            resource_type="Approval",
            resource_id=approval.id,
            payload={"agent": agent.name, "scope": scope, "expires_at": expires_at.isoformat()},
            source=source,
            ip=ip,
        )
        return approval

    # ── Transition ──────────────────────────────────────────────────────────

    def transition(
        self,
        *,
        actor,
        agent: Agent,
        to_status: str,
        reason: str = "",
        source: str = "ui",
        ip: str | None = None,
        bypass: bool = False,
    ) -> None:
        """
        Transition agent to ``to_status``.  Enforces:
        - ALLOWED_TRANSITIONS state machine.
        - Promotion to production requires an approved GovernanceReview AND
          a valid (unexpired, unconsumed) Approval by an authorised approver.
        ``bypass=True`` is reserved for platform_admin / staff break-glass;
        always audited as agent.transition.forced.
        """
        from_status = agent.status

        if not agent.can_transition_to(to_status):
            allowed = agent.ALLOWED_TRANSITIONS.get(agent.status, set())
            raise TransitionError(
                f"Cannot transition '{agent.name}' from '{from_status}' to '{to_status}'. "
                f"Allowed: {sorted(allowed) or 'none'}."
            )

        if to_status == Agent.Status.PRODUCTION and not bypass:
            self._check_production_gates(agent)

        is_forced = bypass and to_status == Agent.Status.PRODUCTION
        action = "agent.transition.forced" if is_forced else "agent.transition"

        # Perform the write via transition_to (handles deployed_at + snapshot).
        agent.transition_to(to_status, bypass_governance=bypass)

        _audit(
            actor=actor.username,
            action=action,
            resource_type="Agent",
            resource_id=agent.id,
            payload={
                "from_status": from_status,
                "to_status": to_status,
                "reason": reason,
                **({"FORCED": True} if is_forced else {}),
            },
            source=source,
            ip=ip,
        )

    # ── Retire ──────────────────────────────────────────────────────────────

    def retire(
        self,
        *,
        actor,
        agent: Agent,
        reason: str = "",
        source: str = "ui",
        ip: str | None = None,
    ) -> None:
        self.transition(actor=actor, agent=agent, to_status=Agent.Status.RETIRED,
                        reason=reason, source=source, ip=ip)

    # ── Internal helpers ────────────────────────────────────────────────────

    @staticmethod
    def _validate_registration(data: dict) -> None:
        required = ("name", "platform", "owner", "purpose")
        missing = [f for f in required if not data.get(f, "").strip()]
        if missing:
            raise RegistrationError(f"Missing required fields: {', '.join(missing)}")

        risk_tier = data.get("risk_tier", 1)
        try:
            if not (1 <= int(risk_tier) <= 4):
                raise RegistrationError("risk_tier must be 1–4.")
        except (TypeError, ValueError):
            raise RegistrationError("risk_tier must be an integer 1–4.")

        platform = data.get("platform", "")
        valid_platforms = {c[0] for c in Agent.Platform.choices}
        if platform not in valid_platforms:
            raise RegistrationError(f"Invalid platform '{platform}'. Choose from: {sorted(valid_platforms)}")

    @staticmethod
    def _check_production_gates(agent: Agent) -> None:
        """Raise TransitionError if any production gate is unmet."""
        has_review = agent.governance_reviews.filter(
            status=GovernanceReview.Status.APPROVED
        ).exists()
        if not has_review:
            raise TransitionError(
                f"Cannot promote '{agent.name}' to production: "
                "an approved GovernanceReview is required."
            )

        has_approval = agent.approvals.filter(
            is_consumed=False,
            expires_at__gt=timezone.now(),
        ).exists()
        if not has_approval:
            raise TransitionError(
                f"Cannot promote '{agent.name}' to production: "
                "a valid (unexpired, unconsumed) Approval by an authorised approver is required."
            )

    @staticmethod
    def _require_role(actor, roles: set) -> None:
        if actor.is_staff or actor.is_superuser:
            return
        if actor.groups.filter(name__in=roles).exists():
            return
        raise PermissionError(
            f"Action requires one of these roles: {sorted(roles)}. "
            f"User '{actor.username}' does not have them."
        )

    @staticmethod
    def _make_slug(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return slug[:50] if slug else f"agent-{uuid.uuid4().hex[:8]}"


# Module-level singleton — import and call directly.
governance = GovernanceService()
