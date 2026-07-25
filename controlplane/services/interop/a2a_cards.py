"""
A2A agent-card projection — Phase 1 interop (outbound discoverability).

Projects a governed :class:`Agent` into an A2A agent card (the JSON-RPC 2.0
discovery document other agents / fabrics consume) and manages its publish state.

Governance gates:
  - only pilot/production agents may be published;
  - only a published card is exposed on the ``/a2a/`` surface;
  - the card carries an ``x-governance`` block so our governance depth is visible
    to consumers — the property a bare A2A card does not advertise.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


class CardPublishError(ValueError):
    """Raised when an agent is not eligible for A2A publication."""


def _skills_for(agent) -> list[dict]:
    """Skills = the agent's declared tools plus its executable (non-proposed) bindings."""
    from controlplane.models import AgentToolBinding

    names: list[str] = []
    for n in (agent.tool_names or []):
        if n not in names:
            names.append(n)
    bound = (
        AgentToolBinding.objects
        .filter(agent=agent)
        .exclude(binding_status=AgentToolBinding.Status.PROPOSED)
        .values_list("tool_name", flat=True)
    )
    for n in bound:
        if n not in names:
            names.append(n)
    return [{"id": n, "name": n, "description": f"Tool: {n}"} for n in names]


def build_card(agent, *, base_url: str = "") -> dict:
    """Build the A2A agent-card document for ``agent`` (does not persist)."""
    base = (base_url or "").rstrip("/")
    return {
        "name": agent.name,
        "description": agent.purpose,
        "url": f"{base}/a2a/agents/{agent.slug}/rpc/",
        "version": agent.version,
        "capabilities": {"streaming": True},
        "skills": _skills_for(agent),
        "provider": {"organization": agent.business_unit},
        # Non-standard but deliberate: expose governance posture to consumers.
        "x-governance": {
            "risk_tier": agent.risk_tier,
            "guardrail_level": agent.guardrail_level,
            "governance_level": agent.governance_level,
            "status": agent.status,
        },
    }


def publish_card(agent, *, base_url: str = "", by: str = "system"):
    """Project + publish the agent's card. Raises CardPublishError if ineligible."""
    from controlplane.models import Agent, AgentCard, AuditLog

    if agent.status not in {Agent.Status.PILOT, Agent.Status.PRODUCTION}:
        raise CardPublishError(
            "Only pilot/production agents may be published to A2A "
            f"(agent is '{agent.status}')."
        )
    card, _ = AgentCard.objects.update_or_create(
        agent=agent,
        defaults={
            "card_json": build_card(agent, base_url=base_url),
            "is_published": True,
            "version": agent.version,
            "published_at": timezone.now(),
        },
    )
    AuditLog.objects.create(
        actor=by, action="a2a_card_published",
        resource_type="Agent", resource_id=str(agent.id),
        payload={"slug": agent.slug, "version": agent.version},
    )
    return card


def unpublish_card(agent, *, by: str = "system"):
    """Withdraw an agent's card from A2A discovery (idempotent)."""
    from controlplane.models import AgentCard, AuditLog

    card = AgentCard.objects.filter(agent=agent).first()
    if card is None or not card.is_published:
        return card
    card.is_published = False
    card.save(update_fields=["is_published", "updated_at"])
    AuditLog.objects.create(
        actor=by, action="a2a_card_unpublished",
        resource_type="Agent", resource_id=str(agent.id),
        payload={"slug": agent.slug},
    )
    return card
