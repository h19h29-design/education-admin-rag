"""Deterministic parent-level reciprocal-rank fusion for hybrid retrieval."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import NoReturn, cast

from src.retrieval.dense import DenseSearchHit
from src.retrieval.lexical import LexicalHit

_CASE_ID_RE = re.compile(r"^senqa-[0-9]{4}(?:-[a-z0-9]+){3,}$")
_ARTICLE_RE = re.compile(
    r"^제[0-9]+(?:조(?:의[0-9]+)?(?:제[0-9]+항)?(?:제[0-9]+호)?|항|호)$"
)
_NUMBER_RE = re.compile(
    r"^(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?(?:%|원|만원|억원|명|개|건|일|개월|월|년|시간|점|㎡|m²|kg|km)$"
)
_LAW_RE = re.compile(
    r"^(?:「[^「」\r\n]{1,120}」|[^「」\r\n]{2,}(?:법률|시행령|시행규칙|조례|규칙|법))$"
)
_MAX_BACKEND_HITS = 25
_MAX_EXACT_TOKENS = 64
_MAX_TERM_CHARACTERS = 200
_MAX_IDENTIFIER_CHARACTERS = 256


class FusionError(ValueError):
    """A fixed, value-free hybrid fusion contract failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _raise(code: str) -> NoReturn:
    raise FusionError(code) from None


@dataclass(frozen=True, slots=True)
class FusedHit:
    case_id: str
    score: float
    reciprocal_rank_score: float
    exact_boost: float
    chunk_ids: tuple[str, ...]
    matched_terms: tuple[str, ...]
    lexical_rank: int | None
    dense_rank: int | None


@dataclass(slots=True)
class _ParentAccumulator:
    reciprocal_rank_score: float = 0.0
    chunk_ids: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()
    lexical_rank: int | None = None
    dense_rank: int | None = None


def _checked_lexical_hit(value: object) -> LexicalHit | None:
    if type(value) is not LexicalHit:
        return None
    if (
        not _valid_identifier(value.case_id)
        or not _valid_identifier(value.chunk_id)
        or not _valid_identifier(value.doc_id)
        or type(value.score) is not float
        or not math.isfinite(value.score)
        or not _valid_exact_tokens(value.matched_terms)
        or value.review_status not in {"search_approved", "approved"}
        or type(value.answer_eligible) is not bool
    ):
        return None
    return value


def _checked_dense_hit(value: object) -> DenseSearchHit | None:
    if type(value) is not DenseSearchHit:
        return None
    if (
        not _valid_identifier(value.case_id)
        or not _valid_identifier(value.chunk_id)
        or not _valid_identifier(value.point_id)
        or type(value.score) is not float
        or not math.isfinite(value.score)
    ):
        return None
    return value


def _valid_identifier(value: object) -> bool:
    return (
        type(value) is str and bool(value) and len(value) <= _MAX_IDENTIFIER_CHARACTERS
    )


def _term_boost(term: str) -> float:
    if _CASE_ID_RE.fullmatch(term) is not None:
        return 0.05
    if _LAW_RE.fullmatch(term) is not None:
        return 0.04
    if _ARTICLE_RE.fullmatch(term) is not None:
        return 0.03
    if _NUMBER_RE.fullmatch(term) is not None:
        return 0.025
    return 0.0


def _valid_exact_tokens(value: object) -> bool:
    if type(value) is not tuple or len(value) > _MAX_EXACT_TOKENS:
        return False
    terms = cast(tuple[object, ...], value)
    if any(
        type(token) is not str or not token or len(token) > _MAX_TERM_CHARACTERS
        for token in terms
    ):
        return False
    checked_terms = cast(tuple[str, ...], terms)
    return len(set(checked_terms)) == len(checked_terms)


def calibrated_no_answer_threshold(exact_tokens: tuple[str, ...]) -> float:
    """Return the fixed question-type threshold without using result recency."""
    if not _valid_exact_tokens(exact_tokens):
        _raise("fusion_invalid")
    highest_boost = max((_term_boost(token) for token in exact_tokens), default=0.0)
    if highest_boost == 0.05:  # exact canonical case ID
        return 0.015
    if highest_boost >= 0.03:  # law name or article/paragraph/item
        return 0.020
    if highest_boost == 0.025:  # exact amount or other measured number
        return 0.022
    return 0.025


def reciprocal_rank_fusion(
    lexical_hits: tuple[LexicalHit, ...],
    dense_hits: tuple[DenseSearchHit, ...],
    *,
    exact_tokens: tuple[str, ...] = (),
    k: int = 60,
    limit: int = 8,
) -> tuple[FusedHit, ...]:
    """Fuse at parent level, using only the best child rank from each backend."""
    if (
        type(lexical_hits) is not tuple
        or len(lexical_hits) > _MAX_BACKEND_HITS
        or type(dense_hits) is not tuple
        or len(dense_hits) > _MAX_BACKEND_HITS
        or not _valid_exact_tokens(exact_tokens)
        or type(k) is not int
        or k != 60
        or type(limit) is not int
        or limit < 1
        or limit > 8
    ):
        _raise("fusion_invalid")
    parents: dict[str, _ParentAccumulator] = {}
    seen_lexical: set[str] = set()
    exact_set = set(exact_tokens)
    for rank, lexical_value in enumerate(lexical_hits, start=1):
        lexical_hit = _checked_lexical_hit(lexical_value)
        if lexical_hit is None:
            _raise("fusion_invalid")
        if lexical_hit.case_id in seen_lexical:
            continue
        seen_lexical.add(lexical_hit.case_id)
        parent = parents.setdefault(lexical_hit.case_id, _ParentAccumulator())
        parent.reciprocal_rank_score += 1.0 / (k + rank)
        parent.lexical_rank = rank
        parent.chunk_ids = (*parent.chunk_ids, lexical_hit.chunk_id)
        parent.matched_terms = tuple(
            token
            for token in exact_tokens
            if token in exact_set and token in lexical_hit.matched_terms
        )
    seen_dense: set[str] = set()
    for rank, dense_value in enumerate(dense_hits, start=1):
        dense_hit = _checked_dense_hit(dense_value)
        if dense_hit is None:
            _raise("fusion_invalid")
        if dense_hit.case_id in seen_dense:
            continue
        seen_dense.add(dense_hit.case_id)
        parent = parents.setdefault(dense_hit.case_id, _ParentAccumulator())
        parent.reciprocal_rank_score += 1.0 / (k + rank)
        parent.dense_rank = rank
        if dense_hit.chunk_id not in parent.chunk_ids:
            parent.chunk_ids = (*parent.chunk_ids, dense_hit.chunk_id)
    fused = tuple(
        FusedHit(
            case_id=case_id,
            score=parent.reciprocal_rank_score
            + sum(_term_boost(term) for term in parent.matched_terms),
            reciprocal_rank_score=parent.reciprocal_rank_score,
            exact_boost=sum(_term_boost(term) for term in parent.matched_terms),
            chunk_ids=parent.chunk_ids,
            matched_terms=parent.matched_terms,
            lexical_rank=parent.lexical_rank,
            dense_rank=parent.dense_rank,
        )
        for case_id, parent in parents.items()
    )
    return tuple(sorted(fused, key=lambda hit: (-hit.score, hit.case_id))[:limit])
