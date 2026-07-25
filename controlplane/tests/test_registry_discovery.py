"""
Phase 2 step 3 tests — capability/domain search + agent-facing /a2a/registry/.

Verifies the shared search (text, domain, capability) and that external agents
can discover the federated catalog over the auth-gated /a2a/ surface.
"""
import json

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from controlplane.models import BusinessUnit, RegistryEntry
from controlplane.services.interop import federation


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


def _entry(name, *, kind=RegistryEntry.Kind.EXTERNAL_A2A, domain="", caps=None, ident=None):
    return RegistryEntry.objects.create(
        kind=kind, identifier=ident or name, name=name, description=f"{name} desc",
        protocol="a2a", domain=domain, capabilities=caps or [], is_active=True,
    )


class SearchEntriesTests(TestCase):
    def setUp(self):
        _entry("Finance Bot", domain="finance",
               caps=[{"id": "forecast", "name": "forecast", "description": "revenue forecast"}])
        _entry("HR Bot", domain="hr",
               caps=[{"id": "pto", "name": "pto", "description": "time off"}])
        _entry("Search Tool", kind=RegistryEntry.Kind.MCP_SERVER, ident="s1",
               caps=[{"id": "search", "name": "search", "description": "web search"}])

    def test_filter_by_domain(self):
        res = federation.search_entries(domain="finance")
        self.assertEqual([e.name for e in res], ["Finance Bot"])

    def test_capability_match(self):
        res = federation.search_entries(capability="forecast")
        self.assertEqual([e.name for e in res], ["Finance Bot"])

    def test_capability_match_by_description(self):
        res = federation.search_entries(capability="time off")
        self.assertEqual([e.name for e in res], ["HR Bot"])

    def test_kind_filter(self):
        res = federation.search_entries(kind=RegistryEntry.Kind.MCP_SERVER)
        self.assertEqual([e.name for e in res], ["Search Tool"])

    def test_inactive_excluded(self):
        RegistryEntry.objects.filter(name="HR Bot").update(is_active=False)
        names = {e.name for e in federation.search_entries()}
        self.assertNotIn("HR Bot", names)


@override_settings(A2A_SERVER_ENABLED=True, A2A_ACCESS_TOKENS=["tok-123"])
class AgentFacingDiscoveryTests(TestCase):
    AUTH = {"HTTP_AUTHORIZATION": "Bearer tok-123"}

    def setUp(self):
        _entry("Finance Bot", domain="finance",
               caps=[{"id": "forecast", "name": "forecast", "description": "revenue"}])
        _entry("Search Tool", kind=RegistryEntry.Kind.MCP_SERVER, ident="s1",
               caps=[{"id": "search", "name": "search", "description": "web"}])

    def test_discovery_lists_all_kinds(self):
        resp = self.client.get("/a2a/registry/", **self.AUTH)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 2)
        # public shape excludes internal fields
        entry = resp.json()["entries"][0]
        self.assertNotIn("visibility", entry)
        self.assertIn("capabilities", entry)

    def test_discovery_capability_filter(self):
        resp = self.client.get("/a2a/registry/?capability=forecast", **self.AUTH)
        self.assertEqual(resp.json()["count"], 1)
        self.assertEqual(resp.json()["entries"][0]["name"], "Finance Bot")

    def test_discovery_requires_auth(self):
        self.assertEqual(self.client.get("/a2a/registry/").status_code, 401)

    def test_discovery_disabled_when_flag_off(self):
        with override_settings(A2A_SERVER_ENABLED=False):
            self.assertEqual(self.client.get("/a2a/registry/", **self.AUTH).status_code, 404)


class RegistryListCapabilityApiTests(TestCase):
    def setUp(self):
        self.user = _user("viewer", role="viewer", bu=_bu())
        _entry("Finance Bot", domain="finance",
               caps=[{"id": "forecast", "name": "forecast", "description": "revenue"}])
        _entry("HR Bot", domain="hr", caps=[{"id": "pto", "name": "pto", "description": "leave"}])

    def test_capability_filter_via_api(self):
        self.client.force_login(self.user)
        resp = self.client.get("/api/v1/registry/?capability=forecast")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)
        self.assertEqual(resp.json()["entries"][0]["name"], "Finance Bot")
