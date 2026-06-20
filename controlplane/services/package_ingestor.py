"""
Agent Factory Package Ingestor — Phase F (package handoff)

Ingests a complete ``agent_factory_package`` exported by the Process
Intelligence Platform / Agent Blueprint Factory.  The package is the canonical
handoff object for creating a *sandbox* agent candidate.

Hard rules enforced here (never overridable by package contents):
  - A package only ever produces a sandbox / DRAFT agent.
  - Production tools are never bound automatically (bindings are *proposed*).
  - The agent is never deployed to production automatically.
  - Human / policy approval gates are never bypassed.

Usage::
    from controlplane.services.package_ingestor import package_ingestor
    pkg = package_ingestor.ingest(payload, ingested_by="user:alice")
"""
from __future__ import annotations

import logging
import re
import uuid

logger = logging.getLogger(__name__)


# ── Validation ──────────────────────────────────────────────────────────────────

class PackageValidationError(Exception):
    """Raised when a package cannot be ingested. Carries a structured report."""

    def __init__(self, report: dict):
        self.report = report
        super().__init__("; ".join(report.get("errors", [])) or "Invalid package")


# Sections required for a complete package (per the handoff contract).
REQUIRED_SECTIONS = [
    "agent_blueprint",
    "agent_build_manifest",
    "tool_binding_plan",
    "decision_policy",
    "evaluation_pack",
    "approval_route",
    "approval_progress",
    "telemetry_contract",
    "telemetry_feedback_plan",
    "safety_boundary",
]

# Sections whose absence blocks ingestion entirely — we cannot build even a
# sandbox candidate without them.
CRITICAL_SECTIONS = ["agent_blueprint", "agent_build_manifest", "safety_boundary"]

_RISK_TIER_TO_LEVEL = {1: "low", 2: "medium", 3: "high", 4: "blocked"}


