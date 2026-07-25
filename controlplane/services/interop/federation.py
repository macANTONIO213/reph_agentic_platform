"""
Federation service — Phase 2 federated registry.

Projects the platform's known agents/tools into the unified ``RegistryEntry``
catalog and keeps them current.  Sources projected here:

  - first-party agents (from a published ``AgentCard``);
  - registered MCP servers (from ``RemoteMcpServer`` once active).

Projection copies the source's governance posture verbatim — it never elevates
state.  Deactivation (unpublish / disable) flips ``is_active`` off rather than
deleting, so history survives.  Phase 3 scanners write external agents into the
same table via ``upsert_entry``.
"""
from __future__ import annotations

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def upsert_entry(*, kind, identifier, defaults) -> "object":
    """Create/update a RegistryEntry by (kind, identifier). Stamps last_synced_at."""
    from controlplane.models import RegistryEntry
    defaults = {**defaults, "last_synced_at": timezone.now(), "is_active": True}
    entry, _ = RegistryEntry.objects.update_or_create(
        kind=kind, identifier=identifier, defaults=defaults,
    )
    return entry


def deactivate_entry(*, kind, identifier) -> None:
    from controlplane.models import RegistryEntry
    RegistryEntry.objects.filter(kind=kind, identifier=identifier).update(
        is_active=False, updated_at=timezone.now(),
    )


# ── first-party agents ──────────────────────────────────────────────────────────

def project_agent(agent):
    """
    Project a first-party agent into the catalog from its published A2A card.

    Returns the RegistryEntry, or None if the agent has no published card
    (only discoverable agents belong in the federated catalog).
    """
    from controlplane.models import AgentCard, RegistryEntry

    card = AgentCard.objects.filter(agent=agent, is_published=True).first()
    if card is None:
        deactivate_entry(kind=RegistryEntry.Kind.FIRST_PARTY_AGENT, identifier=agent.slug)
        return None

    doc = card.card_json or {}
    return upsert_entry(
        kind=RegistryEntry.Kind.FIRST_PARTY_AGENT,
        identifier=agent.slug,
        defaults={
            "name": agent.name,
            "description": agent.purpose,
            "protocol": "a2a",
            "endpoint_url": doc.get("url", ""),
            "provider_org": agent.business_unit,
            "capabilities": doc.get("skills", []),
            "card_json": doc,
            "governance": doc.get("x-governance", {}),
            "visibility": RegistryEntry.Visibility.PRIVATE,
            "source": "projection",
            "agent": agent,
            "mcp_server": None,
        },
    )


def unproject_agent(agent) -> None:
    from controlplane.models import RegistryEntry
    deactivate_entry(kind=RegistryEntry.Kind.FIRST_PARTY_AGENT, identifier=agent.slug)


# ── MCP servers ─────────────────────────────────────────────────────────────────

def _mcp_identifier(server) -> str:
    return str(server.id)


def project_mcp_server(server):
    """Project an active MCP server (with a synced catalog) into the catalog."""
    from controlplane.models import RegistryEntry

    if not getattr(server, "is_usable", False):
        deactivate_entry(kind=RegistryEntry.Kind.MCP_SERVER, identifier=_mcp_identifier(server))
        return None

    caps = [
        {"id": t.get("name", ""), "name": t.get("name", ""),
         "description": t.get("description", "")}
        for t in (server.tool_catalog or []) if isinstance(t, dict) and t.get("name")
    ]
    return upsert_entry(
        kind=RegistryEntry.Kind.MCP_SERVER,
        identifier=_mcp_identifier(server),
        defaults={
            "name": server.name,
            "description": f"MCP server exposing {len(caps)} tool(s).",
            "protocol": "mcp",
            "endpoint_url": server.base_url,
            "provider_org": server.business_unit.name if server.business_unit_id else "",
            "capabilities": caps,
            "card_json": {},
            "governance": {},
            "visibility": RegistryEntry.Visibility.PRIVATE,
            "source": "projection",
            "agent": None,
            "mcp_server": server,
        },
    )


def deactivate_mcp_server(server) -> None:
    from controlplane.models import RegistryEntry
    deactivate_entry(kind=RegistryEntry.Kind.MCP_SERVER, identifier=_mcp_identifier(server))


