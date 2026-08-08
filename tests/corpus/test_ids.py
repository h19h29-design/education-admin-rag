"""Behavioral contracts for stable canonical IDs."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from src.corpus.ids import IssuedIdRegistry, make_case_id, make_release_id, title_hash


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
    ("page", "title"),
    [(0, "제목"), (1, " \t ")],
)
def test_duplicate_case_requires_valid_collision_inputs(page: int, title: str) -> None:
    """Catches a collision suffix without a trustworthy page and title anchor."""
    with pytest.raises(ValueError, match="duplicate"):
        make_case_id(2025, "계약", "계약 일반", "1", page, title, duplicate=True)


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
