"""
Agent Factory Service — Phase F Blueprint Lifecycle

Provides:
  OpportunityScorer      — scores a ProcessInsight across four dimensions
  BlueprintGenerator     — derives an AgentBlueprint from a ProcessInsight (heuristics)
  LLMBlueprintGenerator  — proposes the design with an LLM; deterministic fallback
  BuildCompiler          — converts an approved AgentBlueprint into an Agent

Usage::
    from controlplane.services.factory import blueprint_generator, build_compiler

    blueprint = blueprint_generator.generate(insight)
    agent = build_compiler.build(blueprint, built_by="user:alice")

    # LLM-proposed design (falls back to heuristics when no API key / bad response):
    from controlplane.services.factory import llm_blueprint_generator
    blueprint = llm_blueprint_generator.generate(insight)
"""
from __future__ import annotations

import json
import logging
import re
import uuid

from django.conf import settings

logger = logging.getLogger(__name__)

# ── Scoring constants ──────────────────────────────────────────────────────────

_FIT_BY_FINDING_TYPE = {
    "automation_opportunity": 9,
    "rework_pattern":         7,
    "bottleneck":             7,
    "exception":              6,
    "control_gap":            5,
    "other":                  4,
}

_HIGH_IMPACT_KEYWORDS = {
    "revenue", "cost", "compliance", "sla", "customer", "critical",
    "million", "significant", "major", "high", "strategic",
}

_HIGH_RISK_KEYWORDS = {
    "gdpr", "hipaa", "pci", "regulated", "personal data", "sensitive",
    "financial", "legal", "compliance", "audit", "restricted",
}


class OpportunityScorer:
    """
    Scores a ProcessInsight across four dimensions (0–10 each) and derives
    a composite opportunity_score.

    Dimensions:
      business_value  — estimated business impact of automation
      automation_fit  — how well the finding type suits automation
      complexity      — implementation complexity (higher = harder)
      risk            — regulatory / operational risk (higher = riskier)

    Composite formula:
      opportunity_score = (business_value * 0.35
                         + automation_fit  * 0.35
                         + (10 - complexity) * 0.20
                         + (10 - risk)       * 0.10)
    """

    def score(self, insight) -> dict:
        business_value = self._score_business_value(insight)
        automation_fit = self._score_automation_fit(insight)
        complexity     = self._score_complexity(insight)
        risk           = self._score_risk(insight)

        opportunity = round(
            business_value * 0.35
            + automation_fit * 0.35
            + (10 - complexity) * 0.20
            + (10 - risk) * 0.10,
            2,
        )

        return {
            "business_value_score": business_value,
            "automation_fit_score": automation_fit,
            "complexity_score":     complexity,
            "risk_score":           risk,
            "opportunity_score":    opportunity,
        }

    # ── dimension helpers ──────────────────────────────────────────────────────

    def _score_business_value(self, insight) -> int:
        text = f"{insight.impact} {insight.frequency} {insight.summary}".lower()
        hits = sum(1 for kw in _HIGH_IMPACT_KEYWORDS if kw in text)
        base = 5
        return min(10, base + hits)

    def _score_automation_fit(self, insight) -> int:
        return _FIT_BY_FINDING_TYPE.get(insight.finding_type, 4)

    def _score_complexity(self, insight) -> int:
        systems = insight.systems_involved if isinstance(insight.systems_involved, list) else []
        raw = len(systems) * 2
        return min(9, max(1, raw))

    def _score_risk(self, insight) -> int:
        text = f"{insight.risk_notes} {insight.summary}".lower()
        hits = sum(1 for kw in _HIGH_RISK_KEYWORDS if kw in text)
        return min(10, hits * 2 + (2 if insight.risk_notes.strip() else 1))


