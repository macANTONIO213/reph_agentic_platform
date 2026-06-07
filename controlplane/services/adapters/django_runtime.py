"""
Django Runtime Adapter — uses the Anthropic Claude API with tool use.
Falls back to a deterministic fake engine when ANTHROPIC_API_KEY is not set.
"""
import json
import re
import time
from typing import Generator

from django.conf import settings

from controlplane.models import AgentRun

from .base import AgentAdapter, RuntimeEvent
from .registry_tools import ANTHROPIC_TOOL_SCHEMAS, RegistryToolsMixin


class DjangoRuntimeAdapter(RegistryToolsMixin, AgentAdapter):
    DEFAULT_MODEL = "claude-opus-4-8"

    def execute(
        self,
        run: AgentRun,
        message: str,
        history: list[dict],
        meta: dict,
    ) -> Generator[str, None, None]:
        api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
        if api_key:
            yield from self._execute_llm(run, message, history, meta)
        else:
            yield from self._execute_fake(run, message, meta)

    # ------------------------------------------------------------------
    # Real LLM path
    # ------------------------------------------------------------------

    def _execute_llm(self, run, message, history, meta):
        try:
            import anthropic
        except ImportError:
            yield from self._execute_fake(run, message, meta)
            return

        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        model_id = self.agent.model_id or self.DEFAULT_MODEL
        system = (
            self.agent.system_prompt.strip()
            or "You are a helpful agent deployment advisor for the REPH Agentic Platform."
        )

        messages = list(history) + [{"role": "user", "content": message}]
        total_input = 0
        total_output = 0
        output_parts: list[str] = []

        while True:
            response = client.messages.create(
                model=model_id,
                max_tokens=4096,
                system=system,
                tools=ANTHROPIC_TOOL_SCHEMAS,
                messages=messages,
                thinking={"type": "adaptive"},
            )
            total_input += response.usage.input_tokens
            total_output += response.usage.output_tokens

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    t0 = time.perf_counter()
                    inp = dict(block.input)
                    result = self._dispatch_tool(block.name, inp, message)
                    dur = int((time.perf_counter() - t0) * 1000)
                    yield self._record_tool(run, block.name, inp, result, dur)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        }
                    )

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if block.type == "text" and block.text:
                        output_parts.append(block.text)
                        yield from self._emit_tokens(block.text)
                break

            if response.stop_reason == "tool_use":
                messages = messages + [
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": tool_results},
                ]
                continue

            for block in response.content:
                if block.type == "text" and block.text:
                    output_parts.append(block.text)
                    yield from self._emit_tokens(block.text)
            break

        meta["output_text"] = "".join(output_parts)
        meta["input_tokens"] = total_input
        meta["output_tokens"] = total_output
        meta["model_id"] = model_id

    # ------------------------------------------------------------------
    # Fake path (no API key)
    # ------------------------------------------------------------------

    def _execute_fake(self, run, message, meta):
        t0 = time.perf_counter()
        registry_result = self._registry_search(message)
        yield self._record_tool(run, "registry_search", {"query": message}, registry_result, int((time.perf_counter() - t0) * 1000))

        t0 = time.perf_counter()
        risk_result = self._classify_risk(message)
        yield self._record_tool(run, "risk_classifier", {"query": message}, risk_result, int((time.perf_counter() - t0) * 1000))

        t0 = time.perf_counter()
        checklist = self._deployment_checklist(risk_result)
        yield self._record_tool(run, "deployment_gate_builder", {"risk_tier": risk_result["risk_tier"]}, checklist, int((time.perf_counter() - t0) * 1000))

        response = self._compose_response(message, risk_result, checklist)
        yield from self._emit_tokens(response)
        meta["output_text"] = response
        meta["model_id"] = "fake"

    def _compose_response(self, message, risk_result, checklist):
        controls = "\n".join(f"- {item}" for item in checklist["controls"])
        reasons = "\n".join(f"- {item}" for item in risk_result["reasons"])
        system_context = self.agent.system_prompt.strip()
        note = f"Operating context: {system_context}\n\n" if system_context else ""
        return (
            f"{note}"
            "I can help deploy this through the Agentic Platform control plane.\n\n"
            f"Recommended risk tier: Tier {risk_result['risk_tier']}\n"
            f"Why:\n{reasons}\n\n"
            "Deployment path:\n"
            "- Register the agent manifest with owner, platform, data sources, tools.\n"
            "- Put the agent into Review status, then Pilot once governance checks pass.\n"
            "- Enable telemetry before any production user traffic.\n"
            "- Promote to Production only after usage, failure, and feedback signals are acceptable.\n\n"
            f"Required controls:\n{controls}\n\n"
            "Note: risk tier is a keyword-based recommendation. A human reviewer must confirm "
            "the final tier and sign off on the deployment checklist before production promotion.\n\n"
            "Set ANTHROPIC_API_KEY in .env to replace this with a real Claude response."
        )
