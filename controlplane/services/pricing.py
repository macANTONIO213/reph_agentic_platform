"""
Pricing table for LLM models. Prices are USD per 1M tokens.
Normalize Bedrock/Azure model IDs to canonical keys before lookup.
"""
import re
from decimal import Decimal

# (input_per_1m_usd, output_per_1m_usd)
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    # Anthropic Claude
    "claude-opus-4-8":    (Decimal("5.00"),  Decimal("25.00")),
    "claude-opus-4-7":    (Decimal("5.00"),  Decimal("25.00")),
    "claude-opus-4-6":    (Decimal("5.00"),  Decimal("25.00")),
    "claude-sonnet-4-6":  (Decimal("3.00"),  Decimal("15.00")),
    "claude-sonnet-4-5":  (Decimal("3.00"),  Decimal("15.00")),
    "claude-haiku-4-5":   (Decimal("1.00"),  Decimal("5.00")),
    "claude-3-5-sonnet":  (Decimal("3.00"),  Decimal("15.00")),
    "claude-3-5-haiku":   (Decimal("0.80"),  Decimal("4.00")),
    "claude-3-opus":      (Decimal("15.00"), Decimal("75.00")),
    "claude-3-sonnet":    (Decimal("3.00"),  Decimal("15.00")),
    "claude-3-haiku":     (Decimal("0.25"),  Decimal("1.25")),
    # OpenAI / Azure
    "gpt-4o":             (Decimal("2.50"),  Decimal("10.00")),
    "gpt-4o-mini":        (Decimal("0.15"),  Decimal("0.60")),
    "gpt-4-turbo":        (Decimal("10.00"), Decimal("30.00")),
    "gpt-35-turbo":       (Decimal("0.50"),  Decimal("1.50")),
    # echo / fake / unknown — documented 0
    "fake":               (Decimal("0"),     Decimal("0")),
    "echo":               (Decimal("0"),     Decimal("0")),
}

# Order matters: longer/more-specific patterns first.
_NORMALIZERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"claude-opus-4-8"),           "claude-opus-4-8"),
    (re.compile(r"claude-opus-4-7"),           "claude-opus-4-7"),
    (re.compile(r"claude-opus-4-6"),           "claude-opus-4-6"),
    (re.compile(r"claude-sonnet-4-6"),         "claude-sonnet-4-6"),
    (re.compile(r"claude-sonnet-4-5"),         "claude-sonnet-4-5"),
    (re.compile(r"claude-haiku-4-5"),          "claude-haiku-4-5"),
    (re.compile(r"claude-3-5-sonnet"),         "claude-3-5-sonnet"),
    (re.compile(r"claude-3-5-haiku"),          "claude-3-5-haiku"),
    (re.compile(r"claude-3-opus"),             "claude-3-opus"),
    (re.compile(r"claude-3-sonnet"),           "claude-3-sonnet"),
    (re.compile(r"claude-3-haiku"),            "claude-3-haiku"),
    (re.compile(r"gpt-4o-mini"),               "gpt-4o-mini"),
    (re.compile(r"gpt-4o"),                    "gpt-4o"),
    (re.compile(r"gpt-4-turbo"),               "gpt-4-turbo"),
    (re.compile(r"gpt-35-turbo|gpt-3\.5"),     "gpt-35-turbo"),
    (re.compile(r"fake|echo"),                 "fake"),
]


def normalize_model_id(model_id: str) -> str:
    """Map a raw model ID (Bedrock ARN, Azure deployment name, etc.) to a pricing key."""
    s = (model_id or "").lower().strip()
    for pattern, canonical in _NORMALIZERS:
        if pattern.search(s):
            return canonical
    return s  # unknown → will price at $0


def price_run(input_tokens: int, output_tokens: int, model_id: str) -> Decimal:
    """Return cost in USD for a single run. Returns Decimal('0') for unknown models."""
    canonical = normalize_model_id(model_id)
    if canonical not in PRICING:
        return Decimal("0")
    input_price, output_price = PRICING[canonical]
    return (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / Decimal("1_000_000")
