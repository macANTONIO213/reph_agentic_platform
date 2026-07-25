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

import json
import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

_DOMAIN_MATCH_WEIGHT = 5.0
_DEFAULT_ROUTER_MODEL = "claude-sonnet-4-6"


def _router_mode() -> str:
    """'deterministic' (default) or 'llm'."""
    return getattr(settings, "BROKER_ROUTER_MODE", "deterministic").lower()


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


# ── live LLM ranker (quality-aware) ──────────────────────────────────────────────

_ROUTER_SYSTEM_PROMPT = (
    "You are the routing engine of a governed enterprise agent platform. Given a user "
    "intent and a shortlist of candidate agents (with their capabilities and recent "
    "quality signals), you pick which agents should handle the intent, best first. "
    "Prefer agents whose capabilities semantically match the intent; break ties toward "
    "higher satisfaction and lower cost. Respond with a SINGLE JSON object and nothing "
    'else: {"ranking": [candidate indexes best-first], "reasoning": "one sentence", '
    '"confidence": 0.0-1.0}.'
)


def _quality_map(entries) -> dict:
    """Batch-load recent quality signals for first-party candidate agents."""
    from controlplane.models import Agent
    ids = [e.agent_id for e in entries if getattr(e, "agent_id", None)]
    if not ids:
        return {}
    rows = Agent.objects.filter(id__in=ids).values(
        "id", "satisfaction_score", "monthly_cost_usd", "monthly_runs",
    )
    return {str(r["id"]): r for r in rows}


def _build_ranking_prompt(intent: str, entries, quality: dict) -> str:
    lines = [f"Intent: {intent}", "", "Candidates:"]
    for i, e in enumerate(entries):
        caps = ", ".join(
            c.get("name", "") for c in (e.capabilities or []) if isinstance(c, dict)
        ) or "—"
        q = quality.get(str(getattr(e, "agent_id", "")), {})
        qbits = []
        if q.get("satisfaction_score"):
            qbits.append(f"satisfaction {q['satisfaction_score']}/5")
        if q.get("monthly_cost_usd"):
            qbits.append(f"${q['monthly_cost_usd']}/mo")
        qstr = f" [{'; '.join(qbits)}]" if qbits else ""
        lines.append(
            f"  {i}. {e.name} — {e.description or 'no description'} "
            f"(domain: {e.domain or 'n/a'}; capabilities: {caps}){qstr}"
        )
    return "\n".join(lines)


def _response_text(response) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text" and getattr(block, "text", ""):
            parts.append(block.text)
    return "".join(parts)


def _parse_json(raw: str):
    if not raw:
        return None
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None


def llm_rank(intent: str, entries, *, model_id: str | None = None):
    """
    Ask the LLM to rank the shortlisted candidates. Returns
    ``(order, reasoning, confidence)`` or None if the LLM path is unavailable /
    malformed — in which case the caller keeps the deterministic order.
    """
    if not entries:
        return None
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    model = model_id or getattr(settings, "BROKER_ROUTER_MODEL", _DEFAULT_ROUTER_MODEL)
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_ROUTER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_ranking_prompt(intent, entries, _quality_map(entries))}],
        )
        text = _response_text(response)
    except Exception as exc:  # network/auth/rate-limit/SDK — never fatal
        logger.warning("Broker LLM ranking failed: %s", exc)
        return None

    data = _parse_json(text)
    if not isinstance(data, dict) or not isinstance(data.get("ranking"), list):
        return None
    order = [i for i in data["ranking"] if isinstance(i, int) and 0 <= i < len(entries)]
    if not order:
        return None
    return order, data.get("reasoning", ""), data.get("confidence")


def _reorder(cands: list, order: list) -> list:
    """Reorder (entry, score) pairs by the LLM index order; unranked keep original order after."""
    picked = [cands[i] for i in order]
    seen = set(order)
    rest = [c for i, c in enumerate(cands) if i not in seen]
    return picked + rest


def _ranked_candidates(intent: str, domain: str):
    """Deterministic prefilter, then LLM re-rank when the live router is enabled."""
    cands = select_candidates(intent, domain=domain)
    if _router_mode() == "llm" and cands:
        result = llm_rank(intent, [e for e, _s in cands])
        if result is not None:
            order, reasoning, confidence = result
            return _reorder(cands, order), "llm", reasoning, confidence
    return cands, "deterministic", None, None


# ── routing ──────────────────────────────────────────────────────────────────────

def route(intent: str, *, domain: str = "") -> dict:
    """Return the routing decision + ranked candidates. Does not execute."""
    cands, method, reasoning, confidence = _ranked_candidates(intent, domain)
    best = cands[0][0] if cands else None
    return {
        "method": method,
        "reasoning": reasoning,
        "confidence": confidence,
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

    cands, method, reasoning, confidence = _ranked_candidates(intent, domain)
    chosen_entry = None
    for e, _s in cands:
        if e.kind == RegistryEntry.Kind.FIRST_PARTY_AGENT and e.agent_id:
            chosen_entry = e
            break

    if chosen_entry is None:
        AuditLog.objects.create(
            actor=submitted_by, action="broker_route",
            resource_type="RegistryEntry", resource_id="none",
            payload={"intent": intent[:200], "domain": domain, "routed": False, "method": method},
        )
        return {
            "routed": False,
            "method": method,
            "reason": "No executable first-party agent matched.",
            "candidates": [{"entry": _brief(e), "score": s} for e, s in cands],
        }

    agent = chosen_entry.agent
    task = agent_tasks.submit(agent, intent, submitted_by=submitted_by, channel="broker")
    AuditLog.objects.create(
        actor=submitted_by, action="broker_route",
        resource_type="Agent", resource_id=str(agent.id),
        payload={"intent": intent[:200], "domain": domain, "routed": True,
                 "method": method, "agent_slug": agent.slug, "task_id": str(task.id)},
    )
    return {
        "routed": True,
        "method": method,
        "reasoning": reasoning,
        "confidence": confidence,
        "agent": {"slug": agent.slug, "name": agent.name},
        "task_id": str(task.id),
        "state": task.state,
        "output": task.output_text,
    }
