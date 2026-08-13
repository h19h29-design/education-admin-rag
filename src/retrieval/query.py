"""Bounded Korean lexical-query normalization with separate access filters."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, NoReturn, Self

CaseType = Literal["qa", "audit", "law_index", "credits"]
AccessLevel = Literal["public", "staff"]

_MAX_QUERY_CHARACTERS = 2_048
_MAX_FILTER_VALUES = 32
_MAX_EXACT_TOKENS = 64
_MAX_NGRAMS = 32_768
_CASE_TYPES = frozenset(("qa", "audit", "law_index", "credits"))
_ACCESS_LEVELS = frozenset(("public", "staff"))
_EXACT_TOKEN_RE = re.compile(
    r"senqa-[0-9]{4}(?:-[a-z0-9]+){3,}"
    r"|「[^「」\r\n]{1,120}」"
    r"|제[0-9]+조(?:의[0-9]+)?(?:제[0-9]+항)?(?:제[0-9]+호)?"
    r"|제[0-9]+(?:항|호)"
    r"|(?<![0-9])[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?(?:원|만원|억원)?"
    r"|(?<![0-9])[0-9]+(?:\.[0-9]+)?%"
    r"|(?<![0-9])[0-9]+(?:\.[0-9]+)?(?:원|만원|억원|명|개|건|일|개월|월|년|시간|점|㎡|m²|kg|km)"
    r"|[가-힣A-Za-z]{2,}(?:법률|시행령|시행규칙|조례|규칙|법)"
)


class QueryError(ValueError):
    """A value-free query contract error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _raise(code: str) -> NoReturn:
    raise QueryError(code) from None


def _deduplicate_sorted_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    if (
        type(values) is not tuple
        or len(values) > _MAX_FILTER_VALUES
        or any(
            type(value) is not str
            or not value
            or len(value) > 120
            or value != unicodedata.normalize("NFC", value)
            or any(character.isspace() for character in value)
            for value in values
        )
    ):
        _raise("query_filters_invalid")
    return tuple(sorted(set(values)))


@dataclass(frozen=True, slots=True)
class QueryFilters:
    """Structured filters that never enter the FTS MATCH expression."""

    years: tuple[int, ...] = ()
    domains: tuple[str, ...] = ()
    case_types: tuple[CaseType, ...] = ()
    access_level: AccessLevel = "public"

    @classmethod
    def create(
        cls,
        *,
        years: tuple[int, ...] = (),
        domains: tuple[str, ...] = (),
        case_types: tuple[CaseType, ...] = (),
        access_level: AccessLevel = "public",
    ) -> Self:
        if (
            type(years) is not tuple
            or len(years) > _MAX_FILTER_VALUES
            or any(
                type(year) is not int or year < 1900 or year > 2100 for year in years
            )
            or type(case_types) is not tuple
            or any(
                type(value) is not str or value not in _CASE_TYPES
                for value in case_types
            )
            or type(access_level) is not str
            or access_level not in _ACCESS_LEVELS
        ):
            _raise("query_filters_invalid")
        checked_domains = _deduplicate_sorted_strings(domains)
        return cls(
            years=tuple(sorted(set(years))),
            domains=checked_domains,
            case_types=tuple(sorted(set(case_types))),
            access_level=access_level,
        )


@dataclass(frozen=True, slots=True)
class NormalizedQuery:
    text: str
    exact_tokens: tuple[str, ...]
    char_ngrams: tuple[str, ...]
    match_expression: str
    filters: QueryFilters


def exact_tokens(text: str) -> tuple[str, ...]:
    """Return exact business tokens in first-occurrence order."""
    seen: set[str] = set()
    tokens: list[str] = []
    for match in _EXACT_TOKEN_RE.finditer(text):
        token = match.group(0)
        if token not in seen:
            seen.add(token)
            tokens.append(token)
        if len(tokens) > _MAX_EXACT_TOKENS:
            _raise("query_invalid")
    return tuple(tokens)


def character_ngrams(text: str) -> tuple[str, ...]:
    """Generate deterministic 2- and 3-character lexical grams across spacing variants."""
    compact = "".join(
        character
        for character in text
        if unicodedata.category(character)[0] in {"L", "N"}
    )
    grams: set[str] = set()
    for width in (2, 3):
        grams.update(
            compact[index : index + width]
            for index in range(max(0, len(compact) - width + 1))
        )
    return tuple(sorted(grams)[:_MAX_NGRAMS])


def _fts_phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _match_expression(text: str, exact: tuple[str, ...], grams: tuple[str, ...]) -> str:
    clauses: list[str] = []
    clauses.extend(f"exact_tokens:{_fts_phrase(token)}" for token in exact)
    clauses.extend(f"char_ngrams:{_fts_phrase(gram)}" for gram in grams)
    clauses.extend(
        "{" + "title question law_names body" + "}:" + _fts_phrase(term)
        for term in text.split()
        if term not in exact
    )
    if not clauses:
        _raise("query_invalid")
    return " OR ".join(clauses)


def normalize_query(
    value: str, *, filters: QueryFilters | None = None
) -> NormalizedQuery:
    """Normalize a query without rewriting its business-significant values."""
    if type(value) is not str or not value or len(value) > _MAX_QUERY_CHARACTERS:
        _raise("query_invalid")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co", "Cn"}
        and not character.isspace()
        for character in value
    ):
        _raise("query_invalid")
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if not text:
        _raise("query_invalid")
    checked_filters = QueryFilters.create() if filters is None else filters
    if type(checked_filters) is not QueryFilters:
        _raise("query_filters_invalid")
    # Rebuild even exact instances so model_construct-style dataclass mutation cannot bypass checks.
    checked_filters = QueryFilters.create(
        years=checked_filters.years,
        domains=checked_filters.domains,
        case_types=checked_filters.case_types,
        access_level=checked_filters.access_level,
    )
    exact = exact_tokens(text)
    grams = character_ngrams(text)
    return NormalizedQuery(
        text=text,
        exact_tokens=exact,
        char_ngrams=grams,
        match_expression=_match_expression(text, exact, grams),
        filters=checked_filters,
    )
