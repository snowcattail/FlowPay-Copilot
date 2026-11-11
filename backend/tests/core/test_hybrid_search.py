"""RRF and exact token coverage without an external PostgreSQL service."""

from flowpay.core.knowledge.contracts import Passage, PassageSource
from flowpay.core.knowledge.store import exact_terms, reciprocal_rank_fusion


def passage(name: str) -> Passage:
    return Passage(
        f"doc:{name}#c001", name, name, 0.5,
        PassageSource("collection", name, 1, f"{name}.md", "a" * 64, {}, {}),
    )


def test_rrf_boosts_overlap_and_preserves_original_passages() -> None:
    first, second, third = (passage(name) for name in ("first", "second", "third"))
    result = reciprocal_rank_fusion([first, second], [third, second], 2)
    assert result == [second, third]
    assert result[0] is second
    assert result[0].score == 0.5


def test_rrf_prioritizes_exact_hits_and_top_k() -> None:
    first, second = passage("first"), passage("second")
    assert reciprocal_rank_fusion([first], [second], 1) == [second]


def test_exact_terms_cover_identifiers_and_error_codes() -> None:
    assert exact_terms("customer_id FP-4021 order_123 status") == [
        "customer_id", "FP-4021", "order_123"
    ]
