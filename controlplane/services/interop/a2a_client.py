"""
A2A client — Phase 2 (fetch external agent cards).

Dependency-light, SSRF-guarded fetch of a remote A2A agent card so an external
agent can be registered into the federated catalog.  Mirrors the governed style
of ``mcp_client`` / ``rest_connector``: destination validated before any network,
response size/time capped.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from controlplane.services.interop.net_guard import validate_destination

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15
_MAX_RESPONSE_BYTES = 1_048_576  # 1 MB


class A2AClientError(RuntimeError):
    """Transport- or validation-level failure fetching an A2A agent card."""


def fetch_card(url: str, *, actor: str = "system") -> dict:
    """
    GET an A2A agent card from ``url`` and validate it minimally.

    Raises A2AClientError on transport error, invalid JSON, or a document that
    is not a plausible agent card (must carry at least ``name`` and ``url``).
    """
    validate_destination(url, error_cls=A2AClientError)
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "RELX-AgentPlatform/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            raw = resp.read(_MAX_RESPONSE_BYTES)
    except Exception as exc:  # noqa: BLE001 — surface as domain error
        raise A2AClientError(f"Could not fetch agent card: {exc}") from exc

    try:
        card = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise A2AClientError("Agent card is not valid JSON.") from exc

    if not isinstance(card, dict) or not card.get("name") or not card.get("url"):
        raise A2AClientError("Not a valid A2A agent card (missing 'name' or 'url').")
    return card
