"""Scanner base types — Phase 3."""
from __future__ import annotations

from dataclasses import dataclass, field


class ScannerError(RuntimeError):
    """Raised when a scanner cannot reach or read its platform."""


@dataclass
class DiscoveredAgent:
    """A normalised agent found on an external platform (pre-catalog)."""
    external_id: str
    name: str
    platform: str                       # "bedrock" | "vertex" | "agentforce" | "copilot"
    description: str = ""
    model: str = ""                     # foundation/LLM powering the agent
    capabilities: list = field(default_factory=list)   # [{id, name, description}]
    data_access: list = field(default_factory=list)    # resources the agent can reach
    endpoint_url: str = ""
    raw: dict = field(default_factory=dict)


class BaseScanner:
    """A platform crawler. Subclasses implement ``scan`` and set ``platform``."""
    platform: str = "base"

    def scan(self) -> list[DiscoveredAgent]:  # pragma: no cover - interface
        raise NotImplementedError
