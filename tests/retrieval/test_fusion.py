from __future__ import annotations

from typing import Any, cast

import pytest

from src.retrieval.dense import DenseSearchHit
from src.retrieval.fusion import FusionError, reciprocal_rank_fusion
from src.retrieval.lexical import LexicalHit


def _lexical(
    case_id: str,
    rank: int,
    *,
    chunk_id: str | None = None,
    matched_terms: tuple[str, ...] = (),
) -> LexicalHit:
    return LexicalHit(
        chunk_id=chunk_id or f"lex-{case_id}-{rank}",
        case_id=case_id,
        doc_id=f"doc-{case_id}",
        score=10.0 - rank,
        matched_terms=matched_terms,
        review_status="approved",
        answer_eligible=True,
    )


def _dense(case_id: str, rank: int, *, chunk_id: str | None = None) -> DenseSearchHit:
    return DenseSearchHit(
        point_id=f"point-{case_id}-{rank}",
        chunk_id=chunk_id or f"dense-{case_id}-{rank}",
        case_id=case_id,
        score=1.0 - rank / 100.0,
    )


def test_rrf_uses_k_60_and_groups_each_parent_once() -> None:
    lexical = (_lexical("case-a", 1), _lexical("case-b", 2))
    dense = (_dense("case-b", 1), _dense("case-a", 2))

    fused = reciprocal_rank_fusion(lexical, dense, k=60, limit=8)

    assert [hit.case_id for hit in fused] == ["case-a", "case-b"]
    assert len({hit.case_id for hit in fused}) == len(fused)
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert fused[0].chunk_ids == ("lex-case-a-1", "dense-case-a-2")


def test_only_the_best_child_rank_contributes_for_each_backend() -> None:
    lexical = (
        _lexical("case-a", 1, chunk_id="a-best"),
        _lexical("case-a", 2, chunk_id="a-lower"),
    )

    fused = reciprocal_rank_fusion(lexical, (), k=60, limit=8)

    assert len(fused) == 1
    assert fused[0].score == pytest.approx(1 / 61)
    assert fused[0].chunk_ids == ("a-best",)


def test_exact_business_token_boost_is_additive_but_year_is_not() -> None:
    lexical = (
        _lexical(
            "senqa-2024-contract-general-1",
            1,
            matched_terms=("senqa-2024-contract-general-1", "제12조", "1,502,000원"),
        ),
        _lexical("senqa-2025-contract-general-1", 2),
    )

    fused = reciprocal_rank_fusion(
        lexical,
        (),
        exact_tokens=("senqa-2024-contract-general-1", "제12조", "1,502,000원"),
        k=60,
        limit=8,
    )

    assert fused[0].case_id == "senqa-2024-contract-general-1"
    assert fused[0].exact_boost > 0
    assert fused[1].exact_boost == 0
    assert all("2025" not in term for term in fused[1].matched_terms)


@pytest.mark.parametrize(
    ("k", "limit"),
    (
        pytest.param(59, 8, id="wrong-k"),
        pytest.param(60, 0, id="zero-limit"),
        pytest.param(60, 9, id="too-many-parents"),
    ),
)
def test_fusion_contract_is_fixed_and_bounded(k: int, limit: int) -> None:
    with pytest.raises(FusionError, match="fusion_invalid") as captured:
        reciprocal_rank_fusion((_lexical("case-a", 1),), (), k=k, limit=limit)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("lexical", "exact_tokens"),
    (
        pytest.param(
            (
                _lexical(
                    "case-a",
                    1,
                    matched_terms=tuple(f"term-{index}" for index in range(65)),
                ),
            ),
            (),
            id="too-many-matched-terms",
        ),
        pytest.param(
            (_lexical("case-a", 1, matched_terms=("x" * 201,)),),
            (),
            id="oversized-matched-term",
        ),
        pytest.param(
            (_lexical("case-a", 1, matched_terms=("제12조",)),),
            ("제12조", "제12조"),
            id="duplicate-exact-token",
        ),
        pytest.param(
            (_lexical("case-a", 1, matched_terms=(cast(Any, []),)),),
            (),
            id="unhashable-matched-term",
        ),
        pytest.param(
            (_lexical("case-a", 1),),
            (cast(Any, []),),
            id="unhashable-exact-token",
        ),
    ),
)
def test_fusion_rejects_unbounded_or_ambiguous_match_metadata(
    lexical: tuple[LexicalHit, ...], exact_tokens: tuple[str, ...]
) -> None:
    with pytest.raises(FusionError, match="fusion_invalid") as captured:
        reciprocal_rank_fusion(lexical, (), exact_tokens=exact_tokens)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_fusion_accepts_the_largest_supported_exact_law_name() -> None:
    law_name = "「" + "가" * 120 + "」"

    fused = reciprocal_rank_fusion(
        (_lexical("case-a", 1, matched_terms=(law_name,)),),
        (),
        exact_tokens=(law_name,),
    )

    assert fused[0].matched_terms == (law_name,)
    assert fused[0].exact_boost == pytest.approx(0.04)


@pytest.mark.parametrize(
    ("lexical", "dense"),
    (
        pytest.param((_lexical("x" * 257, 1),), (), id="lexical-case-id"),
        pytest.param((), (_dense("x" * 257, 1),), id="dense-case-id"),
    ),
)
def test_fusion_rejects_oversized_backend_identifiers(
    lexical: tuple[LexicalHit, ...], dense: tuple[DenseSearchHit, ...]
) -> None:
    with pytest.raises(FusionError, match="fusion_invalid") as captured:
        reciprocal_rank_fusion(lexical, dense)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