class PackageIngestor:
    """Parses, validates, persists, and materialises an agent_factory_package."""

    # ── public entrypoint ───────────────────────────────────────────────────────

    def ingest(self, payload: dict, ingested_by: str = "factory"):
        """
        Ingest a package payload and return the persisted AgentFactoryPackage.

        ``payload`` may be ``{"agent_factory_package": {...}}`` or the package
        object directly.  Raises PackageValidationError on a critical failure
        (the caller can inspect ``exc.report``).
        """
        from controlplane.models import AgentFactoryPackage

        pkg_data = self._unwrap(payload)
        report = self._validate(pkg_data)

        if not report["ok"]:
            # Persist the rejected package for traceability where we can, then raise.
            self._persist_invalid(pkg_data, report, ingested_by)
            raise PackageValidationError(report)

        package = self._persist_package(pkg_data, report, ingested_by)

        # Source traceability: upsert the originating ProcessInsight.
        insight = self._upsert_insight(pkg_data, package)
        package.insight = insight

        # Blueprint: persist the proposed design and its approval gate.
        blueprint = self._create_blueprint(pkg_data, insight, package)
        package.blueprint = blueprint
        package.risk_tier = self._risk_tier(pkg_data)

        # Sandbox agent — only if the safety boundary permits it.
        if package.can_build_sandbox_agent:
            agent = self._create_sandbox_agent(pkg_data, package, ingested_by)
            package.sandbox_agent = agent
            package.status = AgentFactoryPackage.Status.SANDBOX_CREATED
            self._register_proposed_bindings(pkg_data, package)
            self._generate_eval_cases(pkg_data, agent, ingested_by)
        else:
            report.setdefault("warnings", []).append(
                "safety_boundary.can_build_sandbox_agent is not true — "
                "no sandbox agent was created."
            )
            package.validation_report = report
            package.status = AgentFactoryPackage.Status.RECEIVED

        package.save()

        self._emit_telemetry(pkg_data, package, ingested_by)
        self._audit(package, ingested_by)

        logger.info(
            "Ingested agent_factory_package '%s' (status=%s, sandbox_agent=%s)",
            package.package_id, package.status,
            package.sandbox_agent_id,
        )
        return package

    # ── unwrap + validate ────────────────────────────────────────────────────────

    def _unwrap(self, payload) -> dict:
        if not isinstance(payload, dict):
            raise PackageValidationError({
                "ok": False,
                "errors": ["Payload must be a JSON object."],
                "warnings": [], "missing_sections": [],
            })
        if "agent_factory_package" in payload and isinstance(
            payload["agent_factory_package"], dict
        ):
            return payload["agent_factory_package"]
        return payload

    def _validate(self, pkg: dict) -> dict:
        from controlplane.models import AgentFactoryPackage

        errors: list[str] = []
        warnings: list[str] = []
        missing: list[str] = []

        version = pkg.get("package_version")
        if version != AgentFactoryPackage.PACKAGE_VERSION:
            errors.append(
                f"Unsupported package_version '{version}'. "
                f"Expected '{AgentFactoryPackage.PACKAGE_VERSION}'."
            )

        ptype = pkg.get("package_type")
        if ptype != AgentFactoryPackage.PACKAGE_TYPE:
            errors.append(
                f"Unsupported package_type '{ptype}'. "
                f"Expected '{AgentFactoryPackage.PACKAGE_TYPE}'."
            )

        if not str(pkg.get("package_id", "")).strip():
            errors.append("package_id is required.")

        # Section presence.
        for section in REQUIRED_SECTIONS:
            val = pkg.get(section)
            if val in (None, "", {}, []):
                missing.append(section)
                msg = f"Missing or empty section: {section}"
                if section in CRITICAL_SECTIONS:
                    errors.append(msg)
                else:
                    warnings.append(msg)

        # Source provenance.
        source = pkg.get("source") or {}
        if not isinstance(source, dict) or not source.get("process_insight"):
            warnings.append("source.process_insight is missing — provenance limited.")
        if isinstance(source, dict) and not source.get("process_intelligence_output"):
            warnings.append("source.process_intelligence_output is missing.")

        if not str(pkg.get("blueprint_id", "")).strip():
            warnings.append("blueprint_id is missing — link back to Blueprint Factory unavailable.")

        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "missing_sections": missing,
        }

    # ── persistence ────────────────────────────────────────────────────────────

    def _persist_invalid(self, pkg: dict, report: dict, ingested_by: str) -> None:
        """Record an invalid package when we have a package_id to key on."""
        from controlplane.models import AgentFactoryPackage

        pkg_id = str(pkg.get("package_id", "")).strip()
        if not pkg_id:
            return
        AgentFactoryPackage.objects.update_or_create(
            package_id=pkg_id,
            defaults={
                "external_blueprint_id": str(pkg.get("blueprint_id", "")),
                "package_version": str(pkg.get("package_version", "")),
                "package_type":    str(pkg.get("package_type", "")),
                "raw_package":     pkg,
                "validation_report": report,
                "status": AgentFactoryPackage.Status.INVALID,
                "ingested_by": ingested_by,
            },
        )

    def _persist_package(self, pkg: dict, report: dict, ingested_by: str):
        from controlplane.models import AgentFactoryPackage

        package, _ = AgentFactoryPackage.objects.update_or_create(
            package_id=str(pkg["package_id"]).strip(),
            defaults={
                "external_blueprint_id":   str(pkg.get("blueprint_id", "")),
                "package_version":         str(pkg.get("package_version", "")),
                "package_type":            str(pkg.get("package_type", "")),
                "source":                  pkg.get("source", {}) or {},
                "agent_blueprint":         pkg.get("agent_blueprint", {}) or {},
                "agent_build_manifest":    pkg.get("agent_build_manifest", {}) or {},
                "tool_binding_plan":       self._normalise_bindings(pkg.get("tool_binding_plan")),
                "decision_policy":         pkg.get("decision_policy", {}) or {},
                "evaluation_pack":         pkg.get("evaluation_pack", {}) or {},
                "approval_route":          pkg.get("approval_route", {}) or {},
                "approval_progress":       pkg.get("approval_progress", {}) or {},
                "telemetry_contract":      pkg.get("telemetry_contract", {}) or {},
                "telemetry_feedback_plan": pkg.get("telemetry_feedback_plan", {}) or {},
                "safety_boundary":         pkg.get("safety_boundary", {}) or {},
                "validation_report":       report,
                "raw_package":             pkg,
                "status":                  AgentFactoryPackage.Status.RECEIVED,
                "ingested_by":             ingested_by,
            },
        )
        return package

    def _upsert_insight(self, pkg: dict, package):
        """Recreate the originating ProcessInsight so the source stays traceable."""
        from controlplane.models import ProcessInsight, BusinessUnit

        source = pkg.get("source", {}) or {}
        pi = source.get("process_insight", {}) or {}

        ref = (
            str(pi.get("source_reference", "")).strip()
            or str(pi.get("id", "")).strip()
            or f"pkg:{package.package_id}"
        )

        bu = None
        bu_name = pi.get("business_unit") or (pkg.get("agent_blueprint", {}) or {}).get("business_unit")
        if bu_name:
            bu = BusinessUnit.objects.filter(name=bu_name).first()

        finding_type = pi.get("finding_type", "automation_opportunity")
        valid_types = set(ProcessInsight.FindingType.values)
        if finding_type not in valid_types:
            finding_type = "other"

        insight, _ = ProcessInsight.objects.update_or_create(
            source_reference=ref,
            defaults={
                "process_name":       pi.get("process_name") or (pkg.get("agent_blueprint", {}) or {}).get("agent_name", "Imported process"),
                "finding_type":       finding_type,
                "summary":            pi.get("summary", "") or "Imported from agent_factory_package.",
                "impact":             pi.get("impact", ""),
                "frequency":          pi.get("frequency", ""),
                "systems_involved":   pi.get("systems_involved", []) or [],
                "recommended_action": pi.get("recommended_action", ""),
                "risk_notes":         pi.get("risk_notes", ""),
                "evidence":           source.get("process_intelligence_output", {}) or pi.get("evidence", {}) or {},
                "business_unit":      bu,
            },
        )
        return insight

    def _create_blueprint(self, pkg: dict, insight, package):
        """Persist the proposed agent design and its approval gate (never approved)."""
        from controlplane.models import AgentBlueprint

        bp_src   = pkg.get("agent_blueprint", {}) or {}
        manifest = pkg.get("agent_build_manifest", {}) or {}
        policy   = pkg.get("decision_policy", {}) or {}
        evalpack = pkg.get("evaluation_pack", {}) or {}
        bindings = self._normalise_bindings(pkg.get("tool_binding_plan"))

        risk_tier = self._risk_tier(pkg)
        risk_level = _RISK_TIER_TO_LEVEL.get(risk_tier, "low")

        missing_tools = [
            f"{b.get('name') or b.get('system') or 'tool'}: {b.get('binding_status')}"
            for b in bindings
            if b.get("binding_status") not in ("available", "bound", "ready")
        ]
        missing_data = [
            f"{b.get('name') or b.get('data_source') or 'data'}: {b.get('binding_status')}"
            for b in bindings
            if b.get("kind") == "data" and b.get("binding_status") not in ("available", "bound", "ready")
        ]

        if missing_data:
            status = AgentBlueprint.Status.NEEDS_DATA
        elif missing_tools:
            status = AgentBlueprint.Status.NEEDS_TOOL
        else:
            status = AgentBlueprint.Status.DRAFT

        version = AgentBlueprint.objects.filter(insight=insight).count() + 1 if insight else 1

        blueprint = AgentBlueprint.objects.create(
            insight               = insight,
            version               = version,
            agent_name            = bp_src.get("agent_name") or bp_src.get("name") or "Imported Agent",
            mission               = bp_src.get("purpose") or bp_src.get("mission") or "Imported from agent_factory_package.",
            trigger               = manifest.get("trigger", "") or bp_src.get("trigger", ""),
            inputs                = manifest.get("inputs", []) or [],
            outputs               = manifest.get("outputs", []) or [],
            tools                 = bindings,
            workflow_steps        = manifest.get("workflow_steps") or manifest.get("workflow", []) or [],
            guardrails            = policy.get("rules", []) or policy.get("guardrails", []) or [],
            human_approval_points = policy.get("human_approval_points") or policy.get("escalation", []) or [],
            success_metrics       = evalpack.get("success_metrics") or evalpack.get("test_criteria", []) or [],
            missing_tools         = missing_tools,
            missing_data          = missing_data,
            status                = status,
            risk_level            = risk_level,
            business_value_score  = self._clamp(bp_src.get("business_value", 5)),
        )
        return blueprint

    def _create_sandbox_agent(self, pkg: dict, package, ingested_by: str):
        """Create a DRAFT (sandbox) agent from the build manifest. Never live."""
        from controlplane.models import Agent, AuditLog, BusinessUnit

        bp_src   = pkg.get("agent_blueprint", {}) or {}
        manifest = pkg.get("agent_build_manifest", {}) or {}
        bindings = self._normalise_bindings(pkg.get("tool_binding_plan"))

        name = bp_src.get("agent_name") or bp_src.get("name") or "Sandbox Agent"
        slug = self._slug(name)
        risk_tier = self._risk_tier(pkg)

        bu_name = bp_src.get("business_unit") or ""
        bu_fk = BusinessUnit.objects.filter(name=bu_name).first() if bu_name else None

        platform = self._map_platform(manifest.get("runtime") or manifest.get("platform"))

        # Proposed tool names only — never bound to live systems.
        tool_names = [b.get("name") or b.get("system") or b.get("tool") for b in bindings]
        tool_names = [t for t in tool_names if t]

        agent = Agent.objects.create(
            slug             = slug,
            name             = name,
            kind             = Agent.Kind.CUSTOM,
            integration_mode = Agent.IntegrationMode.SDK,
            platform         = platform,
            business_unit    = bu_name or "Factory-sandbox",
            owner            = ingested_by,
            technical_owner  = ingested_by,
            purpose          = bp_src.get("purpose") or bp_src.get("mission") or "Sandbox agent candidate.",
            system_prompt    = self._build_system_prompt(pkg),
            status           = Agent.Status.DRAFT,   # sandbox only — never live
            risk_tier        = risk_tier,
            tool_names       = tool_names,
            data_sources     = [],                   # no live data bindings
            org_unit         = bu_fk,
        )

        AuditLog.objects.create(
            actor         = ingested_by,
            action        = "factory_package_sandbox_created",
            resource_type = "AgentFactoryPackage",
            resource_id   = str(package.id),
            payload       = {
                "package_id": package.package_id,
                "agent_id": str(agent.id),
                "agent_slug": slug,
                "status": agent.status,
                "proposed_tools": tool_names,
            },
        )
        return agent

    def _register_proposed_bindings(self, pkg: dict, package) -> None:
        """
        Record tool/data bindings as *proposed* — we do not create live
        DataConnectors or connect production systems.
        """
        bindings = self._normalise_bindings(pkg.get("tool_binding_plan"))
        # Bindings are persisted on the package (tool_binding_plan) with their
        # status forced to non-live; nothing is connected here by design.
        package.tool_binding_plan = bindings
        logger.info(
            "Registered %d proposed binding(s) for package '%s' (none live).",
            len(bindings), package.package_id,
        )

    def _generate_eval_cases(self, pkg: dict, agent, ingested_by: str) -> None:
        """Create an EvalSuite + EvalCases from the evaluation_pack."""
        from controlplane.models import EvalSuite, EvalCase

        evalpack = pkg.get("evaluation_pack", {}) or {}
        cases = (
            evalpack.get("test_cases")
            or evalpack.get("test_criteria")
            or evalpack.get("cases")
            or []
        )
        if not isinstance(cases, list) or not cases:
            return

        suite = EvalSuite.objects.create(
            agent=agent,
            name="Factory package evaluation",
            description="Generated from agent_factory_package evaluation_pack.",
            created_by=ingested_by,
        )

        created = 0
        for idx, case in enumerate(cases):
            if not isinstance(case, dict):
                continue
            input_message = (
                case.get("input")
                or case.get("input_message")
                or case.get("prompt")
                or case.get("criterion")
                or ""
            )
            if not input_message:
                continue
            EvalCase.objects.create(
                suite=suite,
                name=case.get("name") or f"Case {idx + 1}",
                input_message=str(input_message),
                expected_keywords=case.get("expected_keywords", []) or [],
                must_not_contain=case.get("must_not_contain", []) or [],
                max_latency_ms=case.get("max_latency_ms"),
                weight=case.get("weight", 1) or 1,
            )
            created += 1

        if created == 0:
            suite.delete()
        else:
            logger.info("Generated %d eval case(s) for sandbox agent '%s'.", created, agent.slug)

    def _emit_telemetry(self, pkg: dict, package, ingested_by: str) -> None:
        """Emit an ingestion telemetry event per the telemetry_contract."""
        from controlplane.models import TelemetryEvent

        contract = pkg.get("telemetry_contract", {}) or {}
        TelemetryEvent.objects.create(
            agent=package.sandbox_agent,
            event_type="factory_package_ingested",
            actor=ingested_by,
            business_unit=(pkg.get("agent_blueprint", {}) or {}).get("business_unit", "") or "",
            payload={
                "package_id": package.package_id,
                "blueprint_id": package.external_blueprint_id,
                "status": package.status,
                "risk_tier": package.risk_tier,
                "telemetry_contract": contract,
            },
        )

    def _audit(self, package, ingested_by: str) -> None:
        from controlplane.models import AuditLog

        AuditLog.objects.create(
            actor         = ingested_by,
            action        = "factory_package_ingested",
            resource_type = "AgentFactoryPackage",
            resource_id   = str(package.id),
            payload       = {
                "package_id": package.package_id,
                "blueprint_id": package.external_blueprint_id,
                "status": package.status,
                "insight_id": str(package.insight_id) if package.insight_id else None,
                "blueprint_db_id": str(package.blueprint_id) if package.blueprint_id else None,
                "sandbox_agent_id": str(package.sandbox_agent_id) if package.sandbox_agent_id else None,
                "validation": package.validation_report,
            },
        )

    # ── helpers ────────────────────────────────────────────────────────────────

    def _normalise_bindings(self, raw) -> list:
        """
        Coerce the tool_binding_plan into a list of binding dicts and force every
        binding to a non-live status (default 'proposed').  Production binding is
        never performed by ingestion.
        """
        if isinstance(raw, dict):
            # Allow {"tools": [...], "data_sources": [...]} shape.
            items = []
            for t in raw.get("tools", []) or []:
                items.append({**(t if isinstance(t, dict) else {"name": t}), "kind": "tool"})
            for d in raw.get("data_sources", []) or []:
                items.append({**(d if isinstance(d, dict) else {"name": d}), "kind": "data"})
            if not items and raw:
                items = [raw]
            raw = items
        if not isinstance(raw, list):
            return []

        normalised = []
        for b in raw:
            if not isinstance(b, dict):
                b = {"name": str(b)}
            status = b.get("binding_status", "proposed")
            # Never allow a 'live'/'production' binding to be recorded as active.
            if status in ("live", "production", "active", "connected"):
                status = "proposed"
            b = dict(b)
            b["binding_status"] = status
            b["live"] = False
            normalised.append(b)
        return normalised

    def _risk_tier(self, pkg: dict) -> int:
        bp = pkg.get("agent_blueprint", {}) or {}
        raw = bp.get("risk_tier", bp.get("risk_level", 1))
        if isinstance(raw, str):
            raw = {"low": 1, "medium": 2, "high": 3, "blocked": 4, "critical": 4}.get(raw.lower(), 1)
        try:
            tier = int(raw)
        except (TypeError, ValueError):
            tier = 1
        return min(4, max(1, tier))

    def _clamp(self, val, lo=0, hi=10) -> int:
        try:
            return min(hi, max(lo, int(val)))
        except (TypeError, ValueError):
            return lo

    def _slug(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:40] or "agent"
        return f"{base}-{str(uuid.uuid4())[:6]}"

    def _map_platform(self, runtime) -> str:
        from controlplane.models import Agent

        mapping = {
            "django": Agent.Platform.DJANGO,
            "django_runtime": Agent.Platform.DJANGO,
            "azure": Agent.Platform.AZURE_AI,
            "azure_ai_foundry": Agent.Platform.AZURE_AI,
            "copilot": Agent.Platform.COPILOT,
            "copilot_studio": Agent.Platform.COPILOT,
            "bedrock": Agent.Platform.BEDROCK,
            "custom_api": Agent.Platform.CUSTOM,
            "vendor": Agent.Platform.VENDOR,
            "embedded": Agent.Platform.EMBEDDED,
        }
        if isinstance(runtime, str):
            return mapping.get(runtime.lower(), Agent.Platform.DJANGO)
        return Agent.Platform.DJANGO

    def _build_system_prompt(self, pkg: dict) -> str:
        bp       = pkg.get("agent_blueprint", {}) or {}
        manifest = pkg.get("agent_build_manifest", {}) or {}
        policy   = pkg.get("decision_policy", {}) or {}

        name = bp.get("agent_name") or bp.get("name") or "Sandbox Agent"
        purpose = bp.get("purpose") or bp.get("mission") or ""

        steps = manifest.get("workflow_steps") or manifest.get("workflow", []) or []
        steps_text = "\n".join(
            f"  {i + 1}. {s.get('step', s) if isinstance(s, dict) else s}"
            f"{': ' + s.get('description', '') if isinstance(s, dict) and s.get('description') else ''}"
            for i, s in enumerate(steps)
        ) or "  (none specified)"

        rules = policy.get("rules", []) or policy.get("guardrails", []) or []
        rules_text = "\n".join(
            f"  - {r.get('rule', r) if isinstance(r, dict) else r}"
            f"{': ' + r.get('description', '') if isinstance(r, dict) and r.get('description') else ''}"
            for r in rules
        ) or "  - audit_log: every action must be audit-logged"

        return (
            f"You are {name} (SANDBOX candidate — not yet approved for production).\n\n"
            f"Purpose: {purpose}\n\n"
            f"Workflow:\n{steps_text}\n\n"
            f"Decision policy / guardrails:\n{rules_text}\n\n"
            f"You must not bind production tools or take irreversible production "
            f"actions until a human or policy approval has been granted."
        )


# Module-level singleton
package_ingestor = PackageIngestor()
