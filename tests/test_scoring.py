"""Table-driven tests for the pure scoring function."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domain.models import EnrichmentResult
from modules.enrichment.scoring import (
    _recency_score,
    _stack_match_score,
    compute_final_score,
)


def _make_result(
    *,
    strength: int = 70,
    icp: int = 70,
    urgency: int = 70,
    dm: int = 70,
    stack: list[str] | None = None,
    approach: str = "cold_dm",
    skip_reason: str | None = None,
) -> EnrichmentResult:
    return EnrichmentResult(
        refined_signal_type="pain_point",
        refined_signal_strength=strength,
        company_stage="startup",
        decision_maker_likelihood=dm,
        urgency_score=urgency,
        icp_fit_score=icp,
        extracted_stack=stack or ["python", "fastapi"],
        pain_summary="Test pain",
        recommended_approach=approach,
        skip_reason=skip_reason,
    )


class TestComputeFinalScore:
    """Verify scoring logic across a range of inputs."""

    @pytest.mark.parametrize(
        "strength, icp, urgency, dm, approach, expected_min, expected_max",
        [
            # High-quality lead: all scores high
            (90, 90, 90, 90, "cold_dm", 75, 100),
            # Low-quality lead: all scores low
            (10, 10, 10, 10, "cold_dm", 0, 25),
            # Medium lead
            (50, 50, 50, 50, "value_first_loom", 30, 60),
            # Skip recommendation: hard-capped at 15
            (90, 90, 90, 90, "skip", 0, 15),
            # Skip with low scores
            (10, 10, 10, 10, "skip", 0, 15),
            # Strong signal but no DM
            (90, 80, 70, 5, "comment_then_dm", 40, 75),
            # Great ICP fit but weak signal
            (10, 95, 20, 50, "cold_dm", 25, 60),
            # Urgent but everything else weak
            (15, 15, 95, 15, "cold_dm", 15, 45),
        ],
        ids=[
            "all_high",
            "all_low",
            "medium",
            "skip_high",
            "skip_low",
            "strong_signal_no_dm",
            "great_icp_weak_signal",
            "urgent_weak_rest",
        ],
    )
    def test_score_ranges(
        self,
        strength: int,
        icp: int,
        urgency: int,
        dm: int,
        approach: str,
        expected_min: float,
        expected_max: float,
    ) -> None:
        result = _make_result(
            strength=strength, icp=icp, urgency=urgency, dm=dm, approach=approach
        )
        score = compute_final_score(
            result,
            posted_at=datetime.now(UTC),  # fresh post
            user_skills=["python", "fastapi"],
        )
        assert expected_min <= score <= expected_max, (
            f"Score {score} not in [{expected_min}, {expected_max}]"
        )

    def test_none_posted_at_uses_neutral_recency(self) -> None:
        """Missing posted_at should not crash, uses neutral 50."""
        result = _make_result()
        score = compute_final_score(result, posted_at=None, user_skills=["python"])
        assert 0 <= score <= 100

    def test_old_post_penalized(self) -> None:
        """A 3-day-old post should score lower than a fresh one."""
        result = _make_result()
        skills = ["python", "fastapi"]
        fresh = compute_final_score(result, datetime.now(UTC), skills)
        old = compute_final_score(
            result,
            datetime.now(UTC) - timedelta(hours=72),
            skills,
        )
        assert fresh > old

    def test_empty_user_skills_neutral(self) -> None:
        """No configured skills should give neutral stack score."""
        result = _make_result()
        score = compute_final_score(result, datetime.now(UTC), user_skills=[])
        assert 0 <= score <= 100

    def test_no_stack_overlap_low_stack_score(self) -> None:
        """Zero overlap between lead stack and user skills."""
        result = _make_result(stack=["ruby", "rails"])
        score = compute_final_score(
            result,
            datetime.now(UTC),
            user_skills=["python", "fastapi"],
        )
        # Should still be a valid score, just lower stack component
        assert 0 <= score <= 100


class TestStackMatchScore:
    """Verify the stack overlap scoring helper."""

    def test_full_overlap(self) -> None:
        assert _stack_match_score(["python", "fastapi"], ["python", "fastapi"]) == 100.0

    def test_no_overlap(self) -> None:
        assert _stack_match_score(["ruby"], ["python"]) == 10.0

    def test_partial_overlap(self) -> None:
        score = _stack_match_score(["python", "ruby"], ["python", "fastapi", "react"])
        assert 10 < score < 100

    def test_empty_skills_neutral(self) -> None:
        assert _stack_match_score(["python"], []) == 50.0

    def test_case_insensitive(self) -> None:
        assert _stack_match_score(["Python", "FASTAPI"], ["python", "fastapi"]) == 100.0


class TestRecencyScore:
    """Verify the recency decay function."""

    def test_fresh_post(self) -> None:
        assert _recency_score(datetime.now(UTC)) == pytest.approx(100.0, abs=1)

    def test_24h_old(self) -> None:
        posted = datetime.now(UTC) - timedelta(hours=24)
        score = _recency_score(posted)
        assert 60 < score < 70  # ~66.7

    def test_72h_old(self) -> None:
        posted = datetime.now(UTC) - timedelta(hours=72)
        assert _recency_score(posted) == 0.0

    def test_very_old(self) -> None:
        posted = datetime.now(UTC) - timedelta(days=30)
        assert _recency_score(posted) == 0.0

    def test_none_returns_neutral(self) -> None:
        assert _recency_score(None) == 50.0
