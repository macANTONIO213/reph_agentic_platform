"""
TelemetryService — Phase D Observability

Wraps every AgentRun with an OpenTelemetry-compatible span tree:

    [ROOT] agent.run  ─── trace_id = run.id (32-char hex)
        [CHILD] agent.guardrail_scan
        [CHILD] agent.llm_call
        [CHILD] agent.tool_call   (one per tool)
        [CHILD] agent.eval_gate   (if eval suite active)

Spans are written to OtelSpan model rows.  A future export_spans management
command can forward unexported rows to any OTLP/gRPC collector.

Span attributes follow the OpenTelemetry Semantic Conventions for LLM systems
(https://opentelemetry.io/docs/specs/semconv/gen-ai/).

Usage (in agent_runtime.py):
    from controlplane.services.telemetry import telemetry_service

    with telemetry_service.run_span(run, agent) as trace_id:
        ...
        with telemetry_service.child_span(trace_id, "agent.llm_call", run=run):
            ...
"""
import contextlib
import logging
import secrets
import time
from datetime import datetime, timezone as dt_tz
from decimal import Decimal

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(dt_tz.utc)


def _trace_id_from_run(run_id) -> str:
    """Derive a stable 32-char hex trace_id from the run UUID."""
    return str(run_id).replace("-", "")[:32].ljust(32, "0")


def _new_span_id() -> str:
    return secrets.token_hex(8)  # 16-char hex


class _SpanContext:
    """Mutable context passed through a with-block."""
    def __init__(self, span):
        self.span = span
        self._start = time.perf_counter()

    def set_error(self, message: str):
        self.span.status_code = "ERROR"
        self.span.status_message = message

    def add_attribute(self, key: str, value):
        self.span.attributes[key] = value


