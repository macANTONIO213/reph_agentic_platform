"""
OpenAI Adapter — supports plain OpenAI and Azure OpenAI (Azure AI Foundry).

Configuration (via .env):
  Plain OpenAI:
    OPENAI_API_KEY=sk-...

  Azure OpenAI / Azure AI Foundry:
    AZURE_OPENAI_KEY=...
    AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
    AZURE_OPENAI_DEPLOYMENT=gpt-4o        # default deployment name

The adapter prefers Azure credentials when both are present.
Set agent.model_id to override the default deployment (e.g. "gpt-4o-mini").
"""
import json
import time
from typing import Generator

from django.conf import settings

from controlplane.models import AgentRun

from .base import AgentAdapter, RuntimeEvent
from .registry_tools import OPENAI_TOOL_SCHEMAS, RegistryToolsMixin


class OpenAIAdapter(RegistryToolsMixin, AgentAdapter):
    DEFAULT_MODEL = "gpt-4o"

    def execute(
        self,
        run: AgentRun,
        message: str,
        history: list[dict],
        meta: dict,
    ) -> Generator[str, None, None]:
        try:
            from openai import AzureOpenAI, OpenAI
        except ImportError:
            meta["output_text"] = "openai package is not installed. Run: pip install openai"
            meta["model_id"] = "unavailable"
            yield RuntimeEvent("token", {"text": meta["output_text"]}).to_sse()
            return

        azure_key = getattr(settings, "AZURE_OPENAI_KEY", "")
        azure_endpoint = getattr(settings, "AZURE_OPENAI_ENDPOINT", "")
        openai_key = getattr(settings, "OPENAI_API_KEY", "")

        if azure_key and azure_endpoint:
            client = AzureOpenAI(
                api_key=azure_key,
                azure_endpoint=azure_endpoint,
                api_version="2024-10-21",
            )
            default_model = getattr(settings, "AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
        elif openai_key:
            client = OpenAI(api_key=openai_key)
            default_model = self.DEFAULT_MODEL
        else:
            meta["output_text"] = (
                "No OpenAI credentials found. "
                "Set OPENAI_API_KEY or AZURE_OPENAI_KEY + AZURE_OPENAI_ENDPOINT in .env"
            )
            meta["model_id"] = "unconfigured"
            yield RuntimeEvent("token", {"text": meta["output_text"]}).to_sse()
            return

        model_id = self.agent.model_id or default_model
        system = (
            self.agent.system_prompt.strip()
            or "You are a helpful agent deployment advisor for the REPH Agentic Platform."
        )

        # OpenAI uses a flat messages list; system goes as first message
        messages = [{"role": "system", "content": system}]
        for h in history:
            if h.get("role") in ("user", "assistant"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": message})

        total_input = 0
        total_output = 0
        output_parts: list[str] = []

        while True:
            response = client.chat.completions.create(
                model=model_id,
                messages=messages,
                tools=OPENAI_TOOL_SCHEMAS,
                tool_choice="auto",
            )
            choice = response.choices[0]
            total_input += response.usage.prompt_tokens
            total_output += response.usage.completion_tokens

            tool_calls = choice.message.tool_calls or []
            tool_results = []

            for tc in tool_calls:
                t0 = time.perf_counter()
                try:
                    inp = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    inp = {}
                result = self._dispatch_tool(tc.function.name, inp, message)
                dur = int((time.perf_counter() - t0) * 1000)
                yield self._record_tool(run, tc.function.name, inp, result, dur)
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    }
                )

            if choice.finish_reason == "stop":
                text = choice.message.content or ""
                if text:
                    output_parts.append(text)
                    yield from self._emit_tokens(text)
                break

            if choice.finish_reason == "tool_calls":
                messages.append(choice.message)
                messages.extend(tool_results)
                continue

            # Fallthrough
            text = choice.message.content or ""
            if text:
                output_parts.append(text)
                yield from self._emit_tokens(text)
            break

        meta["output_text"] = "".join(output_parts)
        meta["input_tokens"] = total_input
        meta["output_tokens"] = total_output
        meta["model_id"] = model_id
