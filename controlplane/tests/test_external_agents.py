"""
Phase 2 step 2 tests — external A2A agent registration (federated catalog).

The remote card fetch is mocked (urllib), so these run offline. Covers card
validation, SSRF rejection, cataloging as external_a2a_agent, the register-by-URL
API, role gating, and deactivation.
"""
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from controlplane.models import BusinessUnit, RegistryEntry
from controlplane.services.interop import federation
from controlplane.services.interop.a2a_client import A2AClientError, fetch_card


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


EXTERNAL_CARD = {
    "name": "Vertex Finance Agent",
    "description": "Handles finance queries",
    "url": "https://partner.example/a2a/agents/fin/rpc/",
    "skills": [{"id": "forecast", "name": "forecast", "description": "Revenue forecast"}],
    "provider": {"organization": "Partner Corp"},
    "x-governance": {"risk_tier": 3, "status": "production"},
}


class _FakeResp:
    def __init__(self, payload, *, raw=None):
        self._raw = raw if raw is not None else json.dumps(payload).encode()

    def read(self, n=None):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(payload=None, *, raw=None):
    return lambda req, timeout=None: _FakeResp(payload, raw=raw)


# ── fetch_card ───────────────────────────────────────────────────────────────────

class FetchCardTests(TestCase):
    def test_valid_card(self):
        with patch("urllib.request.urlopen", _urlopen(EXTERNAL_CARD)):
            card = fetch_card("https://partner.example/.well-known/agent.json")
        self.assertEqual(card["name"], "Vertex Finance Agent")

    def test_invalid_json_raises(self):
        with patch("urllib.request.urlopen", _urlopen(raw=b"not json")):
            with self.assertRaises(A2AClientError):
                fetch_card("https://partner.example/card")

    def test_missing_fields_raises(self):
        with patch("urllib.request.urlopen", _urlopen({"description": "no name/url"})):
            with self.assertRaises(A2AClientError):
                fetch_card("https://partner.example/card")

    def test_ssrf_blocks_localhost(self):
        called = {"n": 0}

        def _tracker(req, timeout=None):
            called["n"] += 1
            return _FakeResp(EXTERNAL_CARD)

        with patch("urllib.request.urlopen", _tracker):
            with self.assertRaises(A2AClientError):
                fetch_card("http://localhost:9000/card")
        self.assertEqual(called["n"], 0)


# ── register_external_agent ──────────────────────────────────────────────────────

class RegisterExternalTests(TestCase):
    def test_catalogs_external_entry(self):
        with patch("urllib.request.urlopen", _urlopen(EXTERNAL_CARD)):
            entry = federation.register_external_agent(
                "https://partner.example/.well-known/agent.json", domain="finance",
            )
        self.assertEqual(entry.kind, RegistryEntry.Kind.EXTERNAL_A2A)
        self.assertEqual(entry.endpoint_url, EXTERNAL_CARD["url"])
        self.assertEqual(entry.provider_org, "Partner Corp")
        self.assertEqual(entry.domain, "finance")
        self.assertEqual({c["id"] for c in entry.capabilities}, {"forecast"})
        self.assertEqual(entry.governance["risk_tier"], 3)
        self.assertEqual(entry.source, "manual")


# ── API ──────────────────────────────────────────────────────────────────────────

class ExternalRegistrationApiTests(TestCase):
    def setUp(self):
        self.bu = _bu()
        self.builder = _user("builder", role="agent_builder", bu=self.bu)
        self.viewer = _user("viewer", role="viewer", bu=self.bu)

    def _register(self, body):
        return self.client.post("/api/v1/registry/external/",
                                data=json.dumps(body), content_type="application/json")

    def test_register_by_url(self):
        self.client.force_login(self.builder)
        with patch("urllib.request.urlopen", _urlopen(EXTERNAL_CARD)):
            resp = self._register({"card_url": "https://partner.example/card", "domain": "finance"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["kind"], "external_a2a_agent")
        # appears in the federated catalog under the external kind
        listing = self.client.get("/api/v1/registry/?kind=external_a2a_agent")
        self.assertEqual(listing.json()["total"], 1)

    def test_missing_card_url_400(self):
        self.client.force_login(self.builder)
        self.assertEqual(self._register({}).status_code, 400)

    def test_bad_card_400(self):
        self.client.force_login(self.builder)
        with patch("urllib.request.urlopen", _urlopen({"nope": 1})):
            resp = self._register({"card_url": "https://partner.example/card"})
        self.assertEqual(resp.status_code, 400)

    def test_viewer_cannot_register(self):
        self.client.force_login(self.viewer)
        resp = self._register({"card_url": "https://partner.example/card"})
        self.assertEqual(resp.status_code, 403)

    def test_deactivate_entry(self):
        self.client.force_login(self.builder)
        with patch("urllib.request.urlopen", _urlopen(EXTERNAL_CARD)):
            entry_id = self._register({"card_url": "https://partner.example/card"}).json()["id"]
        resp = self.client.delete(f"/api/v1/registry/{entry_id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(RegistryEntry.objects.get(id=entry_id).is_active)
