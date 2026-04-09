"""Deduplication hash computation — pure function, no I/O.

Hash strategy (ordered by priority):
1. ``co:{company_domain}:{signal_type}:{day_bucket}`` when company_domain is present
2. ``p:{lower(person_name)}:{signal_type}:{day_bucket}`` when person_name is present
3. ``u:{url}`` as the universal fallback

``day_bucket`` = ``floor(posted_at_unix / 86400)``
"""

from __future__ import annotations

import hashlib
import math

from domain.models import CanonicalLead


def compute_dedup_hash(lead: CanonicalLead) -> str:
    """Return a SHA-256 hex digest that uniquely identifies a lead for dedup."""
    identity = _build_identity_string(lead)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _build_identity_string(lead: CanonicalLead) -> str:
    """Construct the stable identity string used as hash input."""
    signal = lead.signal_type.value if lead.signal_type else "unknown"
    day_bucket = _day_bucket(lead)

    if lead.company_domain:
        return f"co:{lead.company_domain}:{signal}:{day_bucket}"
    if lead.person_name:
        return f"p:{lead.person_name.lower()}:{signal}:{day_bucket}"
    return f"u:{lead.url}"


def _day_bucket(lead: CanonicalLead) -> int:
    """Floor division of posted_at unix timestamp by 86400."""
    if lead.posted_at is None:
        return 0
    return math.floor(lead.posted_at.timestamp() / 86400)
