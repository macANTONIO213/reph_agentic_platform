"""
Prometheus-compatible text metrics scraper — Phase D Observability

Produces a plain-text Prometheus exposition format response for /api/v1/metrics/.
Uses only Django ORM queries — no prometheus_client dependency required.

Metrics exposed:
  relx_agent_runs_total{agent, status}           — run counts by agent + status
  relx_agent_latency_p50_ms{agent}               — median latency (last 24 h)
  relx_agent_latency_p95_ms{agent}               — 95th percentile latency (last 24 h)
  relx_agent_cost_usd_month{agent}               — month-to-date spend
  relx_agent_budget_usd_monthly{agent}           — configured monthly budget (0 = no cap)
  relx_agent_budget_alert{agent}                 — 1 if budget breached
  relx_agent_quality_alert{agent}                — 1 if quality drift detected
  relx_agent_spans_total{agent}                  — total OTel spans stored
  relx_platform_agents_total{status}             — agent count by lifecycle status
  relx_platform_runs_24h                         — total runs in last 24 h
  relx_platform_cost_usd_24h                     — total cost in last 24 h
"""
from __future__ import annotations

import statistics
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone
from datetime import timedelta


def _label(name: str) -> str:
    """Sanitize a label value for Prometheus."""
    return name.replace('"', "'").replace("\n", " ").replace("\\", "/")


