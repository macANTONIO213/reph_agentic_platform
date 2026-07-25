"""
Phase 4 step 1 tests — agent broker (governed intent → agent routing).

Selection is deterministic (keyword/domain/capability scoring over the federated
registry). route_and_execute uses a real embedded (Echo) agent so the test proves
the chosen agent runs through the governed runtime (an AgentRun is created) and the
broker hop is audited.
"""
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from controlplane.models import (
    Agent, AgentRun, AuditLog, BusinessUnit, RegistryEntry,
)
from controlplane.services.interop import broker
from controlplane.services.interop.a2a_cards import publish_card


def _bu(name="Engineering"):
    return BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:8].upper()})[0]


def _agent(slug, *, bu, purpose, tools, platform="embedded"):
    return Agent.objects.create(
        name=slug.replace("-", " ").title(), slug=slug, purpose=purpose,
        business_unit=bu.name, owner="o", technical_owner="o", system_prompt="s",
        platform=platform, status=Agent.Status.PILOT, risk_tier=1, org_unit=bu,
        tool_names=tools, guardrail_level="off",
    )


def _user(username, *, role=None, bu=None):
    u, _ = User.objects.get_or_create(username=username)
    if role or bu:
        p = u.profile
        if bu:
            p.business_unit = bu
        if role:
            p.role = role
        p.save()
    return u


def _external_entry(name, *, domain, caps):
    return RegistryEntry.objects.create(
        kind=RegistryEntry.Kind.EXTERNAL_A2A, identifier=name, name=name,
        description=f"{name} agent", protocol="a2a", domain=domain, capabilities=caps,
        review_status=RegistryEntry.ReviewStatus.APPROVED, is_active=True,
    )


class BrokerSelectionTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.finance = _agent("finance-bot", bu=self.bu,
                              purpose="handles finance forecasting", tools=["forecast"])
        self.hr = _agent("hr-bot", bu=self.bu,
                        purpose="handles time off and leave", tools=["pto"])
        publish_card(self.finance, base_url="https://p.test")   # auto-projects to registry
        publish_card(self.hr, base_url="https://p.test")

    def test_selects_best_by_capability(self):
        decision = broker.route("please run a revenue forecast")
        self.assertEqual(decision["decision"]["identifier"], "finance-bot")

    def test_domain_boosts_selection(self):
        _external_entry("Ext Finance", domain="finance", caps=[])
        decision = broker.route("anything", domain="finance")
        self.assertEqual(decision["decision"]["name"], "Ext Finance")

    def test_only_approved_entries_considered(self):
        # deactivate finance-bot's card → its entry drops out of routing
        from controlplane.services.interop.a2a_cards import unpublish_card
        unpublish_card(self.finance)
        decision = broker.route("forecast")
        names = {c["entry"]["identifier"] for c in decision["candidates"]}
        self.assertNotIn("finance-bot", names)


class BrokerExecuteTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.agent = _agent("finance-bot", bu=self.bu,
                            purpose="handles finance forecasting", tools=["forecast"])
        publish_card(self.agent, base_url="https://p.test")

    def test_routes_and_runs_through_runtime(self):
        result = broker.route_and_execute("run a finance forecast")
        self.assertTrue(result["routed"])
        self.assertEqual(result["agent"]["slug"], "finance-bot")
        self.assertEqual(result["state"], "completed")
        # governed runtime path: an AgentRun exists, and the hop is audited
        self.assertTrue(AgentRun.objects.filter(agent=self.agent).exists())
        self.assertTrue(AuditLog.objects.filter(action="broker_route", payload__routed=True).exists())

    def test_no_executable_agent_returns_decision(self):
        _external_entry("Ext Only", domain="", caps=[{"id": "x", "name": "x", "description": "y"}])
        # unpublish the only first-party agent so nothing executable matches
        from controlplane.services.interop.a2a_cards import unpublish_card
        unpublish_card(self.agent)
        result = broker.route_and_execute("do something")
        self.assertFalse(result["routed"])
        self.assertIn("No executable", result["reason"])


class BrokerApiTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.builder = _user("builder", role="agent_builder", bu=self.bu)
        self.viewer = _user("viewer", role="viewer", bu=self.bu)
        self.agent = _agent("finance-bot", bu=self.bu,
                            purpose="handles finance forecasting", tools=["forecast"])
        publish_card(self.agent, base_url="https://p.test")

    def test_route_endpoint(self):
        self.client.force_login(self.viewer)
        resp = self.client.post("/api/v1/broker/route/",
                                data=json.dumps({"intent": "forecast revenue"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["decision"]["identifier"], "finance-bot")

    def test_route_requires_intent(self):
        self.client.force_login(self.viewer)
        resp = self.client.post("/api/v1/broker/route/", data=json.dumps({}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_execute_requires_builder(self):
        self.client.force_login(self.viewer)
        resp = self.client.post("/api/v1/broker/execute/",
                                data=json.dumps({"intent": "forecast"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 403)

    def test_execute_runs_agent(self):
        self.client.force_login(self.builder)
        resp = self.client.post("/api/v1/broker/execute/",
                                data=json.dumps({"intent": "run a finance forecast"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["routed"])
        self.assertEqual(resp.json()["state"], "completed")


# ── live LLM router ──────────────────────────────────────────────────────────────

def _make_anthropic(json_text):
    """Build a fake anthropic.Anthropic class whose messages.create returns json_text."""
    class _Block:
        type = "text"
        def __init__(self, t):
            self.text = t

    class _Resp:
        def __init__(self, t):
            self.content = [_Block(t)]

    class _Msgs:
        def create(self, **kw):
            return _Resp(json_text)

    class _Client:
        def __init__(self, *a, **k):
            self.messages = _Msgs()

    return _Client


class BrokerLlmRouterTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.finance = _agent("finance-bot", bu=self.bu,
                              purpose="finance forecasting", tools=["forecast"])
        self.hr = _agent("hr-bot", bu=self.bu,
                        purpose="time off and leave", tools=["pto"])
        publish_card(self.finance, base_url="https://p.test")
        publish_card(self.hr, base_url="https://p.test")

    def _entries(self):
        return [e for e, _s in broker.select_candidates("anything")]

    @override_settings(ANTHROPIC_API_KEY="")
    def test_llm_rank_no_key_returns_none(self):
        self.assertIsNone(broker.llm_rank("x", self._entries()))

    @override_settings(ANTHROPIC_API_KEY="k")
    def test_llm_rank_parses_ranking(self):
        client = _make_anthropic('{"ranking":[1,0],"reasoning":"hr first","confidence":0.8}')
        with patch("anthropic.Anthropic", client):
            res = broker.llm_rank("help with leave", self._entries())
        self.assertIsNotNone(res)
        order, reasoning, conf = res
        self.assertEqual(order, [1, 0])
        self.assertEqual(conf, 0.8)

    @override_settings(ANTHROPIC_API_KEY="k")
    def test_llm_rank_malformed_returns_none(self):
        with patch("anthropic.Anthropic", _make_anthropic("not json at all")):
            self.assertIsNone(broker.llm_rank("x", self._entries()))

    @override_settings(BROKER_ROUTER_MODE="llm")
    def test_route_uses_llm_reorder(self):
        with patch("controlplane.services.interop.broker.llm_rank",
                   return_value=([1, 0], "because", 0.7)):
            r = broker.route("something that matches both weakly")
        self.assertEqual(r["method"], "llm")
        self.assertEqual(r["confidence"], 0.7)
        self.assertEqual(r["reasoning"], "because")

    @override_settings(BROKER_ROUTER_MODE="llm", ANTHROPIC_API_KEY="")
    def test_route_falls_back_deterministic(self):
        r = broker.route("forecast")  # llm_rank returns None (no key)
        self.assertEqual(r["method"], "deterministic")

    @override_settings(BROKER_ROUTER_MODE="deterministic")
    def test_deterministic_mode_never_calls_llm(self):
        with patch("controlplane.services.interop.broker.llm_rank") as m:
            broker.route("forecast")
        m.assert_not_called()

    @override_settings(BROKER_ROUTER_MODE="llm")
    def test_route_and_execute_respects_llm(self):
        cands = broker.select_candidates("generic")
        hr_idx = next(i for i, (e, _s) in enumerate(cands) if e.identifier == "hr-bot")
        order = [hr_idx] + [i for i in range(len(cands)) if i != hr_idx]
        with patch("controlplane.services.interop.broker.llm_rank",
                   return_value=(order, "hr best", 0.9)):
            res = broker.route_and_execute("generic")
        self.assertTrue(res["routed"])
        self.assertEqual(res["agent"]["slug"], "hr-bot")
        self.assertEqual(res["method"], "llm")
