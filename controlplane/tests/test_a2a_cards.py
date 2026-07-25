"""
Phase 1 step 4 tests — A2A card projection, discovery surface, publish control.

Covers the outbound-discoverability guarantees: only pilot/production agents can
be published, only published cards are discoverable, the /a2a/ surface is off by
default and auth-gated when on, and the card carries the x-governance block.
"""
import json

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from controlplane.models import Agent, AgentCard, BusinessUnit
from controlplane.services.interop.a2a_cards import (
    CardPublishError, build_card, publish_card, unpublish_card,
)


def _bu(name="Engineering"):
    return BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:8].upper()})[0]


def _agent(slug="card-agent", bu=None, status=Agent.Status.PILOT, tier=2, tools=None):
    return Agent.objects.create(
        name=slug.replace("-", " ").title(), slug=slug, purpose="Answer questions",
        business_unit=(bu.name if bu else "Engineering"), owner="o", technical_owner="o",
        system_prompt="s", platform="django_runtime", status=status, risk_tier=tier,
        org_unit=bu, tool_names=tools or [],
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


# ── projection ──────────────────────────────────────────────────────────────────

class BuildCardTests(TestCase):
    def test_card_shape_and_governance_block(self):
        agent = _agent(tools=["registry_search", "retrieve_knowledge"])
        card = build_card(agent, base_url="https://platform.test/")
        self.assertEqual(card["name"], agent.name)
        self.assertEqual(card["url"], "https://platform.test/a2a/agents/card-agent/rpc/")
        self.assertTrue(card["capabilities"]["streaming"])
        self.assertEqual({s["id"] for s in card["skills"]}, {"registry_search", "retrieve_knowledge"})
        gov = card["x-governance"]
        self.assertEqual(gov["risk_tier"], 2)
        self.assertIn("guardrail_level", gov)
        self.assertEqual(gov["status"], Agent.Status.PILOT)


class PublishTests(TestCase):
    def test_publish_pilot_agent(self):
        agent = _agent()
        card = publish_card(agent, base_url="https://platform.test")
        self.assertTrue(card.is_published)
        self.assertEqual(card.version, agent.version)
        self.assertIsNotNone(card.published_at)

    def test_publish_draft_rejected(self):
        agent = _agent(status=Agent.Status.DRAFT)
        with self.assertRaises(CardPublishError):
            publish_card(agent)

    def test_unpublish_idempotent(self):
        agent = _agent()
        publish_card(agent)
        unpublish_card(agent)
        unpublish_card(agent)  # no error second time
        self.assertFalse(AgentCard.objects.get(agent=agent).is_published)


# ── external /a2a/ surface ───────────────────────────────────────────────────────

@override_settings(A2A_SERVER_ENABLED=True, A2A_ACCESS_TOKENS=["tok-123"])
class A2ASurfaceTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.agent = _agent(bu=self.bu, tools=["registry_search"])
        publish_card(self.agent, base_url="https://platform.test")
        self.unpublished = _agent(slug="hidden-agent", bu=self.bu)  # no card

    def test_discovery_lists_only_published(self):
        resp = self.client.get("/a2a/agents/", HTTP_AUTHORIZATION="Bearer tok-123")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["agents"][0]["name"], self.agent.name)

    def test_agent_card_returned(self):
        resp = self.client.get(f"/a2a/agents/{self.agent.slug}/card/", HTTP_AUTHORIZATION="Bearer tok-123")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("x-governance", resp.json())

    def test_unpublished_agent_card_404(self):
        resp = self.client.get(f"/a2a/agents/{self.unpublished.slug}/card/", HTTP_AUTHORIZATION="Bearer tok-123")
        self.assertEqual(resp.status_code, 404)

    def test_unauthorized_without_token(self):
        resp = self.client.get("/a2a/agents/")
        self.assertEqual(resp.status_code, 401)

    # NB: the /a2a/…/rpc/ invocation path is covered end-to-end in test_a2a_invoke.


class A2ASurfaceDisabledTests(TestCase):
    @override_settings(A2A_SERVER_ENABLED=False)
    def test_surface_off_returns_404(self):
        resp = self.client.get("/a2a/agents/", HTTP_AUTHORIZATION="Bearer anything")
        self.assertEqual(resp.status_code, 404)


# ── control-plane preview / publish API ──────────────────────────────────────────

class CardControlPlaneApiTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.agent = _agent(bu=self.bu, tools=["registry_search"])
        self.builder = _user("builder", role="agent_builder", bu=self.bu)
        self.approver = _user("approver", role="agent_approver", bu=self.bu)

    def test_preview_returns_card(self):
        self.client.force_login(self.builder)
        resp = self.client.get(f"/api/v1/agents/{self.agent.id}/a2a-card/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("x-governance", data["card"])
        self.assertFalse(data["is_published"])
        self.assertTrue(data["publishable"])

    def test_publish_requires_approver(self):
        self.client.force_login(self.builder)  # builder lacks approver role
        resp = self.client.post(f"/api/v1/agents/{self.agent.id}/a2a-card/publish/",
                                data=json.dumps({"publish": True}), content_type="application/json")
        self.assertEqual(resp.status_code, 403)

    def test_approver_can_publish_and_unpublish(self):
        self.client.force_login(self.approver)
        pub = self.client.post(f"/api/v1/agents/{self.agent.id}/a2a-card/publish/",
                               data=json.dumps({"publish": True}), content_type="application/json")
        self.assertEqual(pub.status_code, 200)
        self.assertTrue(AgentCard.objects.get(agent=self.agent).is_published)
        unp = self.client.post(f"/api/v1/agents/{self.agent.id}/a2a-card/publish/",
                               data=json.dumps({"publish": False}), content_type="application/json")
        self.assertEqual(unp.status_code, 200)
        self.assertFalse(AgentCard.objects.get(agent=self.agent).is_published)

    def test_publish_draft_agent_400(self):
        draft = _agent(slug="draft-a", bu=self.bu, status=Agent.Status.DRAFT)
        self.client.force_login(self.approver)
        resp = self.client.post(f"/api/v1/agents/{draft.id}/a2a-card/publish/",
                                data=json.dumps({"publish": True}), content_type="application/json")
        self.assertEqual(resp.status_code, 400)
