"""
Shared mixin that provides the three built-in platform tools:
  registry_search, risk_classifier, deployment_gate_builder

These tools query the local agent registry and apply governance rules.
Both the Anthropic and OpenAI adapters use them.
"""
import logging
import re

from django.db.models import Q

logger = logging.getLogger(__name__)

from controlplane.models import Agent


# ------------------------------------------------------------------
# Anthropic tool schemas
# ------------------------------------------------------------------

ANTHROPIC_TOOL_SCHEMAS = [
    {
        "name": "registry_search",
        "description": (
            "Search the internal agent registry for existing agents related to the "
            "deployment request. Use this to check for duplicates or similar agents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords or description to search for in the registry.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "risk_classifier",
        "description": (
            "Classify the risk tier (1–4) of the proposed agent based on its description. "
            "Tier 1 = informational, Tier 2 = internal system access, "
            "Tier 3 = write/workflow actions, Tier 4 = regulated/customer data or production systems."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Full description of what the agent will do.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "deployment_gate_builder",
        "description": (
            "Generate the required governance controls and deployment checklist for a given risk tier. "
            "Call this after risk_classifier to get the specific controls the agent must meet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "risk_tier": {
                    "type": "integer",
                    "description": "Risk tier (1–4) returned by risk_classifier.",
                }
            },
            "required": ["risk_tier"],
        },
    },
    {
        "name": "retrieve_knowledge",
        "description": (
            "Search the enterprise knowledge base for relevant documents and policies. "
            "Use this to retrieve context from uploaded PDFs, policies, guides, and procedures "
            "that are relevant to the user's question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The question or topic to search for in the knowledge base.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of passages to retrieve (default 4, max 8).",
                    "default": 4,
                },
            },
            "required": ["query"],
        },
    },
]

# ------------------------------------------------------------------
# OpenAI / Azure OpenAI tool schemas (same tools, different format)
# ------------------------------------------------------------------

OPENAI_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "registry_search",
            "description": ANTHROPIC_TOOL_SCHEMAS[0]["description"],
            "parameters": ANTHROPIC_TOOL_SCHEMAS[0]["input_schema"],
        },
    },
    {
        "type": "function",
        "function": {
            "name": "risk_classifier",
            "description": ANTHROPIC_TOOL_SCHEMAS[1]["description"],
            "parameters": ANTHROPIC_TOOL_SCHEMAS[1]["input_schema"],
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deployment_gate_builder",
            "description": ANTHROPIC_TOOL_SCHEMAS[2]["description"],
            "parameters": ANTHROPIC_TOOL_SCHEMAS[2]["input_schema"],
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retrieve_knowledge",
            "description": ANTHROPIC_TOOL_SCHEMAS[3]["description"],
            "parameters": ANTHROPIC_TOOL_SCHEMAS[3]["input_schema"],
        },
    },
]


# ------------------------------------------------------------------
# Mixin
# ------------------------------------------------------------------

class RegistryToolsMixin:
    """Provides registry_search, risk_classifier, deployment_gate_builder methods."""

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

    def _registry_search(self, message: str) -> dict:
        # C1: Try semantic search first; fall back to keyword if embeddings unavailable
        try:
            from controlplane.services.embeddings import embedding_service
            bu_id = getattr(self.agent, "org_unit_id", None)
            semantic_results = embedding_service.search_agents(
                message, top_k=5, business_unit_id=bu_id
            )
            # Exclude self
            results = [r for r in semantic_results if r["agent_id"] != str(self.agent.id)]
            if results:
                return {
                    "summary": f"Found {len(results)} semantically related agent(s).",
                    "search_type": "semantic",
                    "matches": results,
                }
        except Exception:
            pass  # fall through to keyword

        # Keyword fallback
        terms = [t for t in re.split(r"[^a-zA-Z0-9]+", message[:500].lower()) if len(t) > 3]
        query = Q()
        for term in terms[:8]:
            query |= Q(name__icontains=term)
            query |= Q(purpose__icontains=term)
            query |= Q(business_unit__icontains=term)
            query |= Q(data_sources__icontains=term)

        matches = (
            Agent.objects.filter(query).exclude(id=self.agent.id)[:5]
            if terms
            else Agent.objects.none()
        )
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

    def _classify_risk(self, message: str) -> dict:
        lowered = message.lower()
        tier = 1
        reasons = []

        for phrase in self._TIER4_PHRASES:
            if phrase in lowered:
                tier = max(tier, 4)
                reasons.append(f"Involves high-impact or regulated context: '{phrase}'")
                break

        for phrase in self._TIER3_PHRASES:
            if phrase in lowered:
                tier = max(tier, 3)
                reasons.append(f"May trigger write, workflow, or automated action: '{phrase}'")
                break

        for phrase in self._TIER2_PHRASES:
            if phrase in lowered:
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

    def _deployment_checklist(self, risk_result: dict) -> dict:
        tier = risk_result.get("risk_tier", 1)
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

    def _retrieve_knowledge(self, query: str, top_k: int = 4) -> dict:
        """C2: Retrieve relevant passages from the knowledge base."""
        try:
            from controlplane.services.rag import rag_service
            top_k = max(1, min(int(top_k), 8))
            passages = rag_service.retrieve(query=query, agent=self.agent, top_k=top_k)
            if not passages:
                return {
                    "summary": "No relevant documents found in the knowledge base.",
                    "passages": [],
                }
            return {
                "summary": f"Retrieved {len(passages)} relevant passage(s).",
                "passages": [
                    {
                        "source": p["title"],
                        "text": p["text"][:800],
                        "relevance_score": p["score"],
                    }
                    for p in passages
                ],
            }
        except Exception as exc:
            logger.error("Knowledge retrieval failed: %s", exc)
            return {"error": f"Knowledge retrieval unavailable: {exc}"}

    def _dispatch_tool(self, name: str, inp: dict, fallback_query: str) -> dict:
        if name == "registry_search":
            return self._registry_search(inp.get("query", fallback_query))
        if name == "risk_classifier":
            return self._classify_risk(inp.get("query", fallback_query))
        if name == "deployment_gate_builder":
            return self._deployment_checklist({"risk_tier": inp.get("risk_tier", 1)})
        if name == "retrieve_knowledge":
            return self._retrieve_knowledge(
                inp.get("query", fallback_query),
                inp.get("top_k", 4),
            )
        return {"error": f"Unknown tool: {name}"}
