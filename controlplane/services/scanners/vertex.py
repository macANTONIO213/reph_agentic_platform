"""
Google Vertex AI scanner — Phase 3 step 2.

Discovers Vertex AI Agent Engine / Reasoning Engine agents and normalises them to
``DiscoveredAgent``.  The client is injectable so the scanner is unit-testable
offline; in production ``_get_client`` builds a REST client from the project's
Google Cloud settings.  Follows the same shape as ``BedrockScanner``.
"""
from __future__ import annotations

import logging

from controlplane.services.scanners.base import BaseScanner, DiscoveredAgent, ScannerError

logger = logging.getLogger(__name__)


class VertexScanner(BaseScanner):
    platform = "vertex"

    def __init__(self, client=None, *, project: str | None = None, location: str | None = None):
        self._client = client
        self._project = project
        self._location = location

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from googleapiclient.discovery import build  # google-api-python-client
            from django.conf import settings
            project = self._project or getattr(settings, "GOOGLE_CLOUD_PROJECT", "")
            location = self._location or getattr(settings, "GOOGLE_CLOUD_LOCATION", "us-central1")
            if not project:
                raise ScannerError("GOOGLE_CLOUD_PROJECT is not set.")
            service = build("aiplatform", "v1", cache_discovery=False)
            return _VertexRestClient(service, f"projects/{project}/locations/{location}")
        except ScannerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ScannerError(f"Cannot create Vertex client: {exc}") from exc

    def scan(self) -> list[DiscoveredAgent]:
        client = self._get_client()
        try:
            resp = client.list_reasoning_engines()
        except Exception as exc:  # noqa: BLE001
            raise ScannerError(f"Vertex list_reasoning_engines failed: {exc}") from exc

        out: list[DiscoveredAgent] = []
        for e in (resp or {}).get("reasoningEngines", []) or []:
            if not isinstance(e, dict) or not e.get("name"):
                continue
            ext = str(e["name"]).rsplit("/", 1)[-1]
            spec = e.get("spec") if isinstance(e.get("spec"), dict) else {}
            out.append(DiscoveredAgent(
                external_id=ext,
                name=e.get("displayName") or ext,
                platform=self.platform,
                description=e.get("description", "") or "",
                model=spec.get("model", "") or "",
                capabilities=[
                    {"id": c.get("name", ""), "name": c.get("name", ""),
                     "description": c.get("description", "")}
                    for c in (spec.get("classMethods") or [])
                    if isinstance(c, dict) and c.get("name")
                ],
                raw=e,
            ))
        return out


class _VertexRestClient:
    """Thin adapter exposing a uniform ``list_reasoning_engines`` over the SDK."""
    def __init__(self, service, parent: str):
        self._service = service
        self._parent = parent

    def list_reasoning_engines(self) -> dict:
        return (
            self._service.projects().locations().reasoningEngines()
            .list(parent=self._parent).execute() or {}
        )