class BlueprintGenerator:
    """
    Derives an AgentBlueprint from a ProcessInsight.

    No LLM call is made — the generator uses deterministic heuristics so that
    blueprints are produced instantly and are fully auditable.  The approver
    can then edit the blueprint before granting approval.
    """

    def __init__(self):
        self._scorer = OpportunityScorer()

    def generate(self, insight) -> "AgentBlueprint":
        """
        Create and persist an AgentBlueprint from an insight.
        Returns the new AgentBlueprint instance.
        """
        scores = self._scorer.score(insight)
        risk_level = self._derive_risk_level(scores["risk_score"])
        design = self.derive_design(insight, risk_level)
        return self._assemble(insight, design, scores, risk_level)

    def derive_design(self, insight, risk_level: str) -> dict:
        """
        Produce the agent-design fields for a blueprint.

        Returns a dict keyed exactly by the AgentBlueprint design fields so it can
        be splatted straight into ``AgentBlueprint.objects.create``.  Subclasses
        (e.g. :class:`LLMBlueprintGenerator`) override this to propose the design a
        different way while reusing scoring, status, and persistence in
        :meth:`_assemble`.
        """
        return {
            "agent_name":            self._derive_name(insight),
            "mission":               self._derive_mission(insight),
            "trigger":               self._derive_trigger(insight),
            "inputs":                self._derive_inputs(insight),
            "outputs":               self._derive_outputs(insight),
            "tools":                 self._derive_tools(insight),
            "workflow_steps":        self._derive_workflow_steps(insight),
            "guardrails":            self._derive_guardrails(insight, risk_level),
            "human_approval_points": self._derive_approval_points(risk_level),
            "success_metrics":       self._derive_success_metrics(insight),
        }

    def _assemble(self, insight, design: dict, scores: dict, risk_level: str) -> "AgentBlueprint":
        """Persist a design dict as an AgentBlueprint, deriving status and version."""
        from controlplane.models import AgentBlueprint

        missing_tools, missing_data = self._detect_missing(insight)

        # Determine initial status
        if missing_data:
            status = AgentBlueprint.Status.NEEDS_DATA
        elif missing_tools:
            status = AgentBlueprint.Status.NEEDS_TOOL
        else:
            status = AgentBlueprint.Status.DRAFT

        # Determine next version for this insight
        version = 1
        if insight.pk:
            existing = AgentBlueprint.objects.filter(insight=insight).count()
            version = existing + 1

        blueprint = AgentBlueprint.objects.create(
            insight               = insight,
            version               = version,
            missing_tools         = missing_tools,
            missing_data          = missing_data,
            status                = status,
            risk_level            = risk_level,
            **design,
            **scores,
        )

        logger.info(
            "Blueprint generated: '%s' (score=%.1f, status=%s)",
            design["agent_name"], scores["opportunity_score"], status,
        )
        return blueprint

    # ── derivation helpers ────────────────────────────────────────────────────

    def _derive_name(self, insight) -> str:
        label = insight.process_name.title()
        suffix = {
            "bottleneck":             "Throughput Agent",
            "exception":              "Exception Handler",
            "control_gap":            "Compliance Monitor",
            "automation_opportunity": "Automation Agent",
            "rework_pattern":         "Quality Improvement Agent",
            "other":                  "Process Agent",
        }.get(insight.finding_type, "Process Agent")
        return f"{label} — {suffix}"

    def _derive_mission(self, insight) -> str:
        base = f"Automate handling of '{insight.process_name}' {insight.finding_type.replace('_', ' ')}."
        if insight.recommended_action:
            return f"{base} Recommended action: {insight.recommended_action}"
        return base

    def _derive_trigger(self, insight) -> str:
        return {
            "bottleneck":             "Queue depth threshold exceeded or scheduled batch",
            "exception":              "Exception event raised in source system",
            "control_gap":            "Scheduled compliance check (daily/weekly)",
            "automation_opportunity": "Inbound case, document, or API event",
            "rework_pattern":         "Quality review trigger or scheduled scan",
            "other":                  "Manual invocation or API call",
        }.get(insight.finding_type, "Manual invocation or API call")

    def _derive_inputs(self, insight) -> list:
        inputs = []
        systems = insight.systems_involved if isinstance(insight.systems_involved, list) else []
        for sys in systems:
            inputs.append({"source": sys, "description": f"Data from {sys}"})
        if not inputs:
            inputs.append({"source": "process_data", "description": "Process event or case record"})
        return inputs

    def _derive_outputs(self, insight) -> list:
        return {
            "bottleneck":             [{"artifact": "throughput_report"}, {"action": "escalation_notification"}],
            "exception":              [{"artifact": "exception_report"}, {"action": "resolution_action"}],
            "control_gap":            [{"artifact": "compliance_report"}, {"action": "gap_alert"}],
            "automation_opportunity": [{"artifact": "processed_output"}, {"action": "downstream_trigger"}],
            "rework_pattern":         [{"artifact": "quality_report"}, {"action": "correction_request"}],
            "other":                  [{"artifact": "process_output"}],
        }.get(insight.finding_type, [{"artifact": "process_output"}])

    def _derive_tools(self, insight) -> list:
        tools = []
        systems = insight.systems_involved if isinstance(insight.systems_involved, list) else []
        for sys in systems:
            s = sys.lower()
            if any(kw in s for kw in ("db", "sql", "database", "postgres", "oracle")):
                tools.append({"name": "sql_connector", "target": sys})
            elif any(kw in s for kw in ("api", "rest", "service", "http")):
                tools.append({"name": "rest_connector", "target": sys})
            else:
                tools.append({"name": "rest_connector", "target": sys})
        if not tools:
            tools.append({"name": "rest_connector", "target": "source_system"})
        return tools

    def _derive_workflow_steps(self, insight) -> list:
        common = [
            {"step": "ingest",    "description": "Receive and validate input data"},
            {"step": "analyse",   "description": f"Analyse {insight.process_name} data for {insight.finding_type.replace('_', ' ')}"},
            {"step": "act",       "description": insight.recommended_action or "Apply automated resolution"},
            {"step": "report",    "description": "Generate output artefact and audit log entry"},
        ]
        if insight.risk_notes:
            common.insert(3, {"step": "review_gate", "description": "Pause for human review if risk threshold exceeded"})
        return common

    def _derive_guardrails(self, insight, risk_level: str) -> list:
        rails = [
            {"rule": "rate_limit", "description": "Maximum 100 actions per hour"},
            {"rule": "audit_log",  "description": "Every action must be audit-logged"},
        ]
        if risk_level in ("medium", "high"):
            rails.append({"rule": "human_approval", "description": "Destructive actions require human approval"})
        if risk_level == "high":
            rails.append({"rule": "dual_control", "description": "High-risk changes require a second approver"})
        if insight.risk_notes:
            rails.append({"rule": "risk_flag", "description": insight.risk_notes[:200]})
        return rails

    def _derive_approval_points(self, risk_level: str) -> list:
        if risk_level == "low":
            return []
        if risk_level == "medium":
            return [{"step": "act", "description": "Review before committing changes"}]
        return [
            {"step": "act",    "description": "Review before committing changes"},
            {"step": "report", "description": "Final sign-off before external notification"},
        ]

    def _derive_success_metrics(self, insight) -> list:
        metrics = [{"metric": "cycle_time_reduction", "target": "20% reduction vs baseline"}]
        if "exception" in insight.finding_type:
            metrics.append({"metric": "exception_rate",   "target": "50% reduction"})
        if "rework" in insight.finding_type:
            metrics.append({"metric": "rework_rate",      "target": "30% reduction"})
        if insight.impact:
            metrics.append({"metric": "business_impact",  "target": insight.impact[:120]})
        return metrics

    def _detect_missing(self, insight) -> tuple[list, list]:
        missing_tools: list[str] = []
        missing_data:  list[str] = []
        systems = insight.systems_involved if isinstance(insight.systems_involved, list) else []
        # Flag systems that look like they need a connector we can't auto-build
        for sys in systems:
            s = sys.lower()
            if any(kw in s for kw in ("erp", "sap", "salesforce", "workday", "peoplesoft")):
                missing_tools.append(f"Connector required for {sys}")
        if not systems and insight.finding_type != "other":
            missing_data.append("No source systems specified — data access plan required")
        return missing_tools, missing_data

    def _derive_risk_level(self, risk_score: int) -> str:
        if risk_score <= 2:
            return "low"
        if risk_score <= 5:
            return "medium"
        if risk_score <= 8:
            return "high"
        return "blocked"


