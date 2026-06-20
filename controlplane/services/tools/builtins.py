"""
Built-in platform tools, registered into the ToolRegistry.

These are the seven tools that previously lived as hardcoded schemas + methods
on ``adapters/registry_tools.RegistryToolsMixin``.  The implementations now live
here as standalone ``(inp, ctx) -> dict`` handlers (single source of truth); the
mixin remains as a thin compatibility facade that delegates here.

This module is intentionally self-contained — it imports models and platform
services (lazily, where heavy) but never the adapters package, so importing it
during adapter import cannot create a cycle.
"""
from __future__ import annotations

import logging
import re

from django.db.models import Q

from controlplane.models import Agent
from controlplane.services.tools.registry import ToolContext, ToolSpec, tool_registry

logger = logging.getLogger(__name__)


# ── risk-classifier phrase lists ──────────────────────────────────────────────

_TIER4_PHRASES = [
    "customer data", "customer record", "customer account",
    "financial data", "financial record", "financial transaction",
    "personally identifiable", "personal data", "pii", "gdpr",
    "regulated data", "sensitive data", "confidential data",
    "production database", "production system", "production environment",
    "production deployment", "production infrastructure",
    "compliance review", "regulatory review",
    "legal matter", "legal review",
    "incident response", "incident management", "data breach",
    "audit log", "audit trail",
]
_TIER3_PHRASES = [
    "write to", "writes to", "written to",
    "update record", "update the record", "updates records",
    "create record", "create a record", "creates records",
    "delete record", "delete the record",
    "route to", "routes to", "route for approval",
    "approve request", "approves request", "auto-approve", "auto approve",
    "trigger workflow", "triggers workflow", "workflow action",
    "automated action", "automated decision",
    "create ticket", "create a ticket",
    "send notification", "push to", "submit form",
    "modify data", "data modification",
]
_TIER2_PHRASES = [
    "sharepoint", "salesforce", "crm system", "crm data",
    "erp system", "erp data", "general ledger",
    "case management", "case system",
    "internal database", "internal system", "enterprise system",
    "active directory", "ldap", "servicenow", "service now", "jira",
    "internal api", "intranet",
]


# ── handlers ──────────────────────────────────────────────────────────────────

def registry_search(inp: dict, ctx: ToolContext) -> dict:
    """Search the agent registry (semantic first, keyword fallback)."""
    agent = ctx.agent
    message = inp.get("query", ctx.message) or ""

    # C1: semantic search first.
    try:
        from controlplane.services.embeddings import embedding_service
        bu_id = getattr(agent, "org_unit_id", None)
        semantic_results = embedding_service.search_agents(message, top_k=5, business_unit_id=bu_id)
        results = [r for r in semantic_results if r["agent_id"] != str(getattr(agent, "id", ""))]
        if results:
            return {
                "summary": f"Found {len(results)} semantically related agent(s).",
                "search_type": "semantic",
                "matches": results,
            }
    except Exception:
        pass  # fall through to keyword

    terms = [t for t in re.split(r"[^a-zA-Z0-9]+", message[:500].lower()) if len(t) > 3]
    query = Q()
    for term in terms[:8]:
        query |= Q(name__icontains=term)
        query |= Q(purpose__icontains=term)
        query |= Q(business_unit__icontains=term)
        query |= Q(data_sources__icontains=term)

    exclude_id = getattr(agent, "id", None)
    if terms:
        qs = Agent.objects.filter(query)
        if exclude_id:
            qs = qs.exclude(id=exclude_id)
        matches = qs[:5]
    else:
        matches = Agent.objects.none()

    results = [
        {
            "agent_id": str(m.id),
            "name": m.name,
            "status": m.get_status_display(),
            "risk_tier": m.risk_tier,
            "business_unit": m.business_unit,
            "score": 0.5,
        }
        for m in matches
    ]
    return {
        "summary": f"Found {len(results)} related registered agent(s).",
        "search_type": "keyword",
        "matches": results,
    }


