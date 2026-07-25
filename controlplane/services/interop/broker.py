"""
Agent Broker — Phase 4.

Routes an intent to the best agent in the federated registry and (optionally) runs
it through the governed runtime.  MuleSoft Agent Fabric's Broker pillar — with the
differentiator that every hop is governed: selection only considers approved
registry entries, and execution goes through ``PlatformAgentRuntime`` (guardrails,
telemetry, audit), never a direct adapter call.

Selection is deterministic and explainable (keyword/domain/capability scoring) so
a routing decision can always be justified; an LLM ranker can layer on later.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_DOMAIN_MATCH_WEIGHT = 5.0


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _score(entry, intent_tokens: set, domain: str) -> float:
    score = 0.0
    if domain and entry.domain and entry.domain.lower() == domain.lower():
        score += _DOMAIN_MATCH_WEIGHT
    hay = _tokens(entry.name) | _tokens(entry.description)
    for c in (entry.capabilities or []):
        if isinstance(c, dict):
            hay |= _tokens(c.get("name", "")) | _tokens(c.get("description", "")) | _tokens(c.get("id", ""))
    score += len(intent_tokens & hay)
    return score


def _brief(entry) -> dict:
    return {
        "id": str(entry.id),
        "kind": entry.kind,
        "name": entry.name,
        "domain": entry.domain,
        "endpoint_url": entry.endpoint_url,
        "identifier": entry.identifier,
        "executable": bool(entry.agent_id),  # first-party agents can run via the runtime
    }


def select_candidates(intent: str, *, domain: str = "", limit: int = 5) -> list:
    """Rank approved agent-kind registry entries against the intent. Returns [(entry, score)]."""
    from controlplane.models import RegistryEntry
    from controlplane.services.interop import federation

    entries = federation.search_entries(domain=domain, review_status="approved", limit=200)
    entries = [
        e for e in entries
        if e.kind in (RegistryEntry.Kind.FIRST_PARTY_AGENT, RegistryEntry.Kind.EXTERNAL_A2A)
    ]
    toks = _tokens(intent)
    scored = [(e, _score(e, toks, domain)) for e in entries]
    positive = [t for t in scored if t[1] > 0]
    ranked = positive if positive else scored  # fall back to all when nothing matches
    ranked.sort(key=lambda t: -t[1])
    return ranked[:limit]


def route(intent: str, *, domain: str = "") -> dict:
    """Return the routing decision + ranked candidates. Does not execute."""
    cands = select_candidates(intent, domain=domain)
    best = cands[0][0] if cands else None
    return {
        "decision": _brief(best) if best is not None else None,
        "candidates": [{"entry": _brief(e), "score": s} for e, s in cands],
    }


def route_and_execute(intent: str, *, domain: str = "", submitted_by: str = "broker") -> dict:
    """
    Route to the best executable first-party agent and run it through the governed
    runtime.  External candidates are surfaced but not executed in this hop.
    """
    from controlplane.models import AuditLog, RegistryEntry
    from controlplane.services.agent_tasks import agent_tasks

    cands = select_candidates(intent, domain=domain)
    chosen_entry = None
    for e, _s in cands:
        if e.kind == RegistryEntry.Kind.FIRST_PARTY_AGENT and e.agent_id:
            chosen_entry = e
            break

    if chosen_entry is None:
        AuditLog.objects.create(
            actor=submitted_by, action="broker_route",
            resource_type="RegistryEntry", resource_id="none",
            payload={"intent": intent[:200], "domain": domain, "routed": False},
        )
        return {
            "routed": False,
            "reason": "No executable first-party agent matched.",
            "candidates": [{"entry": _brief(e), "score": s} for e, s in cands],
        }

    agent = chosen_entry.agent
    task = agent_tasks.submit(agent, intent, submitted_by=submitted_by, channel="broker")
    AuditLog.objects.create(
        actor=submitted_by, action="broker_route",
        resource_type="Agent", resource_id=str(agent.id),
        payload={"intent": intent[:200], "domain": domain, "routed": True,
                 "agent_slug": agent.slug, "task_id": str(task.id)},
    )
    return {
        "routed": True,
        "agent": {"slug": agent.slug, "name": agent.name},
        "task_id": str(task.id),
        "state": task.state,
        "output": task.output_text,
    }
