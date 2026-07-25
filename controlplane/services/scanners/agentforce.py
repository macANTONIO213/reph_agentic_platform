"""
Salesforce Agentforce scanner — Phase 3 step 2.

Discovers Agentforce / Einstein Bot agents via the Salesforce REST query API and
normalises them to ``DiscoveredAgent``.  The client is injectable (offline-testable);
in production ``_get_client`` builds a REST client from the org's Salesforce
settings.  The Salesforce endpoint is SSRF-guarded before any call.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

from controlplane.services.scanners.base import BaseScanner, DiscoveredAgent, ScannerError

logger = logging.getLogger(__name__)


class AgentforceScanner(BaseScanner):
    platform = "agentforce"

    def __init__(self, client=None, *, instance_url: str | None = None, access_token: str | None = None):
        self._client = client
        self._instance_url = instance_url
        self._token = access_token

    def _get_client(self):
        if self._client is not None:
            return self._client
        from django.conf import settings
        instance = self._instance_url or getattr(settings, "SALESFORCE_INSTANCE_URL", "")
        token = self._token or getattr(settings, "SALESFORCE_ACCESS_TOKEN", "")
        if not instance or not token:
            raise ScannerError("SALESFORCE_INSTANCE_URL / SALESFORCE_ACCESS_TOKEN not configured.")
        return _SalesforceRestClient(instance.rstrip("/"), token)

    def scan(self) -> list[DiscoveredAgent]:
        client = self._get_client()
        try:
            resp = client.list_agents()
        except Exception as exc:  # noqa: BLE001
            raise ScannerError(f"Agentforce list_agents failed: {exc}") from exc

        out: list[DiscoveredAgent] = []
        for a in (resp or {}).get("agents", []) or []:
            if not isinstance(a, dict) or not a.get("id"):
                continue
            topics = a.get("topics") or a.get("capabilities") or []
            out.append(DiscoveredAgent(
                external_id=str(a["id"]),
                name=a.get("name") or a.get("developerName") or str(a["id"]),
                platform=self.platform,
                description=a.get("description", "") or "",
                model=a.get("model", "") or "",
                capabilities=[
                    {"id": t.get("name", ""), "name": t.get("name", ""),
                     "description": t.get("description", "")}
                    for t in topics if isinstance(t, dict) and t.get("name")
                ],
                raw=a,
            ))
        return out


class _SalesforceRestClient:
    """Thin adapter exposing a uniform ``list_agents`` over the Salesforce REST API."""
    _API = "v60.0"
    _SOQL = "SELECT Id, DeveloperName, MasterLabel, Description FROM BotDefinition"

    def __init__(self, instance_url: str, token: str):
        self._instance = instance_url
        self._token = token

    def list_agents(self) -> dict:
        from controlplane.services.interop.net_guard import validate_destination
        url = f"{self._instance}/services/data/{self._API}/query?q={urllib.parse.quote(self._SOQL)}"
        validate_destination(url, error_cls=ScannerError)
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read(2_000_000))
        return {"agents": [
            {"id": rec.get("Id"),
             "name": rec.get("MasterLabel") or rec.get("DeveloperName"),
             "description": rec.get("Description", "")}
            for rec in data.get("records", []) if isinstance(rec, dict) and rec.get("Id")
        ]}
