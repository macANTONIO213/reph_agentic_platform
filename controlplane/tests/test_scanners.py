"""
Phase 3 step 1 tests — scanners (auto-discovery) + review gate.

The cloud client is faked (injected), so these run offline. Covers Bedrock
normalisation, cataloging as governed 'discovered' entries, the discovered→approved
gate (discovered hidden from discovery until approved), idempotent re-scan, and the
scan/approve API.
"""
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from controlplane.models import BusinessUnit, RegistryEntry
from controlplane.services.interop import federation
from controlplane.services.scanners import service as scanner_service
from controlplane.services.scanners.agentforce import AgentforceScanner
from controlplane.services.scanners.bedrock import BedrockScanner
from controlplane.services.scanners.vertex import VertexScanner


AGENTS = [
    {"agentId": "AG1", "agentName": "Support Bot", "description": "help desk",
     "foundationModel": "anthropic.claude-v2", "agentStatus": "PREPARED"},
    {"agentName": "no id — skipped"},
]


class _FakeBedrock:
    def __init__(self, agents):
        self._agents = agents

    def list_agents(self):
        return {"agentSummaries": self._agents}


VERTEX_ENGINES = [
    {"name": "projects/p/locations/l/reasoningEngines/RE1", "displayName": "Vertex Planner",
     "description": "plans things",
     "spec": {"model": "gemini-1.5-pro",
              "classMethods": [{"name": "plan", "description": "make a plan"}]}},
    {"description": "no name — skipped"},
]

AGENTFORCE_AGENTS = [
    {"id": "BOT1", "name": "Service Agent", "description": "helps customers",
     "model": "einstein-gpt", "topics": [{"name": "orders", "description": "order help"}]},
    {"name": "no id — skipped"},
]


class _FakeVertex:
    def list_reasoning_engines(self):
        return {"reasoningEngines": VERTEX_ENGINES}


class _FakeAgentforce:
    def list_agents(self):
        return {"agents": AGENTFORCE_AGENTS}


def _bu(name="Engineering"):
    return BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:8].upper()})[0]


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


# ── scanner + catalog ────────────────────────────────────────────────────────────

class BedrockScannerTests(TestCase):
    def test_scan_normalises(self):
        agents = BedrockScanner(client=_FakeBedrock(AGENTS)).scan()
        self.assertEqual(len(agents), 1)
        a = agents[0]
        self.assertEqual(a.external_id, "AG1")
        self.assertEqual(a.name, "Support Bot")
        self.assertEqual(a.model, "anthropic.claude-v2")
        self.assertEqual(a.platform, "bedrock")

    def test_run_scan_catalogs_as_discovered(self):
        result = scanner_service.run_scan("bedrock", client=_FakeBedrock(AGENTS))
        self.assertEqual(result["discovered"], 1)
        entry = RegistryEntry.objects.get(identifier="bedrock:AG1")
        self.assertEqual(entry.kind, RegistryEntry.Kind.EXTERNAL_A2A)
        self.assertEqual(entry.source, "scanner")
        self.assertEqual(entry.review_status, RegistryEntry.ReviewStatus.DISCOVERED)
        self.assertEqual(entry.governance["model"], "anthropic.claude-v2")

    def test_discovered_hidden_from_discovery(self):
        scanner_service.run_scan("bedrock", client=_FakeBedrock(AGENTS))
        # default search excludes 'discovered'
        self.assertEqual(federation.search_entries(), [])
        # explicit review_status surfaces it
        self.assertEqual(len(federation.search_entries(review_status="discovered")), 1)

    def test_approve_surfaces_entry(self):
        scanner_service.run_scan("bedrock", client=_FakeBedrock(AGENTS))
        entry = RegistryEntry.objects.get(identifier="bedrock:AG1")
        federation.set_review_status(entry, "approved")
        self.assertEqual(len(federation.search_entries()), 1)

    def test_rescan_preserves_approval(self):
        scanner_service.run_scan("bedrock", client=_FakeBedrock(AGENTS))
        entry = RegistryEntry.objects.get(identifier="bedrock:AG1")
        federation.set_review_status(entry, "approved")
        scanner_service.run_scan("bedrock", client=_FakeBedrock(AGENTS))  # re-scan
        entry.refresh_from_db()
        self.assertEqual(entry.review_status, RegistryEntry.ReviewStatus.APPROVED)

    def test_unknown_platform_raises(self):
        from controlplane.services.scanners.base import ScannerError
        with self.assertRaises(ScannerError):
            scanner_service.run_scan("nope")


