"""Behavioral contracts for stable canonical IDs."""

from __future__ import annotations

import hashlib
import traceback
from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.corpus.ids import (
    IssuedIdRegistry,
    make_case_id,
    make_release_id,
    title_hash,
    validate_case_id,
)


def _secret_shape(*fragments: str) -> str:
    return "".join(fragments)


def test_duplicate_number_gets_stable_page_and_title_suffix() -> None:
    """Catches duplicate case numbers replacing a source-locatable suffix."""
    assert make_case_id(2025, "계약", "계약 일반", "1", 13, "2단계 입찰", duplicate=True) == (
        "senqa-2025-contract-contract-general-1-p13-" + title_hash("2단계 입찰")
    )


def test_title_hash_is_nfc_and_whitespace_stable() -> None:
    """Catches source formatting differences creating a new collision suffix."""
    assert title_hash("  2단계\t입찰  ") == title_hash("2단계 입찰")
    assert title_hash("Cafe\u0301") == title_hash("Café")


def test_known_business_slugs_and_unknown_values_are_deterministic() -> None:
    """Catches a category spelling changing either a registered or fallback ID."""
    assert make_case_id(2025, "계약", "계약 일반", "001") == "senqa-2025-contract-contract-general-1"
    fallback = hashlib.sha256("미등록 분야".encode()).hexdigest()[:10]
    assert make_case_id(2025, "미등록 분야", "미등록 편", "  01 ") == (
        f"senqa-2025-{fallback}-{hashlib.sha256('미등록 편'.encode()).hexdigest()[:10]}-1"
    )


def test_registered_source_category_uses_its_fixed_business_slug() -> None:
    """Catches an approved Korean business category silently falling back to a hash."""
    assert make_case_id(2025, "학교회계 지출", "계약 일반", "1") == (
        "senqa-2025-school-accounting-expenditure-contract-general-1"
    )


@pytest.mark.parametrize(
    ("case_no", "message"),
    [("", "case number"), ("../1", "case number"), ("1/2", "case number")],
)
def test_case_number_normalization_rejects_unsafe_identifiers(case_no: str, message: str) -> None:
    """Catches a number injecting separators or an empty segment into an ID."""
    with pytest.raises(ValueError, match=message):
        make_case_id(2025, "계약", "계약 일반", case_no)


@pytest.mark.parametrize(
    "case_no",
    [
        "1234567890",
        "1234567890123456",
        "1" * 17,
        "1" * 23,
        "x" + ("1" * 17) + "y",
        "x" + ("1" * 23) + "y",
        "010-1234-5678",
        "110-123-456789",
        "900101-1234567",
        "x010-1234-5678y",
        "x070-123-4567y",
        "x02-123-4567y",
        "x900101-1234567y",
        _secret_shape("sk", "-", "proj", "-", "abcdefghijklmnop", "123456"),
        _secret_shape("AK", "IA", "IOSFODNN", "7EXAMPLE"),
        _secret_shape(
            "x", "sk", "-", "proj", "-", "abcdefghijklmnop", "123456", "y"
        ),
        _secret_shape("x", "AK", "IA", "IOSFODNN", "7EXAMPLE", "y"),
    ],
)
def test_sensitive_case_number_fails_closed_without_value_or_reversible_digest(
    case_no: str,
) -> None:
    """Catches a guessable source value being redistributed as an unkeyed ID hash."""
    with pytest.raises(ValueError, match="case number") as error:
        make_case_id(2025, "계약", "계약 일반", case_no)

    rendered = "".join(
        traceback.format_exception(error.type, error.value, error.tb)
    )
    reversible_digest = hashlib.sha256(case_no.lower().encode("utf-8")).hexdigest()[:12]
    assert case_no not in str(error.value)
    assert case_no not in repr(error.value)
    assert case_no not in rendered
    assert reversible_digest not in rendered


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "senqa-2025-contract-contract-general-1234567890",
        "senqa-2025-contract-contract-general-1234567890123456",
        "senqa-2025-contract-contract-general-" + ("1" * 17),
        "senqa-2025-contract-contract-general-" + ("1" * 23),
        "senqa-2025-contract-contract-general-x" + ("1" * 17) + "y",
        "senqa-2025-contract-contract-general-x" + ("1" * 23) + "y",
        "senqa-2025-contract-contract-general-010-1234-5678",
        "senqa-2025-contract-contract-general-110123456789",
        "senqa-2025-contract-contract-general-900101-1234567",
        "senqa-2025-contract-contract-general-x010-1234-5678y",
        "senqa-2025-contract-contract-general-x070-123-4567y",
        "senqa-2025-contract-contract-general-x02-123-4567y",
        "senqa-2025-contract-contract-general-x900101-1234567y",
        "senqa-2025-contract-contract-general-"
        + _secret_shape("sk", "-", "proj", "-", "abcdefghijklmnop", "123456"),
        "senqa-2025-contract-contract-general-"
        + _secret_shape("AK", "IA", "IOSFODNN", "7EXAMPLE").lower(),
        "senqa-2025-contract-contract-general-"
        + _secret_shape(
            "x", "sk", "-", "proj", "-", "abcdefghijklmnop", "123456", "y"
        ),
        "senqa-2025-contract-contract-general-"
        + _secret_shape("x", "AK", "IA", "IOSFODNN", "7EXAMPLE", "y").lower(),
        "senqa-2025-contract-contract-general-opaque-deadbeefcafe",
        "senqa-2025-contract-contract-general-opaque-deadbeefcafe-p1-deadbeef",
        "senqa-2025-contract-contract-general-1-p0-deadbeef",
        "senqa-2025-contract-contract-general-p0-deadbeef",
        "senqa-2025-contract-literal-unknown-1",
        "senqa-2025-safe",
        "senqa-2199-contract-contract-general-1",
    ],
)
def test_case_id_validator_rejects_value_bearing_or_out_of_range_ids_without_echo(
    unsafe_id: str,
) -> None:
    with pytest.raises(ValueError, match="canonical case ID") as error:
        validate_case_id(unsafe_id)

    assert unsafe_id not in str(error.value)


