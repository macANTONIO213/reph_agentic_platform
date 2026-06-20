"""
Tool Bindings — Layer 1 of the autonomous agent build (see AUTONOMOUS_BUILD_DESIGN.md).

Turns an ``AgentToolBinding`` into an executable, connector-backed tool, and
resolves an agent's per-run toolset.  Bindings carry the safety teeth:

  proposed → not exposed, not executable
  sandbox  → exposed; handler dry-runs (validates inputs, NO external call)
  live     → handler calls the real DataConnector (requires approval)

A sandbox run (agent not yet pilot/production) forces sandbox behaviour for
*every* binding, so a candidate agent can be exercised without touching
production systems. Promotion to ``live`` is an explicit, approval-gated step.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from controlplane.services.tools.registry import ToolContext, ToolSpec

logger = logging.getLogger(__name__)


def _slug_tool_name(raw: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(raw).lower()).strip("_")
    return s[:60] or "tool"


# ── connector tool schemas ────────────────────────────────────────────────────

def _schema_for(binding) -> dict:
    """JSON schema for the connector tool, derived from connector type."""
    ctype = getattr(binding.connector, "connector_type", None)
    if ctype == "sql":
        return {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A single SELECT statement."},
            },
            "required": ["sql"],
        }
    # REST / GraphQL / default
    return {
        "type": "object",
        "properties": {
            "path":   {"type": "string", "description": "Request path relative to the connector base URL."},
            "params": {"type": "object", "description": "Optional query parameters."},
        },
        "required": ["path"],
    }


def _describe(binding) -> str:
    if binding.description:
        return binding.description
    target = binding.connector.name if binding.connector_id else "an unbound system"
    return f"Query {target} via the '{binding.tool_name}' binding."


# ── connector execution ───────────────────────────────────────────────────────

def _execute_live(binding, inp: dict, actor: str) -> dict:
    """Call the real connector. Raises on misconfiguration / connector errors."""
    connector = binding.connector
    if connector is None:
        return {"error": f"Binding '{binding.tool_name}' has no connector attached."}

    ctype = connector.connector_type
    if ctype == "sql":
        from controlplane.services.connectors.sql_connector import SqlConnector
        return SqlConnector(connector).query(sql=inp.get("sql", ""), actor=actor)

    from controlplane.services.connectors.rest_connector import RestConnector
    rc = RestConnector(connector)
    op = (binding.operation or "get").lower()
    if op == "post":
        return rc.post(path=inp.get("path", ""), body=inp.get("body", {}), actor=actor)
    return rc.get(path=inp.get("path", ""), params=inp.get("params"), actor=actor)


def _dry_run(binding, inp: dict) -> dict:
    """Sandbox execution — validate inputs and echo intent, NEVER call out."""
    connector_name = binding.connector.name if binding.connector_id else None
    missing = [k for k in _schema_for(binding).get("required", []) if not inp.get(k)]
    return {
        "mode": "sandbox",
        "tool": binding.tool_name,
        "connector": connector_name,
        "operation": binding.operation or ("query" if (binding.connector and binding.connector.connector_type == "sql") else "get"),
        "would_execute": inp,
        "input_valid": not missing,
        "missing_inputs": missing,
        "note": "Sandbox dry-run: no external call was made.",
    }


def build_connector_spec(binding) -> ToolSpec:
    """Build a ToolSpec whose handler honours the binding's effective mode."""

    def handler(inp: dict, ctx: ToolContext) -> dict:
        mode = binding.effective_mode(getattr(ctx, "mode", "live"))
        if mode == "live":
            actor = getattr(ctx, "actor", "agent")
            try:
                return _execute_live(binding, inp, actor)
            except Exception as exc:  # noqa: BLE001 — surface as tool error, never crash run
                logger.warning("Live binding '%s' failed: %s", binding.tool_name, exc)
                return {"error": f"Connector call failed: {exc}"}
        return _dry_run(binding, inp)

    return ToolSpec(
        name=binding.tool_name,
        description=_describe(binding),
        input_schema=_schema_for(binding),
        handler=handler,
        risk_tier=getattr(binding.agent, "risk_tier", 1),
        requires_binding=True,
    )


# ── resolution ────────────────────────────────────────────────────────────────

def resolve_bindings(agent) -> dict:
    """Return {tool_name: AgentToolBinding} for executable (non-proposed) bindings."""
    from controlplane.models import AgentToolBinding
    qs = (AgentToolBinding.objects
          .filter(agent=agent)
          .exclude(binding_status=AgentToolBinding.Status.PROPOSED)
          .select_related("connector"))
    return {b.tool_name: b for b in qs}


def toolset_for(agent, mode: str = "live") -> tuple[dict, dict]:
    """
    Resolve an agent's connector toolset for a run.

    Returns ``(extra_specs, bindings)``:
      extra_specs — {tool_name: ToolSpec} for connector-backed tools
      bindings    — {tool_name: AgentToolBinding} (attach to ToolContext.bindings)

    ``mode`` is informational here; per-binding effective mode is resolved at
    dispatch time via ``binding.effective_mode(ctx.mode)``.
    """
    bindings = resolve_bindings(agent)
    extra_specs = {name: build_connector_spec(b) for name, b in bindings.items()}
    return extra_specs, bindings


