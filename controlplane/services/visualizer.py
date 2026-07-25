"""
Visualizer — Phase 4 step 2.

Builds the agent interaction graph MuleSoft's Agent Visualizer renders: nodes are
agents (with run metrics), edges are the hops between them — broker routes, inbound
A2A invocations, and agent-to-agent delegations — reconstructed from the audit log
and tool-call records the platform already keeps.  Per-node metrics approximate the
Visualizer's overlays (calls, latency, success, bottleneck, at-risk).
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

from django.db.models import Avg, Count, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

_BOTTLENECK_LATENCY_FACTOR = 1.5
_MIN_CALLS_FOR_FLAGS = 3
_AT_RISK_SUCCESS_PCT = 80.0


def build_graph(*, window_days: int = 30) -> dict:
    """Return {"nodes": [...], "edges": [...]} for the interaction map."""
    from controlplane.models import Agent, AgentRun, AgentToolCall, AuditLog

    since = timezone.now() - timedelta(days=window_days)

    # ── per-agent run metrics ────────────────────────────────────────────────
    metrics: dict = {}
    for row in (AgentRun.objects.filter(started_at__gte=since)
                .values("agent_id")
                .annotate(
                    calls=Count("id"),
                    avg_latency=Avg("latency_ms"),
                    completed=Count("id", filter=Q(status=AgentRun.Status.COMPLETED)),
                    failed=Count("id", filter=Q(status=AgentRun.Status.FAILED)),
                )):
        metrics[str(row["agent_id"])] = row

    latencies = [m["avg_latency"] or 0 for m in metrics.values()]
    global_avg = (sum(latencies) / len(latencies)) if latencies else 0

    # ── edges from audit + tool calls ────────────────────────────────────────
    edge_counts: Counter = Counter()  # (source, target, kind) -> n
    referenced_agent_ids: set = set(metrics.keys())

    for a in AuditLog.objects.filter(action="broker_route", created_at__gte=since):
        tgt = a.resource_id
        if tgt and tgt != "none":
            edge_counts[("broker", tgt, "broker")] += 1
            referenced_agent_ids.add(tgt)

    for a in AuditLog.objects.filter(action="a2a_inbound_invoke", created_at__gte=since):
        if a.resource_id:
            edge_counts[("external", a.resource_id, "a2a")] += 1
            referenced_agent_ids.add(a.resource_id)

    deleg = (AgentToolCall.objects
             .filter(tool_name="delegate_to_agent", created_at__gte=since)
             .select_related("run"))
    slug_targets = []
    for tc in deleg:
        src = str(tc.run.agent_id) if tc.run_id and tc.run.agent_id else None
        slug = (tc.input_payload or {}).get("agent_slug")
        if src and slug:
            slug_targets.append((src, slug))
    slug_map = {}
    if slug_targets:
        slugs = {s for _, s in slug_targets}
        slug_map = {sl: str(aid) for sl, aid in
                    Agent.objects.filter(slug__in=slugs).values_list("slug", "id")}
    for src, slug in slug_targets:
        tgt = slug_map.get(slug)
        if tgt:
            edge_counts[(src, tgt, "delegation")] += 1
            referenced_agent_ids.update({src, tgt})

    # ── nodes ────────────────────────────────────────────────────────────────
    names = dict(Agent.objects.filter(id__in=referenced_agent_ids).values_list("id", "name"))
    names = {str(k): v for k, v in names.items()}

    nodes = []
    for aid in referenced_agent_ids:
        m = metrics.get(aid, {})
        calls = m.get("calls", 0) or 0
        avg_lat = round(m.get("avg_latency") or 0, 1)
        success = round((m["completed"] / calls * 100), 1) if calls else 0.0
        nodes.append({
            "id": aid,
            "label": names.get(aid, aid),
            "type": "agent",
            "metrics": {
                "calls": calls,
                "avg_latency_ms": avg_lat,
                "success_rate": success,
                "failed": m.get("failed", 0) or 0,
                "bottleneck": bool(global_avg and avg_lat > global_avg * _BOTTLENECK_LATENCY_FACTOR
                                   and calls >= _MIN_CALLS_FOR_FLAGS),
                "at_risk": bool(calls >= _MIN_CALLS_FOR_FLAGS and success < _AT_RISK_SUCCESS_PCT),
            },
        })

    # virtual nodes for non-agent edge sources
    for vid, label, vtype in (("broker", "Broker", "broker"), ("external", "External A2A", "external")):
        if any(s == vid for (s, _t, _k) in edge_counts):
            nodes.append({"id": vid, "label": label, "type": vtype, "metrics": {}})

    edges = [
        {"source": s, "target": t, "kind": k, "count": n}
        for (s, t, k), n in edge_counts.items()
    ]

    return {"nodes": nodes, "edges": edges,
            "window_days": window_days, "node_count": len(nodes), "edge_count": len(edges)}
