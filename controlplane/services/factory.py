"""
Agent Factory Service — Phase F Blueprint Lifecycle

Provides:
  OpportunityScorer   — scores a ProcessInsight across four dimensions
  BlueprintGenerator  — derives an AgentBlueprint from a ProcessInsight
  BuildCompiler       — converts an approved AgentBlueprint into an Agent

Usage::
    from controlplane.services.factory import blueprint_generator, build_compiler

    blueprint = blueprint_generator.generate(insight)
    agent = build_compiler.build(blueprint, built_by="user:alice")
"""
from __future__ import annotations

import logging
import re
import uuid

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
        from controlplane.models import AgentBlueprint

        scores = self._scorer.score(insight)
        risk_level = self._derive_risk_level(scores["risk_score"])

        agent_name     = self._derive_name(insight)
        mission        = self._derive_mission(insight)
        trigger        = self._derive_trigger(insight)
        inputs         = self._derive_inputs(insight)
        outputs        = self._derive_outputs(insight)
        tools          = self._derive_tools(insight)
        workflow_steps = self._derive_workflow_steps(insight)
        guardrails     = self._derive_guardrails(insight, risk_level)
        approval_pts   = self._derive_approval_points(risk_level)
        metrics        = self._derive_success_metrics(insight)
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
            agent_name            = agent_name,
            mission               = mission,
            trigger               = trigger,
            inputs                = inputs,
            outputs               = outputs,
            tools                 = tools,
            workflow_steps        = workflow_steps,
            guardrails            = guardrails,
            human_approval_points = approval_pts,
            success_metrics       = metrics,
            missing_tools         = missing_tools,
            missing_data          = missing_data,
            status                = status,
            risk_level            = risk_level,
            **scores,
        )

        logger.info(
            "Blueprint generated: '%s' (score=%.1f, status=%s)",
            agent_name, scores["opportunity_score"], status,
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
opportunity_scorer    = OpportunityScorer()
blueprint_generator   = BlueprintGenerator()
build_compiler        = BuildCompiler()