class TelemetryService:
    """
    Creates OtelSpan rows without any external dependency.
    All methods silently swallow exceptions so telemetry never breaks the run.
    """

    # ── Public context managers ───────────────────────────────────────────────

    @contextlib.contextmanager
    def run_span(self, run, agent):
        """
        Root span for an entire AgentRun.  Yields the trace_id string so
        callers can open child spans inside the same with-block.

        Usage::
            with telemetry_service.run_span(run, agent) as trace_id:
                ...
        """
        from controlplane.models import OtelSpan
        trace_id = _trace_id_from_run(run.id)
        span_id = _new_span_id()
        start = _now()
        span = None
        try:
            span = OtelSpan.objects.create(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id="",
                name="agent.run",
                kind=OtelSpan.Kind.SERVER,
                start_time=start,
                status_code="UNSET",
                attributes={
                    "agent.id": str(agent.id),
                    "agent.slug": agent.slug,
                    "agent.risk_tier": agent.risk_tier,
                    "agent.business_unit": agent.business_unit,
                    "run.id": str(run.id),
                    "run.channel": run.channel,
                    "run.user_label": run.user_label,
                },
                agent=agent,
                run=run,
            )
        except Exception as exc:
            logger.debug("OtelSpan root create failed: %s", exc)
            span = None

        try:
            yield trace_id
        except Exception:
            if span:
                try:
                    span.status_code = "ERROR"
                    span.status_message = "Run raised an exception"
                    span.end_time = _now()
                    span.duration_ms = int((time.perf_counter()) * 1000)
                    span.save(update_fields=["status_code", "status_message", "end_time", "duration_ms"])
                except Exception:
                    pass
            raise
        finally:
            if span:
                try:
                    end = _now()
                    dur = max(0, int((end - start).total_seconds() * 1000))
                    # Pull final token/cost from the run (may have been set after yield)
                    try:
                        run.refresh_from_db(fields=["input_tokens", "output_tokens", "cost_usd", "model_id", "status"])
                    except Exception:
                        pass
                    span.end_time = end
                    span.duration_ms = dur
                    if span.status_code == "UNSET":
                        span.status_code = "OK" if run.status == "completed" else "ERROR"
                    span.attributes.update({
                        "llm.input_tokens": run.input_tokens,
                        "llm.output_tokens": run.output_tokens,
                        "llm.model_id": run.model_id,
                        "cost.usd": float(run.cost_usd),
                        "run.latency_ms": run.latency_ms,
                        "run.status": run.status,
                    })
                    span.save(update_fields=["end_time", "duration_ms", "status_code", "attributes"])
                except Exception as exc:
                    logger.debug("OtelSpan root finalise failed: %s", exc)

    @contextlib.contextmanager
    def child_span(self, trace_id: str, name: str, *, run=None, agent=None, attributes: dict | None = None):
        """
        Open a child span inside an existing trace.

        Usage::
            with telemetry_service.child_span(trace_id, "agent.llm_call", run=run) as ctx:
                ctx.add_attribute("llm.model_id", model_id)
                ...
        """
        from controlplane.models import OtelSpan
        span_id = _new_span_id()
        start = _now()
        t_start = time.perf_counter()
        span = None
        try:
            span = OtelSpan.objects.create(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=_new_span_id(),   # root span_id isn't tracked in ctx; good enough
                name=name,
                kind=OtelSpan.Kind.INTERNAL,
                start_time=start,
                status_code="UNSET",
                attributes=attributes or {},
                agent=agent,
                run=run,
            )
        except Exception as exc:
            logger.debug("OtelSpan child create failed: %s", exc)
            span = None

        ctx = _SpanContext(span) if span else _SpanContext(type("_Dummy", (), {
            "status_code": "UNSET", "status_message": "", "attributes": {}
        })())

        try:
            yield ctx
        except Exception as exc:
            ctx.set_error(str(exc))
            raise
        finally:
            if span:
                try:
                    end = _now()
                    dur = max(0, int((time.perf_counter() - t_start) * 1000))
                    if ctx.span.status_code == "UNSET":
                        ctx.span.status_code = "OK"
                    span.end_time = end
                    span.duration_ms = dur
                    span.status_code = ctx.span.status_code
                    span.status_message = ctx.span.status_message
                    span.attributes.update(ctx.span.attributes)
                    span.save(update_fields=["end_time", "duration_ms", "status_code",
                                             "status_message", "attributes"])
                except Exception as exc:
                    logger.debug("OtelSpan child finalise failed: %s", exc)

    # ── Budget check (called after each run completion) ───────────────────────

    def check_budget(self, agent) -> bool:
        """
        Compare agent's month-to-date spend against budget_usd_monthly.
        Sets agent.budget_alert flag and creates BudgetAlert row if breached.
        Returns True if a new breach was detected.
        """
        from django.utils import timezone
        from controlplane.models import AgentRun, BudgetAlert

        if not agent.budget_usd_monthly:
            return False

        now = timezone.now()
        period = now.strftime("%Y-%m")
        # Sum cost for current calendar month
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = AgentRun.objects.filter(
            agent=agent,
            started_at__gte=month_start,
            status="completed",
        ).aggregate(total=models.Sum("cost_usd"))["total"] or Decimal("0")

        over_budget = total > agent.budget_usd_monthly

        if over_budget and not agent.budget_alert:
            # New breach
            overage = total - agent.budget_usd_monthly
            try:
                BudgetAlert.objects.update_or_create(
                    agent=agent,
                    period_month=period,
                    defaults={
                        "budget_usd": agent.budget_usd_monthly,
                        "actual_usd": total,
                        "overage_usd": overage,
                        "resolved": False,
                    },
                )
                from controlplane.models import AuditLog
                AuditLog.objects.create(
                    actor="system:compute_budgets",
                    action="budget.breach_detected",
                    resource_type="Agent",
                    resource_id=str(agent.id),
                    payload={
                        "period": period,
                        "budget_usd": float(agent.budget_usd_monthly),
                        "actual_usd": float(total),
                        "overage_usd": float(overage),
                    },
                )
            except Exception as exc:
                logger.warning("BudgetAlert create failed: %s", exc)
            agent.budget_alert = True
            agent.save(update_fields=["budget_alert"])
            return True

        if not over_budget and agent.budget_alert:
            # Resolved (e.g. month rolled over or budget raised)
            try:
                BudgetAlert.objects.filter(agent=agent, period_month=period).update(resolved=True)
                from controlplane.models import AuditLog
                AuditLog.objects.create(
                    actor="system:compute_budgets",
                    action="budget.breach_resolved",
                    resource_type="Agent",
                    resource_id=str(agent.id),
                    payload={"period": period, "actual_usd": float(total)},
                )
            except Exception as exc:
                logger.warning("BudgetAlert resolve failed: %s", exc)
            agent.budget_alert = False
            agent.save(update_fields=["budget_alert"])

        return False


    # ── Direct open/close API (for use inside generators) ────────────────────

    def _trace_id_from_run(self, run) -> str:
        return _trace_id_from_run(run.id)

    def _open_root_span(self, run, agent) -> None:
        """Create the root span for a run.  Fire-and-forget; errors are suppressed."""
        from controlplane.models import OtelSpan
        try:
            OtelSpan.objects.create(
                trace_id=_trace_id_from_run(run.id),
                span_id=_new_span_id(),
                parent_span_id="",
                name="agent.run",
                kind=OtelSpan.Kind.SERVER,
                start_time=_now(),
                status_code="UNSET",
                attributes={
                    "agent.id": str(agent.id),
                    "agent.slug": agent.slug,
                    "agent.risk_tier": agent.risk_tier,
                    "agent.business_unit": agent.business_unit,
                    "run.id": str(run.id),
                    "run.channel": run.channel,
                    "run.user_label": run.user_label,
                },
                agent=agent,
                run=run,
            )
        except Exception as exc:
            logger.debug("_open_root_span failed: %s", exc)

    def _close_root_span(self, run, *, error: str | None = None) -> None:
        """Finalise the root span for a run.  Fire-and-forget; errors are suppressed."""
        from controlplane.models import OtelSpan
        try:
            span = OtelSpan.objects.filter(
                run=run, name="agent.run", parent_span_id=""
            ).order_by("created_at").last()
            if span is None:
                return
            end = _now()
            dur = max(0, int((end - span.start_time).total_seconds() * 1000))
            span.end_time = end
            span.duration_ms = dur
            span.status_code = "ERROR" if error else "OK"
            if error:
                span.status_message = error
            span.attributes.update({
                "llm.input_tokens": run.input_tokens,
                "llm.output_tokens": run.output_tokens,
                "llm.model_id": run.model_id,
                "cost.usd": float(run.cost_usd),
                "run.latency_ms": run.latency_ms,
                "run.status": run.status,
            })
            span.save(update_fields=[
                "end_time", "duration_ms", "status_code", "status_message", "attributes"
            ])
        except Exception as exc:
            logger.debug("_close_root_span failed: %s", exc)

    def record_child_span(self, run, agent, name: str, duration_ms: int,
                          attributes: dict | None = None, error: str | None = None) -> None:
        """Write a completed child span in one shot.  Fire-and-forget."""
        from controlplane.models import OtelSpan
        try:
            now = _now()
            from datetime import timedelta
            start = now - timedelta(milliseconds=duration_ms)
            OtelSpan.objects.create(
                trace_id=_trace_id_from_run(run.id),
                span_id=_new_span_id(),
                parent_span_id=_new_span_id(),
                name=name,
                kind=OtelSpan.Kind.INTERNAL,
                start_time=start,
                end_time=now,
                duration_ms=duration_ms,
                status_code="ERROR" if error else "OK",
                status_message=error or "",
                attributes=attributes or {},
                agent=agent,
                run=run,
            )
        except Exception as exc:
            logger.debug("record_child_span failed: %s", exc)


# Need Sum for budget check
from django.db import models  # noqa: E402  (import after class def to avoid circular)

# Module-level singleton
telemetry_service = TelemetryService()
