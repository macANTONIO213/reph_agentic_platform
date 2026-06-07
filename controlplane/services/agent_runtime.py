"""
Platform Agent Runtime — routes each agent run to the correct adapter.

The runtime owns the run lifecycle (create, save, telemetry, session update).
Adapters own the actual LLM / API call.

Platform → Adapter mapping:
  django_runtime  → DjangoRuntimeAdapter  (Anthropic Claude)
  azure_ai_foundry→ OpenAIAdapter         (Azure OpenAI / GPT-4o)
  copilot_studio  → HttpApiAdapter        (Microsoft Copilot Studio REST)
  custom_api      → HttpApiAdapter        (any REST endpoint)
  vendor          → HttpApiAdapter        (any REST endpoint)
  embedded        → EchoAdapter           (internal embed, no direct call)
  bedrock         → BedrockAdapter        (AWS Bedrock Converse API)
"""
import logging
import time
import uuid
from typing import Generator

from django.utils import timezone

from controlplane.models import Agent, AgentRun, ConversationSession, TelemetryEvent
from controlplane.services.pricing import price_run

logger = logging.getLogger(__name__)

from .adapters import (
    BedrockAdapter,
    DjangoRuntimeAdapter,
    EchoAdapter,
    HttpApiAdapter,
    OpenAIAdapter,
    RuntimeEvent,
)

# Re-export so views.py can import RuntimeEvent from here if needed.
__all__ = ["PlatformAgentRuntime", "RuntimeEvent"]

_PLATFORM_ADAPTER_MAP = {
    Agent.Platform.DJANGO: DjangoRuntimeAdapter,
    Agent.Platform.AZURE_AI: OpenAIAdapter,
    Agent.Platform.COPILOT: HttpApiAdapter,
    Agent.Platform.CUSTOM: HttpApiAdapter,
    Agent.Platform.VENDOR: HttpApiAdapter,
    Agent.Platform.EMBEDDED: EchoAdapter,
}

# "bedrock" is a string value not yet in Platform.choices — still routes correctly.
_PLATFORM_ADAPTER_MAP["bedrock"] = BedrockAdapter


def _select_adapter_class(agent: Agent):
    cls = _PLATFORM_ADAPTER_MAP.get(agent.platform)
    if cls is None:
        # Unknown platform: fall back to Echo so a run always completes.
        cls = EchoAdapter
    return cls


class PlatformAgentRuntime:
    """Creates and manages a single agent run end-to-end."""

    def __init__(self, agent: Agent, user_label: str = "demo_user", channel: str = "web"):
        self.agent = agent
        self.user_label = user_label
        self.channel = channel

    def stream(
        self, message: str, session: ConversationSession | None = None
    ) -> Generator[str, None, None]:
        started = time.perf_counter()
        run = AgentRun.objects.create(
            agent=self.agent,
            user_label=self.user_label,
            channel=self.channel,
            input_text=message,
        )
        self._telemetry("agent_invoked", run, {"input_length": len(message)})

        try:
            yield RuntimeEvent("status", {"run_id": str(run.id), "message": "Run started"}).to_sse()

            history = list(session.messages) if session and session.messages else []
            meta = {"output_text": "", "input_tokens": 0, "output_tokens": 0, "model_id": ""}

            adapter_cls = _select_adapter_class(self.agent)
            adapter = adapter_cls(agent=self.agent, user_label=self.user_label)
            yield from adapter.execute(run, message, history, meta)

            run.status = AgentRun.Status.COMPLETED
            run.output_text = meta["output_text"]
            run.input_tokens = meta["input_tokens"]
            run.output_tokens = meta["output_tokens"]
            run.model_id = meta["model_id"]
            run.cost_usd = price_run(run.input_tokens, run.output_tokens, run.model_id)
            run.completed_at = timezone.now()
            run.latency_ms = int((time.perf_counter() - started) * 1000)
            run.save(
                update_fields=[
                    "status", "output_text", "input_tokens", "output_tokens",
                    "model_id", "cost_usd", "completed_at", "latency_ms",
                ]
            )

            if session is not None:
                session.messages = session.messages + [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": meta["output_text"]},
                ]
                session.save(update_fields=["messages"])

            self._telemetry(
                "task_completed",
                run,
                {
                    "latency_ms": run.latency_ms,
                    "tool_calls": run.tool_calls.count(),
                    "output_length": len(meta["output_text"]),
                    "input_tokens": run.input_tokens,
                    "output_tokens": run.output_tokens,
                    "model_id": run.model_id,
                    "adapter": adapter_cls.__name__,
                },
            )
            yield RuntimeEvent(
                "done",
                {
                    "run_id": str(run.id),
                    "latency_ms": run.latency_ms,
                    "tool_calls": run.tool_calls.count(),
                    "session_id": str(session.id) if session else None,
                    "input_tokens": run.input_tokens,
                    "output_tokens": run.output_tokens,
                    "model_id": run.model_id,
                    "adapter": adapter_cls.__name__,
                },
            ).to_sse()

        except Exception as exc:
            error_id = str(uuid.uuid4())[:8]
            logger.exception("Agent run %s failed [error_id=%s]", run.id, error_id)
            run.status = AgentRun.Status.FAILED
            run.completed_at = timezone.now()
            run.latency_ms = int((time.perf_counter() - started) * 1000)
            run.output_text = str(exc)
            run.save(update_fields=["status", "completed_at", "latency_ms", "output_text"])
            self._telemetry("task_failed", run, {"error": str(exc), "error_id": error_id, "latency_ms": run.latency_ms})
            yield RuntimeEvent("error", {"message": f"Run failed. Reference: {error_id}"}).to_sse()

    def _telemetry(self, event_type: str, run: AgentRun, payload: dict):
        if not self.agent.telemetry_enabled:
            return
        TelemetryEvent.objects.create(
            agent=self.agent,
            run=run,
            event_type=event_type,
            actor=self.user_label,
            business_unit=self.agent.business_unit,
            payload=payload,
        )