class MetricsRenderer:
    """Collects metrics from the DB and renders Prometheus text format."""

    def render(self) -> str:
        lines: list[str] = []
        lines.append("# HELP relx_agent_platform_info Platform identity gauge")
        lines.append("# TYPE relx_agent_platform_info gauge")
        lines.append('relx_agent_platform_info{version="1.0",phase="D"} 1')
        lines.append("")

        self._platform_totals(lines)
        self._per_agent_metrics(lines)

        return "\n".join(lines) + "\n"

    # ── Platform-wide ─────────────────────────────────────────────────────────

    def _platform_totals(self, lines: list[str]) -> None:
        from controlplane.models import Agent, AgentRun

        now = timezone.now()
        window_24h = now - timedelta(hours=24)

        # Agent counts by status
        lines.append("# HELP relx_platform_agents_total Agent count by lifecycle status")
        lines.append("# TYPE relx_platform_agents_total gauge")
        for row in Agent.objects.values("status").annotate(n=Count("id")):
            lines.append(
                f'relx_platform_agents_total{{status="{_label(row["status"])}"}} {row["n"]}'
            )
        lines.append("")

        # Runs last 24 h
        runs_24h = AgentRun.objects.filter(started_at__gte=window_24h).count()
        lines.append("# HELP relx_platform_runs_24h Total agent runs in the last 24 hours")
        lines.append("# TYPE relx_platform_runs_24h gauge")
        lines.append(f"relx_platform_runs_24h {runs_24h}")
        lines.append("")

        # Cost last 24 h
        cost_24h = AgentRun.objects.filter(
            started_at__gte=window_24h, status="completed"
        ).aggregate(total=Sum("cost_usd"))["total"] or Decimal("0")
        lines.append("# HELP relx_platform_cost_usd_24h Total LLM cost (USD) in the last 24 hours")
        lines.append("# TYPE relx_platform_cost_usd_24h gauge")
        lines.append(f"relx_platform_cost_usd_24h {float(cost_24h):.6f}")
        lines.append("")

    # ── Per-agent ─────────────────────────────────────────────────────────────

    def _per_agent_metrics(self, lines: list[str]) -> None:
        from controlplane.models import Agent, AgentRun, OtelSpan

        now = timezone.now()
        window_24h = now - timedelta(hours=24)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        agents = Agent.objects.all().order_by("slug")

        # ── Run counts ──────────────────────────────────────────────────────
        lines.append("# HELP relx_agent_runs_total Cumulative run count by agent and status")
        lines.append("# TYPE relx_agent_runs_total counter")
        run_counts = (
            AgentRun.objects.values("agent__slug", "status").annotate(n=Count("id"))
        )
        for row in run_counts:
            slug = _label(row["agent__slug"] or "unknown")
            lines.append(
                f'relx_agent_runs_total{{agent="{slug}",status="{row["status"]}"}} {row["n"]}'
            )
        lines.append("")

        # ── Latency percentiles (last 24 h) ──────────────────────────────────
        lines.append("# HELP relx_agent_latency_p50_ms Median run latency (ms) in last 24 h")
        lines.append("# TYPE relx_agent_latency_p50_ms gauge")
        lines.append("# HELP relx_agent_latency_p95_ms p95 run latency (ms) in last 24 h")
        lines.append("# TYPE relx_agent_latency_p95_ms gauge")

        latencies_qs = (
            AgentRun.objects
            .filter(started_at__gte=window_24h, status="completed", latency_ms__gt=0)
            .values("agent__slug", "latency_ms")
        )
        agent_latencies: dict[str, list[int]] = {}
        for row in latencies_qs:
            slug = row["agent__slug"] or "unknown"
            agent_latencies.setdefault(slug, []).append(row["latency_ms"])

        for slug, lats in agent_latencies.items():
            lats_sorted = sorted(lats)
            p50 = statistics.median(lats_sorted)
            idx95 = max(0, int(len(lats_sorted) * 0.95) - 1)
            p95 = lats_sorted[idx95]
            sl = _label(slug)
            lines.append(f'relx_agent_latency_p50_ms{{agent="{sl}"}} {p50:.1f}')
            lines.append(f'relx_agent_latency_p95_ms{{agent="{sl}"}} {p95:.1f}')
        lines.append("")

        # ── Month-to-date cost + budget ───────────────────────────────────────
        lines.append("# HELP relx_agent_cost_usd_month Month-to-date spend in USD")
        lines.append("# TYPE relx_agent_cost_usd_month gauge")
        lines.append("# HELP relx_agent_budget_usd_monthly Configured monthly budget cap (0 = none)")
        lines.append("# TYPE relx_agent_budget_usd_monthly gauge")
        lines.append("# HELP relx_agent_budget_alert 1 if agent has exceeded its monthly budget")
        lines.append("# TYPE relx_agent_budget_alert gauge")
        lines.append("# HELP relx_agent_quality_alert 1 if agent has a quality drift alert")
        lines.append("# TYPE relx_agent_quality_alert gauge")

        cost_by_agent = {
            row["agent_id"]: row["total"]
            for row in AgentRun.objects.filter(
                started_at__gte=month_start, status="completed"
            ).values("agent_id").annotate(total=Sum("cost_usd"))
        }

        for agent in agents:
            sl = _label(agent.slug)
            cost = float(cost_by_agent.get(agent.id, Decimal("0")))
            budget = float(agent.budget_usd_monthly) if agent.budget_usd_monthly else 0.0
            lines.append(f'relx_agent_cost_usd_month{{agent="{sl}"}} {cost:.6f}')
            lines.append(f'relx_agent_budget_usd_monthly{{agent="{sl}"}} {budget:.2f}')
            lines.append(f'relx_agent_budget_alert{{agent="{sl}"}} {1 if agent.budget_alert else 0}')
            lines.append(f'relx_agent_quality_alert{{agent="{sl}"}} {1 if agent.quality_alert else 0}')
        lines.append("")

        # ── OTel span counts ──────────────────────────────────────────────────
        lines.append("# HELP relx_agent_spans_total Total OTel spans stored by agent")
        lines.append("# TYPE relx_agent_spans_total counter")
        span_counts = OtelSpan.objects.values("agent__slug").annotate(n=Count("id"))
        for row in span_counts:
            sl = _label(row["agent__slug"] or "unknown")
            lines.append(f'relx_agent_spans_total{{agent="{sl}"}} {row["n"]}')
        lines.append("")


_renderer = MetricsRenderer()


def render_metrics() -> str:
    """Entry point called by the /api/v1/metrics/ view."""
    return _renderer.render()
