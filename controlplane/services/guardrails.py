"""
GuardrailService — pre-run content scanner for prompt injection and PII.

Every agent message passes through scan() before reaching the LLM adapter.
The service checks for:

  1. Prompt-injection patterns — role-override, jailbreak, instruction-smuggling
  2. PII leakage — credit-card numbers, SSNs, passport-style IDs
  3. System-prompt override attempts — [SYSTEM], <|im_start|>, etc.

Per-agent guardrail level (stored on Agent.guardrail_level):
  "off"   — scan but never block; log only
  "warn"  — scan; yield warning event; continue run
  "block" — scan; raise GuardrailBlock on HIGH findings; run is aborted

All findings are written to AuditLog regardless of level.
"""
import re
import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ── Severity ─────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"


# ── Finding ───────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    rule_id:  str
    severity: Severity
    detail:   str
    matched:  str = ""   # the redacted snippet that triggered the rule


# ── Exception ─────────────────────────────────────────────────────────────────

class GuardrailBlock(RuntimeError):
    """Raised when a HIGH-severity finding is detected and level == 'block'."""
    def __init__(self, findings: list[Finding]):
        self.findings = findings
        details = "; ".join(f.detail for f in findings)
        super().__init__(f"Run blocked by guardrails: {details}")


# ── Rules ─────────────────────────────────────────────────────────────────────
# Each rule: (rule_id, severity, compiled_regex, human detail)

_RULES: list[tuple[str, Severity, re.Pattern, str]] = []


def _rule(rule_id: str, severity: Severity, pattern: str, detail: str,
          flags: int = re.IGNORECASE) -> None:
    _RULES.append((rule_id, severity, re.compile(pattern, flags), detail))


# ── Prompt injection / role-override ─────────────────────────────────────────
_rule("PI-001", Severity.HIGH,
      r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|context)",
      "Instruction-override attempt detected")

_rule("PI-002", Severity.HIGH,
      r"\bact\s+as\s+(if\s+you\s+(are|were)\s+)?(a\s+)?(jailbreak|dan|evil|unrestricted|unfiltered)",
      "Jailbreak persona request detected")

_rule("PI-003", Severity.HIGH,
      r"(you\s+are\s+now|from\s+now\s+on\s+you\s+are|pretend\s+you\s+are)\s+.{0,60}"
      r"(no\s+restrictions?|unrestricted|no\s+limits?|without\s+rules?)",
      "Unrestricted-mode injection attempt")

_rule("PI-004", Severity.HIGH,
      r"<\|im_start\|>|<\|im_end\|>|\[INST\]|\[/INST\]|\{\{system\}\}",
      "LLM special-token injection detected")

_rule("PI-005", Severity.HIGH,
      r"(?:^|\n)\s*\[SYSTEM\]|\bSYSTEM\s*PROMPT\s*:|###\s*System\s*:",
      "System-section header injection")

_rule("PI-006", Severity.MEDIUM,
      r"(repeat|print|output|reveal|show|display)\s+(your\s+)?(system\s+prompt|instructions?|context|rules)",
      "System-prompt extraction attempt")

_rule("PI-007", Severity.MEDIUM,
      r"bypass\s+(the\s+)?(filter|guardrail|safety|restriction|content\s+polic)",
      "Guardrail bypass attempt")

_rule("PI-008", Severity.MEDIUM,
      r"developer\s+mode|maintenance\s+mode|god\s+mode|debug\s+mode",
      "Mode-override jailbreak attempt")

# ── PII patterns ──────────────────────────────────────────────────────────────
_rule("PII-001", Severity.HIGH,
      r"\b(?:\d[ -]?){13,16}\b",
      "Possible credit/debit card number detected")

_rule("PII-002", Severity.HIGH,
      r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b",
      "Possible US Social Security Number detected")

_rule("PII-003", Severity.MEDIUM,
      r"\b[A-Z]{1,2}\d{6,9}\b",
      "Possible passport / national ID number detected")

_rule("PII-004", Severity.LOW,
      r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
      "Email address detected in input")

# ── Data exfiltration ─────────────────────────────────────────────────────────
_rule("EX-001", Severity.HIGH,
      r"(send|email|post|upload|exfil(trate)?|transmit)\s+.{0,40}"
      r"(password|secret|key|token|credential)",
      "Potential credential exfiltration attempt")

_rule("EX-002", Severity.MEDIUM,
      r"curl\s+https?://|wget\s+https?://|fetch\s*\(\s*['\"]https?://",
      "Outbound HTTP call embedded in user message")


# ── Service ───────────────────────────────────────────────────────────────────

class GuardrailService:
    """
    Stateless.  Call scan() before every agent run.

    Usage::

        from controlplane.services.guardrails import guardrails, GuardrailBlock

        try:
            findings = guardrails.scan(
                message=user_message,
                agent=agent,
                actor=user_label,
                run_id=str(run.id),
                ip=ip_address,
            )
        except GuardrailBlock as exc:
            # abort the run, yield an error event
            ...
    """

    def scan(
        self,
        *,
        message: str,
        agent,                  # Agent model instance
        actor: str = "unknown",
        run_id: str = "",
        ip: str | None = None,
    ) -> list[Finding]:
        """
        Scan ``message`` against all rules.

        Returns the list of findings (may be empty).
        Raises GuardrailBlock if level == 'block' and any HIGH finding exists.
        Always writes HIGH/MEDIUM findings to AuditLog.
        """
        findings = self._run_rules(message)
        if not findings:
            return []

        level = getattr(agent, "guardrail_level", "block")  # default safe
        self._audit(findings, agent, actor, run_id, ip)

        high = [f for f in findings if f.severity == Severity.HIGH]
        if high and level == "block":
            raise GuardrailBlock(high)

        if findings:
            logger.warning(
                "Guardrail findings for agent=%s run=%s level=%s findings=%s",
                agent.slug, run_id, level,
                [f.rule_id for f in findings],
            )

        return findings

    # ── Internal ─────────────────────────────────────────────────────────────

    @staticmethod
    def _run_rules(message: str) -> list[Finding]:
        findings: list[Finding] = []
        for rule_id, severity, pattern, detail in _RULES:
            m = pattern.search(message)
            if m:
                # Redact: replace actual match with [REDACTED] in snippet
                raw = m.group(0)
                snippet = raw[:40] + ("…" if len(raw) > 40 else "")
                findings.append(Finding(
                    rule_id=rule_id,
                    severity=severity,
                    detail=detail,
                    matched=f"[REDACTED:{len(raw)}chars]",
                ))
        return findings

    @staticmethod
    def _audit(findings: list[Finding], agent, actor: str,
               run_id: str, ip: str | None) -> None:
        # Lazy import to avoid circular at module level
        from controlplane.models import AuditLog
        noteworthy = [f for f in findings if f.severity in (Severity.HIGH, Severity.MEDIUM)]
        if not noteworthy:
            return
        AuditLog.objects.create(
            actor=actor,
            action="guardrail.finding",
            resource_type="Agent",
            resource_id=str(agent.id),
            payload={
                "run_id": run_id,
                "agent": agent.slug,
                "findings": [
                    {"rule": f.rule_id, "severity": f.severity, "detail": f.detail}
                    for f in noteworthy
                ],
            },
            ip_address=ip,
        )


# Module-level singleton
guardrails = GuardrailService()
