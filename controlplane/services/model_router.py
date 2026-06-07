"""
ModelRouter — Phase E Dynamic Model Selection

Selects the optimal LLM model for a given agent task based on:
  1. Explicit override (step-level or agent-level model_id field)
  2. Risk tier routing (Tier 4 → most capable; Tier 1 → fastest/cheapest)
  3. Budget pressure (if agent.budget_alert is set, route to cheaper model)
  4. Latency routing (low timeout → fast model)
  5. Platform default

Decision matrix (in priority order):
  explicit override           → honour it
  risk_tier == 4              → claude-opus-4-8       (capability floor — budget/latency cannot downgrade)
  budget_alert == True        → claude-haiku-4-5      (cheapest; Tiers 1–3 only)
  timeout_seconds <= 30       → claude-haiku-4-5      (fastest; Tiers 1–3 only)
  risk_tier == 3              → claude-sonnet-4-6     (balanced)
  risk_tier <= 2              → claude-sonnet-4-6     (default)
  fallback                    → claude-sonnet-4-6

For OpenAI/Azure platform agents the routing mirrors the same tiers:
  tier 4 → gpt-4o, tier 3 → gpt-4o, tier <= 2 → gpt-4o-mini

Usage::
    from controlplane.services.model_router import model_router

    model_id = model_router.select(agent, task=workflow_task)
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── Routing tables ────────────────────────────────────────────────────────────

_ANTHROPIC_BY_TIER = {
    4: "claude-opus-4-8",
    3: "claude-sonnet-4-6",
    2: "claude-sonnet-4-6",
    1: "claude-sonnet-4-6",
}
_ANTHROPIC_BUDGET   = "claude-haiku-4-5"
_ANTHROPIC_FAST     = "claude-haiku-4-5"
_ANTHROPIC_DEFAULT  = "claude-sonnet-4-6"

_OPENAI_BY_TIER = {
    4: "gpt-4o",
    3: "gpt-4o",
    2: "gpt-4o-mini",
    1: "gpt-4o-mini",
}
_OPENAI_BUDGET  = "gpt-4o-mini"
_OPENAI_FAST    = "gpt-4o-mini"
_OPENAI_DEFAULT = "gpt-4o-mini"

_ANTHROPIC_PLATFORMS = {"django_runtime", "bedrock"}
_OPENAI_PLATFORMS    = {"azure_ai_foundry", "openai"}

# Fast timeout threshold (seconds)
_FAST_TIMEOUT = 30


class ModelRouter:
    """
    Stateless model selector.  Returns a model ID string.
    All routing decisions are logged at DEBUG level.
    """

    def select(self, agent, task=None) -> str:
        """
        Select the best model for an agent step.

        Parameters
        ----------
        agent : Agent model instance
        task  : WorkflowTask model instance (optional; used for step-level overrides)
        """
        platform = (agent.platform or "").lower()
        is_openai = any(p in platform for p in _OPENAI_PLATFORMS)

        # 1. Step-level override (WorkflowTask.model_override)
        if task and getattr(task, "model_override", ""):
            chosen = task.model_override
            logger.debug("Router: step override → %s", chosen)
            return chosen

        # 2. Agent-level override
        if getattr(agent, "model_id", ""):
            chosen = agent.model_id
            logger.debug("Router: agent override → %s", chosen)
            return chosen

        risk_tier = getattr(agent, "risk_tier", 1)
        budget_alert = getattr(agent, "budget_alert", False)
        timeout = getattr(task, "timeout_seconds", 120) if task else 120

        # 3. Capability floor — Tier-4 (highest-risk: regulated/customer data,
        #    production systems) must always use the most capable model. Budget
        #    and latency pressure must NEVER silently downgrade a Tier-4 agent.
        if risk_tier >= 4:
            chosen = _OPENAI_BY_TIER[4] if is_openai else _ANTHROPIC_BY_TIER[4]
            logger.debug("Router: Tier-4 capability floor → %s", chosen)
            return chosen

        # 4. Budget pressure (Tiers 1–3 only)
        if budget_alert:
            chosen = _OPENAI_BUDGET if is_openai else _ANTHROPIC_BUDGET
            logger.debug("Router: budget pressure → %s", chosen)
            return chosen

        # 5. Fast-path for tight timeouts
        if timeout <= _FAST_TIMEOUT:
            chosen = _OPENAI_FAST if is_openai else _ANTHROPIC_FAST
            logger.debug("Router: fast timeout (%ss) → %s", timeout, chosen)
            return chosen

        # 6. Risk tier routing
        tier = max(1, min(4, risk_tier))
        if is_openai:
            chosen = _OPENAI_BY_TIER.get(tier, _OPENAI_DEFAULT)
        else:
            chosen = _ANTHROPIC_BY_TIER.get(tier, _ANTHROPIC_DEFAULT)

        logger.debug("Router: tier %s → %s", tier, chosen)
        return chosen

    def explain(self, agent, task=None) -> dict:
        """Return the routing decision with reasoning (for API / debug endpoint)."""
        model_id = self.select(agent, task)
        return {
            "model_id": model_id,
            "agent_slug": agent.slug,
            "risk_tier": getattr(agent, "risk_tier", 1),
            "budget_alert": getattr(agent, "budget_alert", False),
            "step_override": (task.model_override if task and getattr(task, "model_override", "") else None),
            "agent_override": (agent.model_id if getattr(agent, "model_id", "") else None),
        }


# Module-level singleton
model_router = ModelRouter()