# ── external A2A agents ─────────────────────────────────────────────────────────

def register_external_agent(card_url: str, *, domain: str = "", visibility=None, by: str = "system"):
    """
    Register an external A2A agent into the catalog by fetching its card URL.

    Catalogs an ``external_a2a_agent`` entry keyed on the agent's advertised rpc
    URL.  Raises A2AClientError if the card can't be fetched/validated.
    """
    from controlplane.models import AuditLog, RegistryEntry
    from controlplane.services.interop.a2a_client import fetch_card

    card = fetch_card(card_url, actor=by)
    provider = card.get("provider") if isinstance(card.get("provider"), dict) else {}
    identifier = (card.get("url") or card_url)[:160]

    entry = upsert_entry(
        kind=RegistryEntry.Kind.EXTERNAL_A2A,
        identifier=identifier,
        defaults={
            "name": card.get("name", ""),
            "description": card.get("description", ""),
            "protocol": "a2a",
            "endpoint_url": card.get("url", ""),
            "domain": domain,
            "provider_org": provider.get("organization", ""),
            "capabilities": card.get("skills", []),
            "card_json": card,
            "governance": card.get("x-governance", {}),
            "visibility": visibility or RegistryEntry.Visibility.PRIVATE,
            "source": "manual",
            "agent": None,
            "mcp_server": None,
        },
    )
    AuditLog.objects.create(
        actor=by, action="registry_external_registered",
        resource_type="RegistryEntry", resource_id=str(entry.id),
        payload={"card_url": card_url, "name": entry.name},
    )
    return entry


# ── search / discovery ───────────────────────────────────────────────────────────

def _entry_matches_capability(entry, cap: str) -> bool:
    cap = cap.lower()
    for c in (entry.capabilities or []):
        if not isinstance(c, dict):
            continue
        if any(cap in str(c.get(k, "")).lower() for k in ("id", "name", "description")):
            return True
    return False


def search_entries(*, q="", kind="", domain="", capability="", visibility="",
                   active_only=True, limit=200) -> list:
    """
    Query the federated catalog. Shared by the human API and the agent-facing
    /a2a/registry/ endpoint.  Text (`q`) matches name/description/identifier in the
    DB; `capability` is matched against each entry's skills in Python (JSON field).
    """
    from django.db.models import Q
    from controlplane.models import RegistryEntry

    qs = RegistryEntry.objects.all()
    if active_only:
        qs = qs.filter(is_active=True)
    if kind:
        qs = qs.filter(kind=kind)
    if domain:
        qs = qs.filter(domain__iexact=domain)
    if visibility:
        qs = qs.filter(visibility=visibility)
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q) | Q(identifier__icontains=q))

    # Fetch a wider window when we still have to capability-filter in Python.
    window = limit * 3 if capability else limit
    entries = list(qs.order_by("kind", "name")[:window])
    if capability:
        entries = [e for e in entries if _entry_matches_capability(e, capability)]
    return entries[:limit]


def to_public_dict(e) -> dict:
    """Compact discovery shape for external/agent consumers (no internal fields)."""
    return {
        "id": str(e.id),
        "kind": e.kind,
        "name": e.name,
        "description": e.description,
        "protocol": e.protocol,
        "endpoint_url": e.endpoint_url,
        "domain": e.domain,
        "provider_org": e.provider_org,
        "capabilities": e.capabilities,
        "governance": e.governance,
    }


# ── full backfill ────────────────────────────────────────────────────────────────

def sync_all() -> dict:
    """
    Rebuild the catalog from current sources: every published first-party agent
    and every active MCP server.  Idempotent.  Returns counts.
    """
    from controlplane.models import AgentCard, RemoteMcpServer

    agents = 0
    for card in AgentCard.objects.filter(is_published=True).select_related("agent"):
        if project_agent(card.agent) is not None:
            agents += 1

    servers = 0
    for server in RemoteMcpServer.objects.filter(is_active=True):
        if project_mcp_server(server) is not None:
            servers += 1

    return {"agents": agents, "mcp_servers": servers}
