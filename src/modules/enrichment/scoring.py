"""Pure scoring function — combines LLM scores, recency, and stack match.

Zero I/O dependencies.  This is the function the user will tune most
often, so readability and clear weights are paramount.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from domain.models import EnrichmentResult


def compute_final_score(
    result: EnrichmentResult,
    posted_at: datetime | None,
    user_skills: list[str],
) -> float:
    """Compute a 0-100 score from LLM classification + post metadata.

    Returns a float clamped to [0, 100] with 2 decimal places.
    """
    # --- Component weights (must sum to 1.0) ---
    # These weights reflect how much each factor matters for
    # lead quality.  Tune them as you learn what converts.
    W_SIGNAL = 0.20    # How strong is the buying signal?
    W_ICP = 0.25       # Does this lead match our ideal customer profile?
    W_URGENCY = 0.15   # How time-sensitive is their need?
    W_DM = 0.15        # Is the poster a decision-maker?
    W_STACK = 0.15     # Do their tools overlap with our skills?
    W_RECENCY = 0.10   # How fresh is the post?

    signal_score = result.refined_signal_strength
    icp_score = result.icp_fit_score
    urgency_score = result.urgency_score
    dm_score = result.decision_maker_likelihood

    stack_score = _stack_match_score(result.extracted_stack, user_skills)
    recency_score = _recency_score(posted_at)

    raw = (
        W_SIGNAL * signal_score
        + W_ICP * icp_score
        + W_URGENCY * urgency_score
        + W_DM * dm_score
        + W_STACK * stack_score
        + W_RECENCY * recency_score
    )

    # Skip penalty: if the LLM recommends skipping, hard-cap at 15
    # so skips still appear in reports but never surface as actionable.
    if result.recommended_approach == "skip":
        raw = min(raw, 15.0)

    return round(_clamp(raw, 0.0, 100.0), 2)


def _stack_match_score(extracted: list[str], user_skills: list[str]) -> float:
    """Score 0-100 based on overlap between lead's stack and user's skills.

    A single match is already valuable — we use sqrt scaling so
    diminishing returns kick in after a few matches.
    """
    if not user_skills:
        return 50.0  # neutral when user hasn't configured skills

    extracted_lower = {s.lower() for s in extracted}
    skills_lower = {s.lower() for s in user_skills}
    overlap = len(extracted_lower & skills_lower)

    if overlap == 0:
        return 10.0  # some leads are worth pursuing even without stack match

    # sqrt scaling: 1 match ≈ 50, 2 ≈ 71, 4 ≈ 100
    ratio = overlap / len(skills_lower)
    return min(100.0, math.sqrt(ratio) * 100.0)


def _recency_score(posted_at: datetime | None) -> float:
    """Score 0-100 based on post age. Fresher = higher.

    Linear decay: 100 at 0h, 50 at 24h, 0 at 72h.
    """
    if posted_at is None:
        return 50.0  # unknown age gets neutral score

    age_hours = (datetime.now(timezone.utc) - posted_at).total_seconds() / 3600

    if age_hours <= 0:
        return 100.0
    if age_hours >= 72:
        return 0.0

    # Linear interpolation: 100 → 0 over 72 hours
    return max(0.0, 100.0 * (1 - age_hours / 72.0))


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