@pytest.mark.parametrize("year", [1900, 2100])
def test_case_id_validator_accepts_inclusive_year_boundaries(year: int) -> None:
    """Catches a valid factory year being rejected by central validation."""
    case_id = f"senqa-{year}-contract-contract-general-1"

    assert validate_case_id(case_id) == case_id


@pytest.mark.parametrize("case_no", ["1" * 8, "1" * 9, "x" + ("1" * 9) + "y"])
def test_short_public_ordinal_case_numbers_remain_valid(case_no: str) -> None:
    """Catches fail-closed digit detection rejecting supported short ordinals."""
    case_id = make_case_id(2025, "계약", "계약 일반", case_no)

    assert validate_case_id(case_id) == case_id


def test_case_id_validator_accepts_registered_hashed_and_duplicate_factory_ids() -> None:
    """Catches central validation drifting from safe factory output shapes."""
    case_ids = (
        make_case_id(2025, "계약", "계약 일반", "safe-1"),
        make_case_id(2025, "미등록 분야", "미등록 편", "safe-2"),
        make_case_id(
            2025,
            "계약",
            "계약 일반",
            "safe-3",
            17,
            "중복 사례",
            duplicate=True,
        ),
    )

    assert tuple(validate_case_id(case_id) for case_id in case_ids) == case_ids


@pytest.mark.parametrize(
    ("start_page", "title", "duplicate"),
    [(None, None, False), (13, "중복 사례", True)],
)
def test_factory_rejects_input_that_conflicts_with_longest_slug_segmentation(
    start_page: int | None,
    title: str | None,
    duplicate: bool,
) -> None:
    """Catches two distinct factory inputs aliasing the same canonical case ID."""
    with pytest.raises(ValueError, match="ambiguous canonical case ID"):
        make_case_id(
            2025,
            "계약",
            "계약",
            "general-1",
            start_page,
            title,
            duplicate=duplicate,
        )


def test_factory_accepts_the_longest_registered_slug_segmentation() -> None:
    """Catches collision hardening rejecting the canonical longest-slug input."""
    case_id = make_case_id(2025, "계약", "계약 일반", "1")

    assert case_id == "senqa-2025-contract-contract-general-1"
    assert validate_case_id(case_id) == case_id


@pytest.mark.parametrize(
    ("part", "case_no"),
    [("계약", "general"), ("계약 일반", "1")],
)
def test_duplicate_factory_preserves_base_body_slug_segmentation(
    part: str,
    case_no: str,
) -> None:
    """Catches a duplicate suffix changing the base body's longest-slug parse."""
    case_id = make_case_id(
        2025,
        "계약",
        part,
        case_no,
        13,
        "중복 사례",
        duplicate=True,
    )

    assert validate_case_id(case_id) == case_id


