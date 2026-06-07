"""
Shared mixin that provides the three built-in platform tools:
  registry_search, risk_classifier, deployment_gate_builder

These tools query the local agent registry and apply governance rules.
Both the Anthropic and OpenAI adapters use them.
"""
import re

from django.db.models import Q

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
                "name": m.name,
                "status": m.get_status_display(),
                "risk_tier": m.risk_tier,
                "business_unit": m.business_unit,
            }
            for m in matches
        ]
        return {"summary": f"Found {len(results)} related registered agent(s).", "matches": results}

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

    def _dispatch_tool(self, name: str, inp: dict, fallback_query: str) -> dict:
        if name == "registry_search":
            return self._registry_search(inp.get("query", fallback_query))
        if name == "risk_classifier":
            return self._classify_risk(inp.get("query", fallback_query))
        if name == "deployment_gate_builder":
            return self._deployment_checklist({"risk_tier": inp.get("risk_tier", 1)})
        return {"error": f"Unknown tool: {name}"}
