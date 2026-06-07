"""
EvalService — executes EvalSuites against live agents.

Usage::

    from controlplane.services.eval_service import eval_service

    run = eval_service.run_suite(suite=suite, triggered_by="ci/push")

The service:
  1. Creates an EvalRun record (status=running)
  2. For each EvalCase, invokes the agent via the runtime adapter
  3. Scores the response (keyword match, must-not-contain, latency)
  4. Aggregates pass rate and marks EvalRun.passed
  5. Writes an AuditLog entry
"""
import logging
import time

from django.utils import timezone

from controlplane.models import AuditLog, EvalCase, EvalRun, EvalSuite

logger = logging.getLogger(__name__)


class EvalService:
    """Stateless singleton.  Call run_suite() to execute an eval."""

    def run_suite(
        self,
        *,
        suite: EvalSuite,
        triggered_by: str = "manual",
    ) -> EvalRun:
        """
        Execute every active EvalCase in ``suite`` against its agent.
        Returns the completed EvalRun.
        """
        agent = suite.agent
        cases = list(suite.cases.all())

        run = EvalRun.objects.create(
            suite=suite,
            status=EvalRun.Status.RUNNING,
            triggered_by=triggered_by,
            total_cases=len(cases),
        )

        case_results = []
        try:
            for case in cases:
                result = self._run_case(agent, case)
                case_results.append(result)

            run.case_results = case_results
            run.passed_cases = sum(1 for r in case_results if r["passed"])
            run.status = EvalRun.Status.COMPLETE
            run.completed_at = timezone.now()
            run.save(update_fields=[
                "case_results", "passed_cases", "status", "completed_at"
            ])
            run.compute_pass_rate()

        except Exception as exc:
            logger.exception("EvalRun %s failed", run.id)
            run.status = EvalRun.Status.ERROR
            run.error_detail = str(exc)
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "error_detail", "completed_at"])

        self._audit(run, agent, triggered_by)
        return run

    # ── Internal ─────────────────────────────────────────────────────────────

    def _run_case(self, agent, case: EvalCase) -> dict:
        """
        Execute a single eval case.
        Uses the platform adapter directly (no SSE streaming) for synchronous scoring.
        """
        from controlplane.services.agent_runtime import _select_adapter_class
        from controlplane.models import AgentRun
        from controlplane.services.pricing import price_run

        started = time.perf_counter()
        meta = {"output_text": "", "input_tokens": 0, "output_tokens": 0, "model_id": ""}

        # Create a lightweight run record for the eval call
        agent_run = AgentRun.objects.create(
            agent=agent,
            user_label=f"eval:{case.suite.name}",
            channel="eval",
            input_text=case.input_message,
        )

        try:
            adapter_cls = _select_adapter_class(agent)
            adapter = adapter_cls(agent=agent, user_label="eval_runner")
            # Consume the generator fully (no streaming to client)
            for _ in adapter.execute(agent_run, case.input_message, [], meta):
                pass

            latency_ms = int((time.perf_counter() - started) * 1000)
            response = meta["output_text"].lower()

            # Score the response
            passed, reasons = self._score(case, response, latency_ms)

            agent_run.status = AgentRun.Status.COMPLETED
            agent_run.output_text = meta["output_text"]
            agent_run.input_tokens = meta["input_tokens"]
            agent_run.output_tokens = meta["output_tokens"]
            agent_run.model_id = meta["model_id"]
            agent_run.cost_usd = price_run(meta["input_tokens"], meta["output_tokens"], meta["model_id"])
            agent_run.completed_at = timezone.now()
            agent_run.latency_ms = latency_ms
            agent_run.save(update_fields=[
                "status", "output_text", "input_tokens", "output_tokens",
                "model_id", "cost_usd", "completed_at", "latency_ms",
            ])

            return {
                "case_id": str(case.id),
                "name": case.name,
                "passed": passed,
                "reasons": reasons,
                "latency_ms": latency_ms,
                "weight": case.weight,
                "run_id": str(agent_run.id),
            }

        except Exception as exc:
            agent_run.status = AgentRun.Status.FAILED
            agent_run.output_text = str(exc)
            agent_run.completed_at = timezone.now()
            agent_run.latency_ms = int((time.perf_counter() - started) * 1000)
            agent_run.save(update_fields=["status", "output_text", "completed_at", "latency_ms"])
            return {
                "case_id": str(case.id),
                "name": case.name,
                "passed": False,
                "reasons": [f"Adapter error: {exc}"],
                "latency_ms": agent_run.latency_ms,
                "weight": case.weight,
                "run_id": str(agent_run.id),
            }

    @staticmethod
    def _score(case: EvalCase, response_lower: str, latency_ms: int) -> tuple[bool, list[str]]:
        """Return (passed, [failure_reasons])."""
        reasons = []

        for kw in (case.expected_keywords or []):
            if kw.lower() not in response_lower:
                reasons.append(f"Missing expected keyword: '{kw}'")

        for kw in (case.must_not_contain or []):
            if kw.lower() in response_lower:
                reasons.append(f"Response contains forbidden term: '{kw}'")

        if case.max_latency_ms and latency_ms > case.max_latency_ms:
            reasons.append(
                f"Latency {latency_ms}ms exceeds limit {case.max_latency_ms}ms"
            )

        return (len(reasons) == 0), reasons

    @staticmethod
    def _audit(run: EvalRun, agent, triggered_by: str) -> None:
        AuditLog.objects.create(
            actor=triggered_by,
            action="eval.run_complete",
            resource_type="EvalRun",
            resource_id=str(run.id),
            payload={
                "suite": run.suite.name,
                "agent": agent.name,
                "total_cases": run.total_cases,
                "passed_cases": run.passed_cases,
                "pass_rate": float(run.pass_rate),
                "passed": run.passed,
                "status": run.status,
            },
        )


# Module-level singleton
eval_service = EvalService()
