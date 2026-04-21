"""Signal classification and keyword extraction.

Exposes a ``SignalClassifier`` object for per-request customisation
(n8n workflows pass their own patterns/keywords), and a
``DEFAULT_CLASSIFIER`` for the dev-focused defaults used when no
overrides are supplied.

The module-level ``classify_signal()`` / ``extract_stack()`` /
``extract_domain()`` functions remain for back-compat; they delegate
to ``DEFAULT_CLASSIFIER``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from domain.models import SignalType

# ---------------------------------------------------------------------------
# Default dev-focused patterns (current behavior preserved)
# ---------------------------------------------------------------------------

_P = re.compile

_DEFAULT_SIGNAL_PATTERNS: tuple[tuple[re.Pattern[str], SignalType, int], ...] = (
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
)

DEFAULT_KEYWORDS: list[str] = [
    "python", "javascript", "typescript", "react", "vue", "angular", "node",
    "django", "flask", "fastapi", "rails", "ruby", "go", "golang", "rust",
    "java", "kotlin", "swift", "postgres", "mysql", "mongodb", "redis",
    "elasticsearch", "kafka", "rabbitmq", "docker", "kubernetes", "aws",
    "gcp", "azure", "terraform", "ansible", "jenkins", "github actions",
    "graphql", "rest", "grpc", "nextjs", "svelte", "tailwind", "prisma",
]

_DOMAIN_PATTERN = re.compile(
    r"\b(?:at|from|our\s+site|check\s+out)"
    r"\s+([\w-]+\.(?:com|io|co|dev|ai|org|net))\b",
    re.IGNORECASE,
)


def _compile_keyword_pattern(keywords: list[str]) -> re.Pattern[str] | None:
    """Build a single regex that matches any of the given keywords."""
    if not keywords:
        return None
    return re.compile(
        r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b",
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# Runtime-configurable classifier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalClassifier:
    """Classifies text into (SignalType, strength) and extracts keywords.

    Built per-request from a ``ScrapeRequest`` or falls back to
    ``DEFAULT_CLASSIFIER`` when no overrides are supplied.
    """

    patterns: tuple[tuple[re.Pattern[str], SignalType, int], ...]
    keywords_pattern: re.Pattern[str] | None = None
    default_signal: tuple[SignalType, int] | None = None  # None => drop unclassified
    keywords: tuple[str, ...] = field(default_factory=tuple)

    def classify(self, text: str) -> tuple[SignalType | None, int | None]:
        """Return (signal_type, strength) for the first matching pattern."""
        for pattern, signal_type, strength in self.patterns:
            if pattern.search(text):
                return signal_type, strength
        if self.default_signal is not None:
            return self.default_signal
        return None, None

    def extract_keywords(self, text: str) -> list[str]:
        """Return unique, lowercased keyword mentions found in text."""
        if self.keywords_pattern is None:
            return []
        return sorted({m.lower() for m in self.keywords_pattern.findall(text)})


DEFAULT_CLASSIFIER = SignalClassifier(
    patterns=_DEFAULT_SIGNAL_PATTERNS,
    keywords_pattern=_compile_keyword_pattern(DEFAULT_KEYWORDS),
    default_signal=None,  # drop unclassified by default
    keywords=tuple(DEFAULT_KEYWORDS),
)


# ---------------------------------------------------------------------------
# Back-compat module-level functions (delegate to DEFAULT_CLASSIFIER)
# ---------------------------------------------------------------------------


def classify_signal(text: str) -> tuple[SignalType | None, int | None]:
    """Return first matching signal type + strength using default patterns."""
    return DEFAULT_CLASSIFIER.classify(text)


def extract_stack(text: str) -> list[str]:
    """Extract default tech-stack keyword mentions from text."""
    return DEFAULT_CLASSIFIER.extract_keywords(text)


def extract_domain(text: str) -> str | None:
    """Extract a company domain from patterns like 'at acme.io'."""
    match = _DOMAIN_PATTERN.search(text)
    return match.group(1).lower() if match else None


# ---------------------------------------------------------------------------
# Classifier builder — used by the orchestrator to turn a ScrapeRequest
# into a SignalClassifier.
# ---------------------------------------------------------------------------


def build_classifier(
    *,
    signal_patterns: list[tuple[str, str, int]] | None = None,
    keywords: list[str] | None = None,
    default_signal_type: str | None = None,
    default_signal_strength: int = 50,
    keep_unclassified: bool = False,
) -> SignalClassifier:
    """Build a SignalClassifier from raw request fields.

    When no overrides are supplied (all None/False), returns
    ``DEFAULT_CLASSIFIER`` unchanged.

    ``signal_patterns`` is a list of (pattern_str, signal_type_str, strength)
    tuples.  Invalid signal_type strings raise ValueError.
    """
    using_defaults = (
        signal_patterns is None
        and keywords is None
        and default_signal_type is None
        and not keep_unclassified
    )
    if using_defaults:
        return DEFAULT_CLASSIFIER

    if signal_patterns is not None:
        compiled: tuple[tuple[re.Pattern[str], SignalType, int], ...] = tuple(
            (re.compile(p, re.IGNORECASE), SignalType(s), strength)
            for p, s, strength in signal_patterns
        )
    else:
        compiled = _DEFAULT_SIGNAL_PATTERNS

    keyword_list = keywords if keywords is not None else DEFAULT_KEYWORDS
    keyword_pattern = _compile_keyword_pattern(keyword_list)

    default: tuple[SignalType, int] | None = None
    if keep_unclassified:
        signal_type = (
            SignalType(default_signal_type)
            if default_signal_type
            else SignalType.GENERAL_INTEREST
        )
        default = (signal_type, default_signal_strength)

    return SignalClassifier(
        patterns=compiled,
        keywords_pattern=keyword_pattern,
        default_signal=default,
        keywords=tuple(keyword_list),
    )