# ── creation from a blueprint / package plan ───────────────────────────────────

def _match_connector(name: str, target: str):
    """Find a DataConnector whose name matches the declared tool/system."""
    from controlplane.models import DataConnector
    for candidate in (target, name):
        if not candidate:
            continue
        conn = DataConnector.objects.filter(name__iexact=candidate, is_active=True).first()
        if conn:
            return conn
    return None


def _infer_operation(connector, declared_name: str) -> str:
    if connector is not None:
        return "query" if connector.connector_type == "sql" else "get"
    return "query" if "sql" in (declared_name or "").lower() else "get"


def create_bindings_from_plan(agent, plan, *, created_by: str = "factory", attach_connectors: bool = True):
    """
    Create AgentToolBinding rows from a blueprint's ``tools`` list or a package's
    ``tool_binding_plan``.

    Safety: a binding is created as SANDBOX when a real DataConnector is matched
    (dry-run only until promoted), otherwise PROPOSED.  This helper NEVER creates
    a live binding — that requires the explicit, approval-gated promote_to_live.

    Idempotent: re-running for the same agent updates existing rows by tool_name.
    Returns the list of bindings (ordered as in the plan).
    """
    from controlplane.models import AgentToolBinding

    if isinstance(plan, dict):
        # {"tools": [...], "data_sources": [...]} shape → flatten.
        flat = []
        for t in plan.get("tools", []) or []:
            flat.append(t if isinstance(t, dict) else {"name": t})
        for d in plan.get("data_sources", []) or []:
            flat.append({**(d if isinstance(d, dict) else {"name": d}), "kind": "data"})
        plan = flat
    if not isinstance(plan, list):
        return []

    bindings = []
    seen: set[str] = set()
    for entry in plan:
        if not isinstance(entry, dict):
            entry = {"name": str(entry)}
        raw_name = entry.get("name") or entry.get("system") or entry.get("tool") or entry.get("data_source")
        target = entry.get("target") or entry.get("system") or entry.get("data_source") or ""
        if not raw_name and not target:
            continue

        tool_name = _slug_tool_name(target or raw_name)
        # De-duplicate within this plan (unique_together is (agent, tool_name)).
        base = tool_name
        i = 2
        while tool_name in seen:
            tool_name = f"{base}_{i}"
            i += 1
        seen.add(tool_name)

        connector = _match_connector(raw_name, target) if attach_connectors else None
        status = (
            AgentToolBinding.Status.SANDBOX if connector is not None
            else AgentToolBinding.Status.PROPOSED
        )

        binding, _ = AgentToolBinding.objects.update_or_create(
            agent=agent,
            tool_name=tool_name,
            defaults={
                "connector":      connector,
                "binding_status": status,
                "operation":      _infer_operation(connector, str(raw_name)),
                "config": {
                    "declared_name": raw_name,
                    "target":        target,
                    "source_status": entry.get("binding_status"),
                },
                "created_by": created_by,
            },
        )
        bindings.append(binding)

    return bindings


# ── lifecycle ─────────────────────────────────────────────────────────────────

def promote_to_sandbox(binding, *, by: str = "system"):
    """Make a proposed binding executable as a dry-run (no approval required)."""
    from controlplane.models import AgentToolBinding
    binding.binding_status = AgentToolBinding.Status.SANDBOX
    binding.created_by = binding.created_by or by
    binding.save(update_fields=["binding_status", "created_by", "updated_at"])
    return binding


def promote_to_live(binding, *, approver, package=None):
    """
    Promote a binding to live execution.

    Guards (all required):
      - a connector must be attached
      - an approver must be supplied (recorded on the binding)
      - if a source package is supplied, it must permit production tool binding
    """
    from controlplane.models import AgentToolBinding, AuditLog

    if binding.connector_id is None:
        raise ValueError("Cannot go live: no DataConnector attached to this binding.")
    if approver is None:
        raise ValueError("Cannot go live: an approver is required.")
    if package is not None and not package.can_bind_production_tools:
        raise ValueError(
            "Cannot go live: the source package's safety_boundary forbids "
            "binding production tools (can_bind_production_tools=false)."
        )

    binding.binding_status = AgentToolBinding.Status.LIVE
    binding.approved_by = approver if not isinstance(approver, str) else None
    binding.approved_at = timezone.now()
    binding.save(update_fields=["binding_status", "approved_by", "approved_at", "updated_at"])

    AuditLog.objects.create(
        actor=getattr(approver, "username", str(approver)),
        action="tool_binding_promoted_live",
        resource_type="AgentToolBinding",
        resource_id=str(binding.id),
        payload={
            "agent_id": str(binding.agent_id),
            "tool_name": binding.tool_name,
            "connector_id": str(binding.connector_id) if binding.connector_id else None,
        },
    )
    return binding
