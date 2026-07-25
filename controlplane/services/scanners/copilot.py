"""
Microsoft Copilot Studio scanner — Phase 3.

Discovers Copilot Studio agents (Power Platform bots, stored in Dataverse) via the
Dataverse Web API and normalises them to ``DiscoveredAgent``.  Client is injectable
(offline-testable); in production ``_get_client`` builds a REST client from the
environment settings.  The Dataverse endpoint is SSRF-guarded before any call.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from controlplane.services.scanners.base import BaseScanner, DiscoveredAgent, ScannerError

logger = logging.getLogger(__name__)


class CopilotScanner(BaseScanner):
    platform = "copilot"

    def __init__(self, client=None, *, environment_url: str | None = None, access_token: str | None = None):
        self._client = client
        self._environment_url = environment_url
        self._token = access_token

    def _get_client(self):
        if self._client is not None:
            return self._client
        from django.conf import settings
        env = self._environment_url or getattr(settings, "COPILOT_ENVIRONMENT_URL", "")
        token = self._token or getattr(settings, "COPILOT_ACCESS_TOKEN", "")
        if not env or not token:
            raise ScannerError("COPILOT_ENVIRONMENT_URL / COPILOT_ACCESS_TOKEN not configured.")
        return _DataverseRestClient(env.rstrip("/"), token)

    def scan(self) -> list[DiscoveredAgent]:
        client = self._get_client()
        try:
            resp = client.list_agents()
        except Exception as exc:  # noqa: BLE001
            raise ScannerError(f"Copilot list_agents failed: {exc}") from exc

        out: list[DiscoveredAgent] = []
        for a in (resp or {}).get("agents", []) or []:
            if not isinstance(a, dict) or not a.get("id"):
                continue
            topics = a.get("topics") or a.get("capabilities") or []
            out.append(DiscoveredAgent(
                external_id=str(a["id"]),
                name=a.get("name") or str(a["id"]),
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


class _DataverseRestClient:
    """Thin adapter exposing a uniform ``list_agents`` over the Dataverse Web API."""
    _API = "v9.2"

    def __init__(self, environment_url: str, token: str):
        self._env = environment_url
        self._token = token

    def list_agents(self) -> dict:
        from controlplane.services.interop.net_guard import validate_destination
        url = f"{self._env}/api/data/{self._API}/bots?$select=botid,name,language"
        validate_destination(url, error_cls=ScannerError)
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read(2_000_000))
        return {"agents": [
            {"id": rec.get("botid"), "name": rec.get("name"), "description": rec.get("language", "")}
            for rec in data.get("value", []) if isinstance(rec, dict) and rec.get("botid")
        ]}
