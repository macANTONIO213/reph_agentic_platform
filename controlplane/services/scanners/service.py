"""
Scanner orchestration — Phase 3.

Registry of platform scanners + ``run_scan`` which crawls a platform and catalogs
every discovered agent into the federated registry (as governed, non-live).
"""
from __future__ import annotations

import logging

from controlplane.services.scanners.agentforce import AgentforceScanner
from controlplane.services.scanners.base import ScannerError
from controlplane.services.scanners.bedrock import BedrockScanner
from controlplane.services.scanners.copilot import CopilotScanner
from controlplane.services.scanners.vertex import VertexScanner

logger = logging.getLogger(__name__)

# Platform → scanner class.
_SCANNERS: dict = {
    "bedrock": BedrockScanner,
    "vertex": VertexScanner,
    "agentforce": AgentforceScanner,
    "copilot": CopilotScanner,
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


def run_all_scans(*, by: str = "system") -> dict:
    """
    Run every registered scanner. A platform that isn't configured (ScannerError)
    is reported per-platform and skipped — one missing credential never aborts the
    rest. Returns a per-platform summary plus the total discovered.
    """
    from controlplane.models import AuditLog

    results = []
    total = 0
    for platform in available_platforms():
        try:
            r = run_scan(platform, by=by)
            results.append({"platform": platform, "discovered": r["discovered"]})
            total += r["discovered"]
        except ScannerError as exc:
            results.append({"platform": platform, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 — never let one platform break the sweep
            logger.exception("scan-all: %s failed", platform)
            results.append({"platform": platform, "error": str(exc)})

    AuditLog.objects.create(
        actor=by, action="registry_scanned_all",
        resource_type="RegistryEntry", resource_id="all",
        payload={"total_discovered": total,
                 "platforms": [r["platform"] for r in results]},
    )
    return {"results": results, "total_discovered": total}
