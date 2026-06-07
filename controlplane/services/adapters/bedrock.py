"""
AWS Bedrock Adapter — uses the Bedrock Converse API.

Supports any Bedrock-hosted model (Claude, Titan, Llama, Mistral, etc.).
Uses the unified Converse API so the same adapter works across model families.

Configuration (via .env):
  AWS_ACCESS_KEY_ID=...
  AWS_SECRET_ACCESS_KEY=...
  AWS_REGION=us-east-1          # defaults to us-east-1

Set agent.model_id to the Bedrock model ID, e.g.:
  anthropic.claude-opus-4-8-20251101-v1:0
  anthropic.claude-3-5-sonnet-20241022-v2:0
  amazon.titan-text-express-v1
  meta.llama3-8b-instruct-v1:0
"""
import json
from typing import Generator

from django.conf import settings

from controlplane.models import AgentRun

from .base import AgentAdapter, RuntimeEvent


class BedrockAdapter(AgentAdapter):
    DEFAULT_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"

    def execute(
        self,
        run: AgentRun,
        message: str,
        history: list[dict],
        meta: dict,
    ) -> Generator[str, None, None]:
        try:
            import boto3
        except ImportError:
            text = "boto3 is not installed. Run: pip install boto3"
            meta["output_text"] = text
            meta["model_id"] = "unavailable"
            yield RuntimeEvent("token", {"text": text}).to_sse()
            return

        aws_key = getattr(settings, "AWS_ACCESS_KEY_ID", "")
        aws_secret = getattr(settings, "AWS_SECRET_ACCESS_KEY", "")
        region = getattr(settings, "AWS_REGION", "us-east-1")

        if not aws_key or not aws_secret:
            text = "AWS credentials not configured. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env"
            meta["output_text"] = text
            meta["model_id"] = "unconfigured"
            yield RuntimeEvent("token", {"text": text}).to_sse()
            return

        client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=aws_key,
            aws_secret_access_key=aws_secret,
        )

        model_id = self.agent.model_id or self.DEFAULT_MODEL
        system_prompt = (
            self.agent.system_prompt.strip()
            or "You are a helpful agent deployment advisor for the REPH Agentic Platform."
        )

        # Build messages in Converse API format
        converse_messages = []
        for h in history:
            role = h.get("role")
            content = h.get("content", "")
            if role in ("user", "assistant") and content:
                converse_messages.append(
                    {"role": role, "content": [{"text": content}]}
                )
        converse_messages.append({"role": "user", "content": [{"text": message}]})

        try:
            response = client.converse(
                modelId=model_id,
                messages=converse_messages,
                system=[{"text": system_prompt}],
                inferenceConfig={"maxTokens": 4096},
            )
        except Exception as e:
            raise RuntimeError(f"Bedrock error: {e}")

        usage = response.get("usage", {})
        output_msg = response.get("output", {}).get("message", {})
        content_blocks = output_msg.get("content", [])

        text_parts = [b["text"] for b in content_blocks if b.get("text")]
        full_text = "\n".join(text_parts)

        meta["output_text"] = full_text
        meta["input_tokens"] = usage.get("inputTokens", 0)
        meta["output_tokens"] = usage.get("outputTokens", 0)
        meta["model_id"] = model_id

        yield from self._emit_tokens(full_text)
