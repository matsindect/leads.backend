"""Shared signal classification, stack extraction, and domain extraction.

Used by all adapters that do text-based lead classification.
Pure functions — no I/O, no domain knowledge beyond pattern matching.
"""

from __future__ import annotations

import re

from domain.models import SignalType

# --- Signal classification patterns (ordered by priority) ---

_P = re.compile  # shorter alias for line length

SIGNAL_PATTERNS: list[tuple[re.Pattern[str], SignalType, int]] = [
    (_P(r"\b(hiring|looking\s+for|job\s+opening|we.re\s+hiring)\b", re.I),
     SignalType.HIRING, 60),
    (_P(r"\b(struggling\s+with|pain\s+point|frustrat|annoying|broken)\b", re.I),
     SignalType.PAIN_POINT, 70),
    (_P(r"\b(evaluat|compar|alternative\s+to|switch\s+from|looking\s+for\s+a\s+tool)\b", re.I),
     SignalType.TOOL_EVALUATION, 80),
    (_P(r"\b(budget|pricing|cost|expensive|afford)\b", re.I),
     SignalType.BUDGET_MENTION, 50),
    (_P(r"\b(expand|scale|scal|growing|growth)\b", re.I),
     SignalType.EXPANSION, 55),
    (_P(r"\b(migrat|moving\s+from|switch\s+to|replac)\b", re.I),
     SignalType.TECH_STACK_CHANGE, 75),
    (_P(r"\b(comply|compliance|gdpr|hipaa|soc\s*2|regulation)\b", re.I),
     SignalType.COMPLIANCE_NEED, 65),
    (_P(r"\b(raised|funding|seed|series\s+[a-c]|venture|investor)\b", re.I),
     SignalType.FUNDING, 85),
    (_P(r"\b(recommend|suggest|advice|help\s+with)\b", re.I),
     SignalType.GENERAL_INTEREST, 30),
]

STACK_KEYWORDS: list[str] = [
    "python", "javascript", "typescript", "react", "vue", "angular", "node",
    "django", "flask", "fastapi", "rails", "ruby", "go", "golang", "rust",
    "java", "kotlin", "swift", "postgres", "mysql", "mongodb", "redis",
    "elasticsearch", "kafka", "rabbitmq", "docker", "kubernetes", "aws",
    "gcp", "azure", "terraform", "ansible", "jenkins", "github actions",
    "graphql", "rest", "grpc", "nextjs", "svelte", "tailwind", "prisma",
]

_STACK_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in STACK_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

_DOMAIN_PATTERN = re.compile(
    r"\b(?:at|from|our\s+site|check\s+out)"
    r"\s+([\w-]+\.(?:com|io|co|dev|ai|org|net))\b",
    re.IGNORECASE,
)


def classify_signal(text: str) -> tuple[SignalType | None, int | None]:
    """Return the first matching signal type and its strength."""
    for pattern, signal_type, strength in SIGNAL_PATTERNS:
        if pattern.search(text):
            return signal_type, strength
    return None, None


def extract_stack(text: str) -> list[str]:
    """Extract unique technology mentions from text, sorted."""
    return sorted({m.lower() for m in _STACK_PATTERN.findall(text)})


def extract_domain(text: str) -> str | None:
    """Extract a company domain from patterns like 'at acme.io'."""
    match = _DOMAIN_PATTERN.search(text)
    return match.group(1).lower() if match else None
