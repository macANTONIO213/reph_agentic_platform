"""
AWS Bedrock scanner — Phase 3.

Discovers Bedrock Agents via the ``bedrock-agent`` control-plane API and
normalises them to ``DiscoveredAgent``.  The boto3 client is injectable so the
scanner is unit-testable offline; in production it is created from the platform's
AWS settings.
"""
from __future__ import annotations

import logging

from controlplane.services.scanners.base import BaseScanner, DiscoveredAgent, ScannerError

logger = logging.getLogger(__name__)


class BedrockScanner(BaseScanner):
    platform = "bedrock"

    def __init__(self, client=None, *, region: str | None = None):
        self._client = client
        self._region = region

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import boto3
            from django.conf import settings
            region = self._region or getattr(settings, "AWS_REGION", "us-east-1")
            return boto3.client("bedrock-agent", region_name=region)
        except Exception as exc:  # noqa: BLE001
            raise ScannerError(f"Cannot create Bedrock client: {exc}") from exc

    def scan(self) -> list[DiscoveredAgent]:
        client = self._get_client()
        try:
            resp = client.list_agents()
        except Exception as exc:  # noqa: BLE001
            raise ScannerError(f"Bedrock list_agents failed: {exc}") from exc

        out: list[DiscoveredAgent] = []
        for s in resp.get("agentSummaries", []) or []:
            if not isinstance(s, dict) or not s.get("agentId"):
                continue
            out.append(DiscoveredAgent(
                external_id=str(s.get("agentId", "")),
                name=s.get("agentName", "") or s.get("agentId", ""),
                platform=self.platform,
                description=s.get("description", "") or "",
                model=s.get("foundationModel", "") or "",
                capabilities=self._capabilities(s),
                raw=s,
            ))
        return out

    @staticmethod
    def _capabilities(summary: dict) -> list[dict]:
        # Bedrock exposes capabilities via action groups; the summary rarely
        # carries them, so we surface latestAgentVersion/status as a coarse hint.
        caps = []
        status = summary.get("agentStatus")
        if status:
            caps.append({"id": "status", "name": "status", "description": str(status)})
        return caps
