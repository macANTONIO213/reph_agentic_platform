"""
Phase 4 step 2 tests — visualizer (agent interaction graph).

Builds real runs + edge records (broker route, A2A invoke, delegation) and asserts
the graph's nodes/metrics/edges.
"""
import json

from django.contrib.auth.models import User
from django.test import TestCase

from controlplane.models import (
    Agent, AgentRun, AgentToolCall, AuditLog, BusinessUnit,
)
from controlplane.services.visualizer import build_graph


def _bu(name="Engineering"):
    return BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:8].upper()})[0]


def _agent(slug, bu):
    return Agent.objects.create(
        name=slug.replace("-", " ").title(), slug=slug, purpose="p",
        business_unit=bu.name, owner="o", technical_owner="o", system_prompt="s",
        platform="embedded", status=Agent.Status.PILOT, risk_tier=1, org_unit=bu,
    )


def _run(agent, *, status=AgentRun.Status.COMPLETED, latency=100):
    return AgentRun.objects.create(
        agent=agent, input_text="x", output_text="y", status=status, latency_ms=latency,
    )


def _user(username):
    return User.objects.get_or_create(username=username)[0]


class BuildGraphTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.a = _agent("agent-a", self.bu)
        self.b = _agent("agent-b", self.bu)
        for _ in range(4):
            _run(self.a)
        _run(self.a, status=AgentRun.Status.FAILED)
        _run(self.b, latency=100)

    def test_nodes_have_metrics(self):
        graph = build_graph()
        node = next(n for n in graph["nodes"] if n["id"] == str(self.a.id))
        self.assertEqual(node["type"], "agent")
        self.assertEqual(node["metrics"]["calls"], 5)
        self.assertEqual(node["metrics"]["failed"], 1)
        self.assertEqual(node["metrics"]["success_rate"], 80.0)

    def test_broker_edge(self):
        AuditLog.objects.create(actor="broker:u", action="broker_route",
                                resource_type="Agent", resource_id=str(self.a.id),
                                payload={"routed": True})
        graph = build_graph()
        self.assertTrue(any(e["source"] == "broker" and e["target"] == str(self.a.id)
                            and e["kind"] == "broker" for e in graph["edges"]))
        self.assertTrue(any(n["id"] == "broker" and n["type"] == "broker" for n in graph["nodes"]))

    def test_a2a_edge(self):
        AuditLog.objects.create(actor="a2a:x", action="a2a_inbound_invoke",
                                resource_type="Agent", resource_id=str(self.b.id), payload={})
        graph = build_graph()
        self.assertTrue(any(e["source"] == "external" and e["target"] == str(self.b.id)
                            and e["kind"] == "a2a" for e in graph["edges"]))

    def test_delegation_edge(self):
        run = _run(self.a)
        AgentToolCall.objects.create(run=run, tool_name="delegate_to_agent",
                                     input_payload={"agent_slug": self.b.slug})
        graph = build_graph()
        self.assertTrue(any(e["source"] == str(self.a.id) and e["target"] == str(self.b.id)
                            and e["kind"] == "delegation" for e in graph["edges"]))

    def test_broker_route_none_ignored(self):
        AuditLog.objects.create(actor="broker:u", action="broker_route",
                                resource_type="RegistryEntry", resource_id="none",
                                payload={"routed": False})
        graph = build_graph()
        self.assertFalse(any(e["target"] == "none" for e in graph["edges"]))


class VisualizerApiTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.a = _agent("agent-a", self.bu)
        _run(self.a)

    def test_graph_endpoint(self):
        self.client.force_login(_user("u1"))
        resp = self.client.get("/api/v1/visualizer/graph/?window=30")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("nodes", data)
        self.assertIn("edges", data)
        self.assertEqual(data["window_days"], 30)

    def test_requires_login(self):
        resp = self.client.get("/api/v1/visualizer/graph/")
        self.assertIn(resp.status_code, (302, 401, 403))
