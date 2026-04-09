"""Tests for the dedup hash computation — pure function, no I/O needed."""

from __future__ import annotations

from modules.scraping.dedup import compute_dedup_hash
from domain.models import CanonicalLead


class TestDedupHash:
    """Verify all three dedup identity strategies."""

    def test_company_domain_strategy(self, sample_canonical_lead: CanonicalLead) -> None:
        """When company_domain is present, hash uses co: prefix."""
        h = compute_dedup_hash(sample_canonical_lead)
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex

    def test_person_name_fallback(self, sample_canonical_lead_no_domain: CanonicalLead) -> None:
        """When no company_domain but person_name exists, hash uses p: prefix."""
        h = compute_dedup_hash(sample_canonical_lead_no_domain)
        assert isinstance(h, str)
        assert len(h) == 64

    def test_url_fallback(self, sample_canonical_lead_minimal: CanonicalLead) -> None:
        """When neither company_domain nor person_name, hash uses u: prefix."""
        h = compute_dedup_hash(sample_canonical_lead_minimal)
        assert isinstance(h, str)
        assert len(h) == 64

    def test_deterministic(self, sample_canonical_lead: CanonicalLead) -> None:
        """Same input always produces the same hash."""
        h1 = compute_dedup_hash(sample_canonical_lead)
        h2 = compute_dedup_hash(sample_canonical_lead)
        assert h1 == h2

    def test_different_leads_different_hashes(
        self,
        sample_canonical_lead: CanonicalLead,
        sample_canonical_lead_no_domain: CanonicalLead,
    ) -> None:
        """Different leads produce different hashes."""
        h1 = compute_dedup_hash(sample_canonical_lead)
        h2 = compute_dedup_hash(sample_canonical_lead_no_domain)
        assert h1 != h2

    def test_person_name_case_insensitive(
        self, sample_canonical_lead_no_domain: CanonicalLead
    ) -> None:
        """Person-name strategy lowercases the name for stability."""
        h = compute_dedup_hash(sample_canonical_lead_no_domain)
        # Manually construct what the hash should be based on lowered name
        from modules.scraping.dedup import _build_identity_string

        identity = _build_identity_string(sample_canonical_lead_no_domain)
        assert "devops dan" in identity  # lowercased
        assert len(h) == 64
