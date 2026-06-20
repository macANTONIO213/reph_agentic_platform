"""
Workflow Compiler — Layer 3 of the autonomous agent build (see AUTONOMOUS_BUILD_DESIGN.md).

Compiles a multi-step blueprint (or package manifest) into a runnable Workflow
DAG that the existing OrchestratorService executes.

Single-agent mode (M3): every step becomes a WorkflowTask assigned to the same
built sandbox agent, chained linearly (or by declared depends_on), with each
step's prose carried in its input_template and upstream output substituted via
``{{outputs.<prev>.text}}``.  Distinct per-step agents are a later refinement.

Usage::
    from controlplane.services.workflow_compiler import workflow_compiler
    wf = workflow_compiler.compile(blueprint)                 # build the DAG
    wf, run = workflow_compiler.compile_and_run(blueprint,    # build + run in sandbox
                                                inputs={"message": "Invoice 123"})
"""
from __future__ import annotations

import logging
import re
import uuid

logger = logging.getLogger(__name__)


def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60]
    return s or fallback


def should_compile_workflow(blueprint) -> bool:
    """Heuristic: a multi-step blueprint is better expressed as a DAG."""
    steps = blueprint.workflow_steps or []
    return isinstance(steps, list) and len(steps) > 1


class WorkflowCompiler:
    """Turns a blueprint's workflow_steps into a Workflow + WorkflowTask DAG."""

    def compile(self, blueprint, *, agent=None, built_by: str = "factory", activate: bool = False):
        """
        Compile ``blueprint.workflow_steps`` into a DRAFT Workflow.

        ``agent`` defaults to the blueprint's built agent; a built sandbox agent
        is required (compile after BuildCompiler.build).  Returns the Workflow.
        """
        from controlplane.models import Workflow, WorkflowTask

        agent = agent or getattr(blueprint, "built_agent", None)
        if agent is None:
            raise ValueError(
                "WorkflowCompiler requires a built agent — build the blueprint first."
            )

        steps = blueprint.workflow_steps or []
        if not isinstance(steps, list) or not steps:
            raise ValueError("Blueprint has no workflow_steps to compile.")

        business_unit = None
        if getattr(blueprint, "insight", None) and blueprint.insight.business_unit_id:
            business_unit = blueprint.insight.business_unit

        wf_slug = f"{_slug(blueprint.agent_name, 'agent')}-wf-{str(uuid.uuid4())[:6]}"
        workflow = Workflow.objects.create(
            name=f"{blueprint.agent_name} — Workflow",
            slug=wf_slug,
            description=blueprint.mission or "",
            business_unit=business_unit,
            status=Workflow.Status.ACTIVE if activate else Workflow.Status.DRAFT,
            owner=built_by,
            created_by=built_by,
        )

        # First pass: assign unique step names so depends_on can reference them.
        step_names: list[str] = []
        seen: set[str] = set()
        for i, step in enumerate(steps):
            label = step.get("step") if isinstance(step, dict) else step
            name = _slug(label, f"step-{i + 1}")
            base = name
            n = 2
            while name in seen:
                name = f"{base}-{n}"
                n += 1
            seen.add(name)
            step_names.append(name)

        # Second pass: create tasks with dependency + template wiring.
        for i, step in enumerate(steps):
            step = step if isinstance(step, dict) else {"step": step}
            name = step_names[i]
            description = step.get("description", "") or step.get("step", "") or name

            declared = step.get("depends_on")
            if isinstance(declared, list) and declared:
                depends_on = [_slug(d, d) for d in declared]
            elif i > 0:
                depends_on = [step_names[i - 1]]   # linear chain
            else:
                depends_on = []

            input_template = self._input_template(description, depends_on)

            WorkflowTask.objects.create(
                workflow=workflow,
                step_name=name,
                agent=agent,
                system_prompt=step.get("system_prompt", "") or "",
                model_override=step.get("model_override", "") or "",
                input_template=input_template,
                depends_on=depends_on,
                order=i,
            )

        logger.info(
            "Compiled workflow '%s' (%d steps) from blueprint %s",
            workflow.slug, len(steps), getattr(blueprint, "id", "?"),
        )
        return workflow

    def _input_template(self, description: str, depends_on: list[str]) -> str:
        """
        First step reads the run input; later steps carry their instruction plus
        the upstream step's output for the orchestrator to substitute.
        """
        if not depends_on:
            instruction = description.strip()
            return f"{instruction}\n\nInput: {{{{inputs.message}}}}" if instruction else "{{inputs.message}}"
        upstream = "\n".join(
            f"- {dep}: {{{{outputs.{dep}.text}}}}" for dep in depends_on
        )
        return f"{description.strip()}\n\nUpstream results:\n{upstream}"

    # ── convenience: compile + run in sandbox ───────────────────────────────────
    def compile_and_run(self, blueprint, *, inputs: dict | None = None,
                        triggered_by: str = "factory", agent=None, activate: bool = False):
        """Compile the workflow then execute it via the orchestrator. Returns (workflow, run)."""
        from controlplane.services.orchestrator import orchestrator

        workflow = self.compile(blueprint, agent=agent, built_by=triggered_by, activate=activate)
        run = orchestrator.start(workflow, inputs=inputs or {}, triggered_by=triggered_by)
        run = orchestrator.execute(run)
        return workflow, run


# Module-level singleton.
workflow_compiler = WorkflowCompiler()
