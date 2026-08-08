"""Deterministic identifiers for canonical corpus records and releases."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

_WHITESPACE = re.compile(r"\s+")
_SAFE_CASE_NO = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_HEX_SHA = re.compile(r"^[0-9a-fA-F]+$")

# These source-facing Korean labels are intentionally fixed.  New labels use a
# deterministic hash until a reviewed vocabulary update adds an explicit slug.
_BUSINESS_SLUGS = {
    "감사": "audit",
    "계약": "contract",
    "계약 일반": "contract-general",
    "교육과정": "curriculum",
    "교육공무직원": "education-labor-staff",
    "급여": "payroll",
    "법규": "law",
    "보건": "health",
    "보안": "security",
    "복무": "attendance",
    "물품관리": "supplies-management",
    "민원 및 행정정보공개": "civil-complaints-information-disclosure",
    "사무관리": "office-administration",
    "세입세출외현금": "extra-budget-cash",
    "시설": "facilities",
    "공유재산관리": "shared-property-management",
    "예산": "budget",
    "인사": "personnel",
    "재산": "property",
    "정보화": "information",
    "학생": "student",
    "회계": "accounting",
    "교특회계 세입": "special-account-revenue",
    "보수": "compensation",
    "학교발전기금": "school-development-fund",
    "학교법인": "school-foundation",
    "학교시설관리": "school-facilities-management",
    "학교운영위원회": "school-operations-committee",
    "학교회계 수입": "school-accounting-revenue",
    "학교회계 예결산": "school-accounting-budget-settlement",
    "학교회계 지출": "school-accounting-expenditure",
}


def _normalized_text(value: str, *, label: str) -> str:
    normalized = _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value)).strip()
    if not normalized:
        raise ValueError(f"{label} must not be blank")
    return normalized


def title_hash(title: str) -> str:
    """Return the stable eight-hex collision suffix for a source title."""
    normalized_title = _normalized_text(title, label="title")
    return hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()[:8]


def _business_slug(value: str) -> str:
    normalized_value = _normalized_text(value, label="business label")
    return _BUSINESS_SLUGS.get(
        normalized_value, hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()[:10]
    )


def _normalize_case_number(case_no: str) -> str:
    normalized = _normalized_text(case_no, label="case number").replace(" ", "-")
    if not _SAFE_CASE_NO.fullmatch(normalized):
        raise ValueError("case number must contain only letters, numbers, and single hyphens")
    if normalized.isdigit():
        return str(int(normalized))
    return normalized.lower()


def make_case_id(
    edition_year: int,
    domain: str,
    part: str,
    case_no: str,
    start_page: int | None = None,
    title: str | None = None,
    *,
    duplicate: bool = False,
) -> str:
    """Build a stable case ID, adding a source anchor only for duplicate numbers."""
    if not 1900 <= edition_year <= 2100:
        raise ValueError("edition year must be between 1900 and 2100")
    case_id = (
        f"senqa-{edition_year}-{_business_slug(domain)}-{_business_slug(part)}-"
        f"{_normalize_case_number(case_no)}"
    )
    if not duplicate:
        return case_id
    if start_page is None or start_page < 1 or title is None:
        raise ValueError("duplicate case IDs require a valid start page and title")
    try:
        suffix = title_hash(title)
    except ValueError as error:
        raise ValueError("duplicate case IDs require a valid start page and title") from error
    return f"{case_id}-p{start_page}-{suffix}"


@dataclass
class IssuedIdRegistry:
    """In-memory tombstone registry used by storage adapters during ID issuance."""

    _active: set[str] = field(default_factory=set)
    _retired: set[str] = field(default_factory=set)

    @classmethod
    def in_memory(cls) -> IssuedIdRegistry:
        """Create an empty registry suitable for a transaction-backed adapter test."""
        return cls()

    def issue(self, case_id: str) -> None:
        """Reserve a never-before-issued ID as active."""
        _validate_issued_id(case_id)
        if case_id in self._active or case_id in self._retired:
            raise ValueError("case ID already issued")
        self._active.add(case_id)

    def retire(self, case_id: str) -> None:
        """Move an active ID to its permanent tombstone state."""
        _validate_issued_id(case_id)
        if case_id not in self._active:
            raise ValueError("case ID is not active")
        self._active.remove(case_id)
        self._retired.add(case_id)


def _validate_issued_id(case_id: str) -> None:
    if not case_id or case_id != case_id.strip():
        raise ValueError("case ID must be a nonblank stable identifier")


def make_release_id(released_at: datetime, git_sha: str) -> str:
    """Build a release ID from an explicit UTC instant and a valid Git SHA."""
    if released_at.tzinfo is None or released_at.utcoffset() != UTC.utcoffset(released_at):
        raise ValueError("release timestamp must be explicit UTC")
    if len(git_sha) < 8:
        raise ValueError("git SHA must contain at least 8 hexadecimal characters")
    if _HEX_SHA.fullmatch(git_sha) is None:
        raise ValueError("git SHA must be hexadecimal")
    return f"corpus-{released_at.astimezone(UTC):%Y%m%d%H%M%S}-{git_sha[:8].lower()}"