def risk_classifier(inp: dict, ctx: ToolContext) -> dict:
    """Keyword-based risk tier (1–4) classification."""
    message = (inp.get("query", ctx.message) or "").lower()
    tier = 1
    reasons: list[str] = []

    for phrase in _TIER4_PHRASES:
        if phrase in message:
            tier = max(tier, 4)
            reasons.append(f"Involves high-impact or regulated context: '{phrase}'")
            break
    for phrase in _TIER3_PHRASES:
        if phrase in message:
            tier = max(tier, 3)
            reasons.append(f"May trigger write, workflow, or automated action: '{phrase}'")
            break
    for phrase in _TIER2_PHRASES:
        if phrase in message:
            tier = max(tier, 2)
            reasons.append(f"Accesses internal enterprise system: '{phrase}'")
            break

    if not reasons:
        reasons.append("No high-risk phrases detected — informational use case assumed.")
    reasons.append("Keyword classification only — human review required before finalising.")

    return {
        "summary": f"Recommended risk tier {tier}. Human review required.",
        "risk_tier": tier,
        "reasons": reasons,
    }


def deployment_gate_builder(inp: dict, ctx: ToolContext) -> dict:
    """Generate the governance control checklist for a risk tier."""
    tier = inp.get("risk_tier", 1)
    controls = [
        "Business owner and technical owner assigned",
        "Agent manifest registered in the platform",
        "Telemetry enabled for runs, tool calls, feedback, and failures",
        "Approved data sources listed and reviewed",
    ]
    if tier >= 2:
        controls.append("Access control mapped to approved user groups")
    if tier >= 3:
        controls.append("Human escalation path and rollback procedure documented")
        controls.append("Regression test set completed before production")
    if tier >= 4:
        controls.append("Compliance review and production change approval required")
        controls.append("Human approval required before customer-impacting actions")
    return {"summary": f"Generated {len(controls)} required control(s).", "controls": controls}


def retrieve_knowledge(inp: dict, ctx: ToolContext) -> dict:
    """C2: retrieve relevant passages from the knowledge base."""
    query = inp.get("query", ctx.message) or ""
    try:
        from controlplane.services.rag import rag_service
        top_k = max(1, min(int(inp.get("top_k", 4)), 8))
        passages = rag_service.retrieve(query=query, agent=ctx.agent, top_k=top_k)
        if not passages:
            return {"summary": "No relevant documents found in the knowledge base.", "passages": []}
        return {
            "summary": f"Retrieved {len(passages)} relevant passage(s).",
            "passages": [
                {"source": p["title"], "text": p["text"][:800], "relevance_score": p["score"]}
                for p in passages
            ],
        }
    except Exception as exc:
        logger.error("Knowledge retrieval failed: %s", exc)
        return {"error": f"Knowledge retrieval unavailable: {exc}"}


def memory_read(inp: dict, ctx: ToolContext) -> dict:
    """E: read from cross-agent shared memory."""
    key = inp.get("key", "")
    try:
        from controlplane.services.memory import memory_service
        value = memory_service.read(key=key, workflow_run=ctx.workflow_run, agent=ctx.agent)
        if value is None:
            return {"found": False, "key": key, "value": None}
        return {"found": True, "key": key, "value": value}
    except Exception as exc:
        logger.error("Memory read failed: %s", exc)
        return {"error": f"Memory read unavailable: {exc}"}


def memory_write(inp: dict, ctx: ToolContext) -> dict:
    """E: write to cross-agent shared memory."""
    key = inp.get("key", "")
    value = inp.get("value", "")
    ttl_seconds = inp.get("ttl_seconds", 0)
    try:
        import json as _json
        from controlplane.services.memory import memory_service
        try:
            parsed = _json.loads(value)
        except Exception:
            parsed = value
        ttl = int(ttl_seconds) if ttl_seconds else None
        memory_service.write(
            key=key,
            value=parsed,
            workflow_run=ctx.workflow_run,
            agent=ctx.agent if ctx.workflow_run is None else None,
            written_by=ctx.actor,
            ttl_seconds=ttl,
        )
        return {"written": True, "key": key}
    except Exception as exc:
        logger.error("Memory write failed: %s", exc)
        return {"error": f"Memory write unavailable: {exc}"}


