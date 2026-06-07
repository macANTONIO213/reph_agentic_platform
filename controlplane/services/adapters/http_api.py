"""
HTTP API Adapter — calls any REST endpoint via POST.

Covers: Microsoft Copilot Studio, custom REST agents, vendor platforms.

The request body sent:
  { "message": "...", "system_prompt": "...", "history": [...], "user": "..." }

The adapter handles two response shapes:
  1. JSON with a "message", "response", "text", or "output" key → streamed as tokens
  2. Plain text → streamed as tokens

Set agent.endpoint_url to the target URL.
Optionally set HTTP_API_BEARER_TOKEN in .env for Authorization headers.
"""
import json
import urllib.error
import urllib.request
from typing import Generator

from django.conf import settings

from controlplane.models import AgentRun

from .base import AgentAdapter, RuntimeEvent


class HttpApiAdapter(AgentAdapter):
    TIMEOUT_SECONDS = 60

    def execute(
        self,
        run: AgentRun,
        message: str,
        history: list[dict],
        meta: dict,
    ) -> Generator[str, None, None]:
        if not self.agent.endpoint_url:
            text = (
                f"Agent '{self.agent.name}' uses {self.agent.get_platform_display()} "
                "but has no endpoint_url configured. "
                "Set it in the admin to enable execution."
            )
            meta["output_text"] = text
            meta["model_id"] = "unconfigured"
            yield RuntimeEvent("token", {"text": text}).to_sse()
            return

        body = json.dumps(
            {
                "message": message,
                "system_prompt": self.agent.system_prompt,
                "history": history,
                "user": self.user_label,
            }
        ).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "REPH-Agentic-Platform/1.0",
        }
        bearer = getattr(settings, "HTTP_API_BEARER_TOKEN", "")
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        req = urllib.request.Request(
            self.agent.endpoint_url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"HTTP {e.code} from {self.agent.endpoint_url}: {e.reason}"
            )
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Could not reach {self.agent.endpoint_url}: {e.reason}"
            )

        # Parse response
        text = raw
        try:
            payload = json.loads(raw)
            text = (
                payload.get("message")
                or payload.get("response")
                or payload.get("text")
                or payload.get("output")
                or payload.get("content")
                or raw
            )
            if not isinstance(text, str):
                text = json.dumps(text)
        except json.JSONDecodeError:
            pass

        meta["output_text"] = text
        meta["model_id"] = f"http:{self.agent.endpoint_url}"
        yield from self._emit_tokens(text)
