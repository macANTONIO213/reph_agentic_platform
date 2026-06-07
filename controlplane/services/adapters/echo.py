"""
Echo Adapter — reflects the message back with metadata.
Used for testing the platform without any external API.
"""
from typing import Generator

from controlplane.models import AgentRun

from .base import AgentAdapter


class EchoAdapter(AgentAdapter):
    def execute(
        self,
        run: AgentRun,
        message: str,
        history: list[dict],
        meta: dict,
    ) -> Generator[str, None, None]:
        text = (
            f"[Echo] Agent: {self.agent.name} | "
            f"Platform: {self.agent.get_platform_display()} | "
            f"Risk tier: {self.agent.risk_tier}\n\n"
            f"You said: {message}"
        )
        meta["output_text"] = text
        meta["model_id"] = "echo"
        yield from self._emit_tokens(text)
