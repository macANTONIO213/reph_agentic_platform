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
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Generator

from django.conf import settings

from controlplane.models import AgentRun

from .base import AgentAdapter, RuntimeEvent

_ALLOWED_SCHEMES = {"http", "https"}


class EndpointValidationError(RuntimeError):
    """Raised when an agent endpoint_url is not safe to call (SSRF guard)."""


def _validate_endpoint(url: str) -> None:
    """
    SSRF guard for agent endpoint URLs.

    Rejects non-HTTP(S) schemes (e.g. file://, ftp://, gopher://) and any host
    that resolves to a loopback, link-local (incl. cloud metadata 169.254.169.254),
    multicast, unspecified, or reserved address.

    Note: RFC1918 private ranges are intentionally permitted because this is an
    enterprise integration platform whose agents may legitimately call internal
    corporate hosts. This check is best-effort and does not fully defeat DNS
    rebinding; the registration path should also constrain endpoint_url.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise EndpointValidationError(
            f"endpoint_url scheme '{parsed.scheme or '(none)'}' is not allowed; "
            "only http and https are permitted."
        )
    host = parsed.hostname
    if not host:
        raise EndpointValidationError("endpoint_url has no host component.")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise EndpointValidationError(f"Could not resolve endpoint host '{host}': {exc}")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_unspecified or ip.is_reserved):
            raise EndpointValidationError(
                f"endpoint_url host '{host}' resolves to a blocked address ({ip})."
            )


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

        # SSRF guard — validate the destination before issuing any request.
        _validate_endpoint(self.agent.endpoint_url)

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