def delegate_to_agent(inp: dict, ctx: ToolContext) -> dict:
    """E: delegate a sub-task to another registered agent."""
    agent_slug = inp.get("agent_slug", "")
    message = inp.get("message", ctx.message)
    try:
        from controlplane.services.orchestrator import delegate_to_agent as _delegate
        caller = f"agent:{getattr(ctx.agent, 'slug', 'delegator')}"
        return _delegate(
            agent_slug=agent_slug,
            message=message,
            workflow_run=ctx.workflow_run,
            caller_label=caller,
        )
    except Exception as exc:
        logger.error("Delegation failed: %s", exc)
        return {"error": f"Delegation unavailable: {exc}"}


# ── registration ────────────────────────────────────────────────────────────

# NOTE: all builtins are risk_tier=1 in M0 to preserve current behaviour for
# every agent.  Connector-backed tools (Layer 1) will carry real tiers and set
# requires_binding=True.
_BUILTINS = [
    ToolSpec(
        name="registry_search",
        description=(
            "Search the internal agent registry for existing agents related to the "
            "deployment request. Use this to check for duplicates or similar agents."
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords or description to search for."}},
            "required": ["query"],
        },
        handler=registry_search,
    ),
    ToolSpec(
        name="risk_classifier",
        description=(
            "Classify the risk tier (1–4) of the proposed agent based on its description. "
            "Tier 1 = informational, Tier 2 = internal system access, "
            "Tier 3 = write/workflow actions, Tier 4 = regulated/customer data or production systems."
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Full description of what the agent will do."}},
            "required": ["query"],
        },
        handler=risk_classifier,
    ),
    ToolSpec(
        name="deployment_gate_builder",
        description=(
            "Generate the required governance controls and deployment checklist for a given risk tier. "
            "Call this after risk_classifier to get the specific controls the agent must meet."
        ),
        input_schema={
            "type": "object",
            "properties": {"risk_tier": {"type": "integer", "description": "Risk tier (1–4) returned by risk_classifier."}},
            "required": ["risk_tier"],
        },
        handler=deployment_gate_builder,
    ),
    ToolSpec(
        name="retrieve_knowledge",
        description=(
            "Search the enterprise knowledge base for relevant documents and policies. "
            "Use this to retrieve context from uploaded PDFs, policies, guides, and procedures "
            "that are relevant to the user's question."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The question or topic to search for."},
                "top_k": {"type": "integer", "description": "Number of passages (default 4, max 8).", "default": 4},
            },
            "required": ["query"],
        },
        handler=retrieve_knowledge,
    ),
    ToolSpec(
        name="memory_read",
        description=(
            "Read a value from shared cross-agent memory. "
            "Use this to retrieve context written by a previous agent in the same workflow."
        ),
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string", "description": "The memory key to read."}},
            "required": ["key"],
        },
        handler=memory_read,
    ),
    ToolSpec(
        name="memory_write",
        description=(
            "Write a value to shared cross-agent memory so downstream agents can access it. "
            "Use this to persist important findings or summaries for later steps."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "The memory key to write."},
                "value": {"type": "string", "description": "The value to store (string or JSON string)."},
                "ttl_seconds": {"type": "integer", "description": "Optional expiry in seconds. 0 = no expiry.", "default": 0},
            },
            "required": ["key", "value"],
        },
        handler=memory_write,
    ),
    ToolSpec(
        name="delegate_to_agent",
        description=(
            "Delegate a sub-task to another registered agent by slug. "
            "Use this when a specialised agent can better handle part of the request. "
            "Returns the delegated agent's output."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_slug": {"type": "string", "description": "The slug of the target agent."},
                "message": {"type": "string", "description": "The full message / task to send to the target agent."},
            },
            "required": ["agent_slug", "message"],
        },
        handler=delegate_to_agent,
    ),
]


def register_builtins() -> None:
    for spec in _BUILTINS:
        tool_registry.register(spec)