@pytest.mark.parametrize(
    ("page", "title"),
    [(0, "제목"), (1, " \t ")],
)
def test_duplicate_case_requires_valid_collision_inputs(page: int, title: str) -> None:
    """Catches a collision suffix without a trustworthy page and title anchor."""
    with pytest.raises(ValueError, match="duplicate"):
        make_case_id(2025, "계약", "계약 일반", "1", page, title, duplicate=True)


def test_case_number_cannot_alias_a_duplicate_collision_suffix() -> None:
    """Catches a base case number producing the exact ID reserved for a duplicate."""
    title = "2단계 입찰"
    reserved_case_no = f"1-p13-{title_hash(title)}"
    duplicate_id = make_case_id(
        2025, "계약", "계약 일반", "1", 13, title, duplicate=True
    )
    assert duplicate_id == f"senqa-2025-contract-contract-general-{reserved_case_no}"

    with pytest.raises(ValueError, match="reserved duplicate suffix"):
        make_case_id(2025, "계약", "계약 일반", reserved_case_no)


@pytest.mark.parametrize("case_no", ["p0-deadbeef", "1-p0-deadbeef"])
def test_factory_rejects_duplicate_like_zero_page_case_numbers(case_no: str) -> None:
    """Catches a malformed collision marker entering through the factory base input."""
    with pytest.raises(ValueError, match="canonical case ID"):
        make_case_id(2025, "계약", "계약 일반", case_no)


@pytest.mark.parametrize("start_page", [True, 1.0, "1"])
def test_duplicate_start_page_requires_a_real_positive_integer(start_page: object) -> None:
    """Catches bool, float, or string pages producing ambiguous duplicate IDs."""
    with pytest.raises(ValueError, match="positive integer"):
        make_case_id(
            2025,
            "계약",
            "계약 일반",
            "1",
            start_page,  # type: ignore[arg-type]
            "2단계 입찰",
            duplicate=True,
        )


def test_retired_case_id_is_never_reissued() -> None:
    """Catches a tombstoned canonical ID being reintroduced after retirement."""
    registry = IssuedIdRegistry.in_memory()
    issued = "senqa-2025-contract-contract-general-1"
    registry.issue(issued)
    registry.retire(issued)
    with pytest.raises(ValueError, match="already issued"):
        registry.issue(issued)


def test_registry_rejects_invalid_retirement_transitions() -> None:
    """Catches retirement records changing state without an active issued ID."""
    registry = IssuedIdRegistry.in_memory()
    with pytest.raises(ValueError, match="not active"):
        registry.retire("senqa-2025-contract-contract-general-1")
    registry.issue("senqa-2025-contract-contract-general-1")
    registry.retire("senqa-2025-contract-contract-general-1")
    with pytest.raises(ValueError, match="not active"):
        registry.retire("senqa-2025-contract-contract-general-1")


def test_release_id_uses_utc_timestamp_and_git_prefix() -> None:
    """Catches release labels that do not correspond to their UTC build instant."""
    assert make_release_id(datetime(2025, 8, 8, 12, 34, 56, tzinfo=UTC), "deadbeef" + "1" * 32) == (
        "corpus-20250808123456-deadbeef"
    )


@pytest.mark.parametrize(
    ("when", "sha", "message"),
    [
        (datetime(2025, 8, 8, 12, 0, tzinfo=UTC).replace(tzinfo=None), "deadbeef", "UTC"),
        (datetime(2025, 8, 8, 12, 0, tzinfo=timezone(timedelta(hours=9))), "deadbeef", "UTC"),
        (datetime(2025, 8, 8, 12, 0, tzinfo=UTC), "deadbee", "at least 8"),
        (datetime(2025, 8, 8, 12, 0, tzinfo=UTC), "deadbeeg", "hex"),
    ],
)
def test_release_id_rejects_ambiguous_or_invalid_inputs(
    when: datetime, sha: str, message: str
) -> None:
    """Catches ambiguous build timestamps or a non-git fingerprint in a release ID."""
    with pytest.raises(ValueError, match=message):
        make_release_id(when, sha)
