import unicodedata

import pytest

from src.retrieval.query import QueryError, QueryFilters, normalize_query


def test_query_normalization_preserves_exact_korean_business_tokens() -> None:
    query = normalize_query(
        "  2단계  입찰  제12조제3항  1,502,000원  17.5%  "
        "「지방계약법」 senqa-2025-contract-contract-general-1  "
    )

    assert query.text == (
        "2단계 입찰 제12조제3항 1,502,000원 17.5% "
        "「지방계약법」 senqa-2025-contract-contract-general-1"
    )
    assert query.exact_tokens == (
        "제12조제3항",
        "1,502,000원",
        "17.5%",
        "「지방계약법」",
        "senqa-2025-contract-contract-general-1",
    )
    assert "2단" in query.char_ngrams
    assert "단계입" in query.char_ngrams
    assert "계입찰" in query.char_ngrams


def test_query_normalization_uses_nfc_and_only_collapses_whitespace() -> None:
    decomposed = unicodedata.normalize("NFD", "학교회계")
    query = normalize_query(f"{decomposed}\n\t  제3호")

    assert query.text == "학교회계 제3호"
    assert query.text == unicodedata.normalize("NFC", query.text)
    assert "제3호" in query.exact_tokens


def test_query_filters_remain_typed_and_outside_match_expression() -> None:
    filters = QueryFilters.create(
        years=(2025, 2024, 2025),
        domains=("계약", "재정", "계약"),
        case_types=("qa", "audit", "qa"),
        access_level="staff",
    )
    query = normalize_query("학교회계 2025", filters=filters)

    assert query.filters.years == (2024, 2025)
    assert query.filters.domains == ("계약", "재정")
    assert query.filters.case_types == ("audit", "qa")
    assert query.filters.access_level == "staff"
    assert "계약" not in query.match_expression
    assert "audit" not in query.match_expression


def test_query_defaults_to_public_access() -> None:
    assert normalize_query("학교회계").filters.access_level == "public"


def test_query_preserves_every_ngram_within_the_bounded_query_size() -> None:
    text = "".join(chr(0xAC00 + index) for index in range(600))

    query = normalize_query(text)

    assert len(query.char_ngrams) == (599 + 598)
    assert text[-2:] in query.char_ngrams
    assert text[-3:] in query.char_ngrams


def test_query_rejects_too_many_exact_tokens_instead_of_silently_dropping_them() -> (
    None
):
    text = " ".join(f"{index}원" for index in range(65))

    with pytest.raises(QueryError, match="query_invalid"):
        normalize_query(text)


@pytest.mark.parametrize(
    "query",
    ("", " \n\t ", "x" * 2049, "\x00학교"),
)
def test_query_rejects_empty_oversized_or_control_input_without_retaining_value(
    query: str,
) -> None:
    marker = "PRIVATE_QUERY_SENTINEL"
    candidate = marker if not query else query
    if candidate == marker:
        candidate = ""

    with pytest.raises(QueryError) as captured:
        normalize_query(candidate)

    rendered = " ".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.__cause__),
            repr(captured.value.__context__),
        )
    )
    assert marker not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("years", (1899,)),
        ("domains", ("",)),
        ("case_types", ("unknown",)),
        ("access_level", "restricted"),
    ),
)
def test_query_filters_fail_closed_with_fixed_errors(field: str, value: object) -> None:
    arguments: dict[str, object] = {field: value}

    with pytest.raises(QueryError, match="query_filters_invalid") as captured:
        QueryFilters.create(**arguments)  # type: ignore[arg-type]

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