# ── LLM-driven blueprint generation ─────────────────────────────────────────────

_LLM_SYSTEM_PROMPT = (
    "You are a solutions architect for an enterprise agentic-automation platform. "
    "Given a normalised process-intelligence finding, you design the blueprint for a "
    "single AI agent that automates it. You propose the operating workflow, the tools "
    "it needs, and the guardrails and human-approval gates that keep it safe.\n\n"
    "Hard rules:\n"
    "- Only propose tools from the provided catalogue. Never invent tool names.\n"
    "- Every workflow step must be concrete and grounded in the finding.\n"
    "- Prefer a human-approval gate before any irreversible or externally-visible action.\n"
    "- Respond with a SINGLE JSON object and nothing else — no prose, no code fences."
)

# The design fields the LLM is asked to produce. Kept intentionally small: scoring,
# risk, status, and versioning stay deterministic in the shared pipeline.
_LLM_DESIGN_KEYS = (
    "agent_name", "mission", "trigger", "inputs", "outputs",
    "tools", "workflow_steps", "guardrails", "human_approval_points", "success_metrics",
)


class LLMBlueprintGenerator(BlueprintGenerator):
    """
    Blueprint generator that asks an LLM to propose the agent design.

    Only the *design* (name, mission, workflow steps, tools, guardrails, …) is
    LLM-generated; opportunity scoring, risk level, missing-requirement detection,
    status, and versioning remain the deterministic logic inherited from
    :class:`BlueprintGenerator`, so blueprints stay reproducible and auditable.

    The LLM never blocks the pipeline: if no API key is configured, the SDK is
    unavailable, or the response is malformed, generation falls back to the
    deterministic derivation.  The result is always a valid, human-reviewable
    blueprint left in DRAFT/NEEDS_* status behind the existing approval gate.
    """

    DEFAULT_MODEL = "claude-opus-4-8"

    def __init__(self, model_id: str | None = None):
        super().__init__()
        self.model_id = model_id or self.DEFAULT_MODEL

    def derive_design(self, insight, risk_level: str) -> dict:
        base = super().derive_design(insight, risk_level)
        raw = self._call_llm(insight)
        if not raw:
            logger.info(
                "LLM blueprint generation unavailable for '%s'; using deterministic design.",
                insight.process_name,
            )
            return base

        data = self._parse_json(raw)
        if not isinstance(data, dict):
            logger.warning(
                "LLM blueprint response for '%s' was not a JSON object; using deterministic design.",
                insight.process_name,
            )
            return base

        merged = self._merge_design(base, data)
        logger.info(
            "LLM blueprint proposal accepted for '%s' (%d workflow steps).",
            insight.process_name, len(merged["workflow_steps"]),
        )
        return merged

    # ── LLM call ────────────────────────────────────────────────────────────────

    def _call_llm(self, insight) -> str | None:
        """Return the raw model response text, or None if the LLM path is unavailable."""
        api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        try:
            import anthropic
        except ImportError:
            return None

        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=self.model_id,
                max_tokens=4096,
                thinking={"type": "adaptive"},
                system=_LLM_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._build_prompt(insight)}],
            )
            return self._response_text(response)
        except Exception as exc:  # network, auth, rate-limit, SDK errors — never fatal
            logger.warning("LLM blueprint generation failed for '%s': %s", insight.process_name, exc)
            return None

    def _build_prompt(self, insight) -> str:
        systems = insight.systems_involved if isinstance(insight.systems_involved, list) else []
        catalog = self._tool_catalog()
        schema_hint = json.dumps({
            "agent_name": "string",
            "mission": "string",
            "trigger": "string — what causes the agent to run",
            "inputs":  [{"source": "string", "description": "string"}],
            "outputs": [{"artifact": "string"}],
            "tools":   [{"name": "one of the catalogue tools", "target": "system it connects to"}],
            "workflow_steps": [
                {"step": "short_name", "description": "string",
                 "depends_on": ["earlier step_name(s), optional"]}
            ],
            "guardrails": [{"rule": "string", "description": "string"}],
            "human_approval_points": [{"step": "step_name", "description": "string"}],
            "success_metrics": [{"metric": "string", "target": "string"}],
        }, indent=2)
        return (
            "Design an agent for the following process-intelligence finding.\n\n"
            f"Process name: {insight.process_name}\n"
            f"Finding type: {insight.finding_type}\n"
            f"Summary: {insight.summary}\n"
            f"Business impact: {insight.impact or 'not stated'}\n"
            f"Frequency: {insight.frequency or 'not stated'}\n"
            f"Systems involved: {', '.join(systems) if systems else 'not stated'}\n"
            f"Recommended action: {insight.recommended_action or 'not stated'}\n"
            f"Risk notes: {insight.risk_notes or 'none'}\n\n"
            f"Available tool catalogue (choose tools ONLY from this list): {', '.join(catalog)}\n\n"
            "Return a single JSON object with exactly these fields:\n"
            f"{schema_hint}\n\n"
            "Make workflow_steps specific to this process. Use depends_on to express "
            "ordering; a linear pipeline can omit it."
        )

    @staticmethod
    def _tool_catalog() -> list[str]:
        names: set[str] = {"sql_connector", "rest_connector"}
        try:
            from controlplane.services.tools import tool_registry
            names.update(tool_registry.names())
        except Exception:  # registry import/order issues must not break generation
            pass
        return sorted(names)

    @staticmethod
    def _response_text(response) -> str:
        parts = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text" and getattr(block, "text", ""):
                parts.append(block.text)
        return "".join(parts)

    # ── parsing & validation ─────────────────────────────────────────────────────

    @staticmethod
    def _parse_json(raw: str):
        """Extract and parse the JSON object from a model response; None on failure."""
        if not raw:
            return None
        text = raw.strip()
        # Tolerate accidental ```json fences or surrounding prose.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        try:
            return json.loads(text[start:end + 1])
        except (ValueError, TypeError):
            return None

    def _merge_design(self, base: dict, data: dict) -> dict:
        """
        Overlay validated LLM fields onto the deterministic design.

        Any field the LLM omits or malforms keeps its deterministic value, so the
        downstream pipeline (build, compile, execute) always receives a complete,
        well-formed design.
        """
        design = dict(base)

        for key in ("agent_name", "mission", "trigger"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                design[key] = value.strip()

        for key in ("inputs", "outputs", "tools", "guardrails",
                    "human_approval_points", "success_metrics"):
            value = data.get(key)
            if isinstance(value, list) and all(isinstance(i, dict) for i in value) and value:
                design[key] = value

        steps = self._coerce_steps(data.get("workflow_steps"))
        if steps:
            design["workflow_steps"] = steps

        return design

    @staticmethod
    def _coerce_steps(value) -> list | None:
        """Normalise LLM workflow steps into the {step, description, depends_on?} shape."""
        if not isinstance(value, list):
            return None
        steps = []
        for item in value:
            if not isinstance(item, dict):
                continue
            name = str(item.get("step") or item.get("name") or "").strip()
            if not name:
                continue
            entry = {"step": name, "description": str(item.get("description") or "").strip()}
            deps = item.get("depends_on")
            if isinstance(deps, list):
                cleaned = [str(d).strip() for d in deps if str(d).strip()]
                if cleaned:
                    entry["depends_on"] = cleaned
            steps.append(entry)
        return steps or None


class BuildCompiler:
    """
    Converts an approved AgentBlueprint into a runnable Agent.

    Rules:
      - Blueprint must be in APPROVED status.
      - Agent is created in DRAFT status (requires normal governance promotions).
      - blueprint.status is set to BUILT and blueprint.built_agent is linked.
    """

    def build(self, blueprint, built_by: str = "factory") -> "Agent":
        from controlplane.models import Agent, AgentBlueprint, AuditLog

        if blueprint.status != AgentBlueprint.Status.APPROVED:
            raise ValueError(
                f"Blueprint must be APPROVED to build, current status: {blueprint.status}"
            )
        if blueprint.risk_level == AgentBlueprint.RiskLevel.BLOCKED:
            raise ValueError("Blueprint is BLOCKED — resolve risk issues before building.")

        slug = self._generate_slug(blueprint.agent_name)
        risk_tier = {"low": 1, "medium": 2, "high": 3, "blocked": 4}.get(blueprint.risk_level, 1)

        # Derive business_unit string
        bu_name = ""
        if blueprint.insight and blueprint.insight.business_unit:
            bu_name = blueprint.insight.business_unit.name
        bu_fk = blueprint.insight.business_unit if blueprint.insight else None

        system_prompt = self._build_system_prompt(blueprint)

        agent = Agent.objects.create(
            slug              = slug,
            name              = blueprint.agent_name,
            kind              = Agent.Kind.CUSTOM,
            integration_mode  = Agent.IntegrationMode.SDK,
            platform          = Agent.Platform.DJANGO,
            business_unit     = bu_name or "Factory-generated",
            owner             = built_by,
            technical_owner   = built_by,
            purpose           = blueprint.mission,
            system_prompt     = system_prompt,
            status            = Agent.Status.DRAFT,
            risk_tier         = risk_tier,
            org_unit          = bu_fk,
        )

        # M2: materialise the blueprint's tools as sandbox/proposed bindings so
        # the DRAFT agent is runnable (dry-run) immediately.  Never live.
        from controlplane.services.tools.bindings import create_bindings_from_plan
        bindings = create_bindings_from_plan(agent, blueprint.tools or [], created_by=built_by)
        agent.tool_names = [b.tool_name for b in bindings]
        agent.save(update_fields=["tool_names", "updated_at"])

        blueprint.built_agent = agent
        blueprint.status = AgentBlueprint.Status.BUILT
        blueprint.save(update_fields=["built_agent", "status", "updated_at"])

        AuditLog.objects.create(
            actor         = built_by,
            action        = "blueprint_built",
            resource_type = "AgentBlueprint",
            resource_id   = str(blueprint.id),
            payload       = {"agent_id": str(agent.id), "agent_slug": slug},
        )

        logger.info("Built agent '%s' (slug=%s) from blueprint %s", agent.name, slug, blueprint.id)
        return agent

    # ── helpers ────────────────────────────────────────────────────────────────

    def _generate_slug(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
        suffix = str(uuid.uuid4())[:6]
        return f"{base}-{suffix}"

    def _build_system_prompt(self, blueprint) -> str:
        steps_text = "\n".join(
            f"  {i+1}. {s.get('step','')}: {s.get('description','')}"
            for i, s in enumerate(blueprint.workflow_steps or [])
        )
        rails_text = "\n".join(
            f"  - {r.get('rule','')}: {r.get('description','')}"
            for r in (blueprint.guardrails or [])
        )
        return (
            f"You are {blueprint.agent_name}.\n\n"
            f"Mission: {blueprint.mission}\n\n"
            f"Trigger: {blueprint.trigger}\n\n"
            f"Workflow:\n{steps_text}\n\n"
            f"Guardrails:\n{rails_text}\n"
        )


# Module-level singletons
opportunity_scorer      = OpportunityScorer()
blueprint_generator     = BlueprintGenerator()
llm_blueprint_generator = LLMBlueprintGenerator()
build_compiler          = BuildCompiler()
