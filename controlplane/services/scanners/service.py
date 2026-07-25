"""
Scanner orchestration — Phase 3.

Registry of platform scanners + ``run_scan`` which crawls a platform and catalogs
every discovered agent into the federated registry (as governed, non-live).
"""
from __future__ import annotations

import logging

from controlplane.services.scanners.base import ScannerError
from controlplane.services.scanners.bedrock import BedrockScanner

logger = logging.getLogger(__name__)

# Platform → scanner class. Vertex/Agentforce/Copilot land here in step 2.
_SCANNERS: dict = {
    "bedrock": BedrockScanner,
}


def available_platforms() -> list[str]:
    return sorted(_SCANNERS.keys())


def get_scanner(platform: str, **kwargs):
    cls = _SCANNERS.get(platform)
    if cls is None:
        raise ScannerError(f"Unknown scanner platform: {platform}")
    return cls(**kwargs)


def run_scan(platform: str, *, by: str = "system", **scanner_kwargs) -> dict:
    """
    Run one platform scanner and catalog its results.

    Returns a summary dict.  Discovered agents land as review_status="discovered"
    and stay out of agent-facing discovery until approved.
    """
    from controlplane.models import AuditLog
    from controlplane.services.interop import federation

    scanner = get_scanner(platform, **scanner_kwargs)
    discovered = scanner.scan()
    entries = [federation.catalog_scanned_agent(d) for d in discovered]

    AuditLog.objects.create(
        actor=by, action="registry_scanned",
        resource_type="RegistryEntry", resource_id=platform,
        payload={"platform": platform, "discovered": len(entries)},
    )
    return {"platform": platform, "discovered": len(entries)}
