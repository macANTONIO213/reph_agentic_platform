"""
Base class and shared utilities for all platform adapters.

Every adapter receives a run, a message, conversation history, and a meta dict.
It yields SSE event strings and populates meta with:
  output_text, input_tokens, output_tokens, model_id
"""
import json
import re
import time
from abc import ABC, abstractmethod
from typing import Generator

from controlplane.models import Agent, AgentRun, AgentToolCall


class RuntimeEvent:
    def __init__(self, event: str, data: dict):
        self.event = event
        self.data = data

    def to_sse(self) -> str:
        return f"event: {self.event}\ndata: {json.dumps(self.data)}\n\n"


class AgentAdapter(ABC):
    def __init__(self, agent: Agent, user_label: str):
        self.agent = agent
        self.user_label = user_label

    @abstractmethod
    def execute(
        self,
        run: AgentRun,
        message: str,
        history: list[dict],
        meta: dict,
    ) -> Generator[str, None, None]:
        """Yield SSE strings; populate meta with output_text / input_tokens / output_tokens / model_id."""
        ...

    # ------------------------------------------------------------------
    # Shared utilities available to all adapters
    # ------------------------------------------------------------------

    def _record_tool(
        self,
        run: AgentRun,
        tool_name: str,
        input_payload: dict,
        output_payload: dict,
        duration_ms: int,
    ) -> str:
        AgentToolCall.objects.create(
            run=run,
            tool_name=tool_name,
            input_payload=input_payload,
            output_payload=output_payload,
            duration_ms=duration_ms,
        )
        return RuntimeEvent(
            "tool",
            {
                "tool_name": tool_name,
                "summary": output_payload.get("summary", "Tool completed"),
                "output": output_payload,
            },
        ).to_sse()

    def _emit_tokens(self, text: str, delay: float = 0.015) -> Generator[str, None, None]:
        for chunk in re.split(r"(\s+)", text):
            if chunk:
                yield RuntimeEvent("token", {"text": chunk}).to_sse()
                time.sleep(delay)