# ── Vertex + Agentforce scanners ────────────────────────────────────────────────

class VertexScannerTests(TestCase):
    def test_scan_normalises(self):
        agents = VertexScanner(client=_FakeVertex()).scan()
        self.assertEqual(len(agents), 1)
        a = agents[0]
        self.assertEqual(a.external_id, "RE1")
        self.assertEqual(a.name, "Vertex Planner")
        self.assertEqual(a.model, "gemini-1.5-pro")
        self.assertEqual(a.platform, "vertex")
        self.assertEqual([c["id"] for c in a.capabilities], ["plan"])

    def test_run_scan_catalogs_discovered(self):
        result = scanner_service.run_scan("vertex", client=_FakeVertex())
        self.assertEqual(result["discovered"], 1)
        e = RegistryEntry.objects.get(identifier="vertex:RE1")
        self.assertEqual(e.source, "scanner")
        self.assertEqual(e.review_status, RegistryEntry.ReviewStatus.DISCOVERED)
        self.assertEqual(e.governance["model"], "gemini-1.5-pro")


class AgentforceScannerTests(TestCase):
    def test_scan_normalises(self):
        agents = AgentforceScanner(client=_FakeAgentforce()).scan()
        self.assertEqual(len(agents), 1)
        a = agents[0]
        self.assertEqual(a.external_id, "BOT1")
        self.assertEqual(a.name, "Service Agent")
        self.assertEqual(a.platform, "agentforce")
        self.assertEqual([c["id"] for c in a.capabilities], ["orders"])

    def test_run_scan_catalogs_discovered(self):
        result = scanner_service.run_scan("agentforce", client=_FakeAgentforce())
        self.assertEqual(result["discovered"], 1)
        self.assertTrue(RegistryEntry.objects.filter(identifier="agentforce:BOT1").exists())

    def test_missing_config_raises(self):
        from controlplane.services.scanners.base import ScannerError
        # no injected client and no Salesforce settings → clear ScannerError
        with self.assertRaises(ScannerError):
            AgentforceScanner().scan()


# ── API ──────────────────────────────────────────────────────────────────────────

class ScannerApiTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.admin = _user("admin1", role="platform_admin", bu=self.bu)
        self.approver = _user("approver", role="agent_approver", bu=self.bu)
        self.viewer = _user("viewer", role="viewer", bu=self.bu)

    def test_list_platforms(self):
        self.client.force_login(self.viewer)
        resp = self.client.get("/api/v1/scanners/")
        platforms = resp.json()["platforms"]
        self.assertIn("bedrock", platforms)
        self.assertIn("vertex", platforms)
        self.assertIn("agentforce", platforms)

    def test_scan_requires_admin(self):
        self.client.force_login(self.approver)
        resp = self.client.post("/api/v1/scanners/bedrock/scan/")
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_scan(self):
        self.client.force_login(self.admin)
        with patch("controlplane.services.scanners.bedrock.BedrockScanner._get_client",
                   return_value=_FakeBedrock(AGENTS)):
            resp = self.client.post("/api/v1/scanners/bedrock/scan/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["discovered"], 1)
        self.assertEqual(RegistryEntry.objects.filter(review_status="discovered").count(), 1)

    def test_unknown_platform_400(self):
        self.client.force_login(self.admin)
        resp = self.client.post("/api/v1/scanners/vertex/scan/")
        self.assertEqual(resp.status_code, 400)

    def test_approve_endpoint(self):
        self.client.force_login(self.admin)
        with patch("controlplane.services.scanners.bedrock.BedrockScanner._get_client",
                   return_value=_FakeBedrock(AGENTS)):
            self.client.post("/api/v1/scanners/bedrock/scan/")
        entry = RegistryEntry.objects.get(identifier="bedrock:AG1")

        self.client.force_login(self.approver)
        resp = self.client.post(f"/api/v1/registry/{entry.id}/approve/",
                                data=json.dumps({"status": "approved"}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.review_status, "approved")

    def test_viewer_cannot_approve(self):
        entry = federation.catalog_scanned_agent(
            type("D", (), {"external_id": "X", "name": "X", "platform": "bedrock",
                           "description": "", "model": "", "capabilities": [],
                           "data_access": [], "endpoint_url": ""})()
        )
        self.client.force_login(self.viewer)
        resp = self.client.post(f"/api/v1/registry/{entry.id}/approve/",
                                data=json.dumps({"status": "approved"}), content_type="application/json")
        self.assertEqual(resp.status_code, 403)
