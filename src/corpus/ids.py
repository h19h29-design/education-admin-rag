"""Deterministic identifiers for canonical corpus records and releases."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime

_WHITESPACE = re.compile(r"\s+")
_SAFE_CASE_NO = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_RESERVED_DUPLICATE_SUFFIX = re.compile(r"^.+-p[1-9][0-9]*-[0-9a-f]{8}$")
_RESERVED_OPAQUE_CASE_NO = re.compile(r"^opaque-[0-9a-f]{12}$")
_DUPLICATE_CASE_COMPONENT = re.compile(r"^(?P<base>.+)-p[1-9][0-9]*-[0-9a-f]{8}$")
_DUPLICATE_LIKE_CASE_COMPONENT = re.compile(r"^(?:.+-)?p[0-9]+-[0-9a-f]{8}$")
_HASHED_BUSINESS_SLUG = re.compile(r"^[0-9a-f]{10}$")
_HEX_SHA = re.compile(r"^[0-9a-fA-F]+$")
_CANONICAL_CASE_ID = re.compile(
    r"^senqa-(?P<year>[0-9]{4})-(?P<body>[a-z0-9]+(?:-[a-z0-9]+)*)$"
)
_SENSITIVE_COMPACT_NUMBER = re.compile(r"[0-9]{10,64}")
_SENSITIVE_GROUPED_NUMBER = re.compile(
    r"(?:"
    r"[0-9]{6}-[1-8][0-9]{6}|"
    r"[0-9]{2,6}(?:-[0-9]{2,6}){2,5}"
    r")"
)
_SENSITIVE_PROVIDER_TOKEN = re.compile(
    r"(?:"
    r"(?:akia|asia)[a-z0-9]{16}|"
    r"sk-(?:proj-|ant-)?[a-z0-9-]{16,256}|"
    r"xox[baprs]-[a-z0-9-]{16,255}|"
    r"(?:sk|rk)-(?:live|test)-[a-z0-9-]{16,255}"
    r")"
)

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
_REGISTERED_BUSINESS_SLUGS = tuple(
    sorted(frozenset(_BUSINESS_SLUGS.values()), key=len, reverse=True)
)


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
        normalized_value,
        hashlib.sha256(normalized_value.encode("utf-8")).hexdigest()[:10],
    )


def _normalize_case_number(case_no: str) -> str:
    normalized = _normalized_text(case_no, label="case number").replace(" ", "-")
    if not _SAFE_CASE_NO.fullmatch(normalized):
        raise ValueError(
            "case number must contain only letters, numbers, and single hyphens"
        )
    canonical = normalized.lower()
    if _RESERVED_DUPLICATE_SUFFIX.fullmatch(canonical):
        raise ValueError("case number uses a reserved duplicate suffix")
    if _RESERVED_OPAQUE_CASE_NO.fullmatch(canonical):
        raise ValueError("case number uses a reserved opaque form")
    if _case_number_requires_rejection(canonical):
        raise ValueError("case number is not eligible for a canonical ID")
    if canonical.isdigit():
        return str(int(canonical))
    return canonical


def _case_number_requires_rejection(value: str) -> bool:
    compact_digits = value.replace("-", "")
    return bool(
        (len(compact_digits) >= 10 and compact_digits.isdigit())
        or _SENSITIVE_COMPACT_NUMBER.search(value)
        or _SENSITIVE_GROUPED_NUMBER.search(value)
        or _SENSITIVE_PROVIDER_TOKEN.search(value)
        or len(value) > 64
        or any(len(component) >= 24 for component in value.split("-"))
    )


def _consume_business_slug(value: str) -> tuple[str, str] | None:
    for slug in _REGISTERED_BUSINESS_SLUGS:
        prefix = f"{slug}-"
        if value.startswith(prefix):
            return slug, value[len(prefix) :]
    if (
        len(value) > 10
        and value[10] == "-"
        and _HASHED_BUSINESS_SLUG.fullmatch(value[:10])
    ):
        return value[:10], value[11:]
    return None


def _is_canonical_case_component(value: str) -> bool:
    duplicate = _DUPLICATE_CASE_COMPONENT.fullmatch(value)
    if duplicate is not None:
        base = duplicate.group("base")
    elif _DUPLICATE_LIKE_CASE_COMPONENT.fullmatch(value):
        return False
    else:
        base = value

    try:
        return _normalize_case_number(base) == base
    except ValueError:
        return False


def _canonical_body_components(body: str) -> tuple[str, str, str] | None:
    duplicate = _DUPLICATE_CASE_COMPONENT.fullmatch(body)
    if duplicate is None:
        base_body = body
        duplicate_suffix = ""
    else:
        base_body = duplicate.group("base")
        duplicate_suffix = body[len(base_body) :]

    domain = _consume_business_slug(base_body)
    if domain is None:
        return None
    domain_slug, after_domain = domain
    part = _consume_business_slug(after_domain)
    if part is None:
        return None
    part_slug, case_component = part
    return domain_slug, part_slug, case_component + duplicate_suffix


def _has_canonical_body(body: str) -> bool:
    components = _canonical_body_components(body)
    return components is not None and _is_canonical_case_component(components[2])


def validate_case_id(case_id: str) -> str:
    """Validate a bounded opaque canonical ID without echoing rejected input."""
    if not isinstance(case_id, str) or len(case_id) > 200:
        raise ValueError("invalid canonical case ID")
    match = _CANONICAL_CASE_ID.fullmatch(case_id)
    if match is None or not 1900 <= int(match.group("year")) <= 2100:
        raise ValueError("invalid canonical case ID")
    if not _has_canonical_body(match.group("body")):
        raise ValueError("invalid canonical case ID")
    return case_id


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
    domain_slug = _business_slug(domain)
    part_slug = _business_slug(part)
    canonical_case_number = _normalize_case_number(case_no)
    body = f"{domain_slug}-{part_slug}-{canonical_case_number}"
    if _canonical_body_components(body) != (
        domain_slug,
        part_slug,
        canonical_case_number,
    ):
        raise ValueError(
            "business labels and case number form an ambiguous canonical case ID"
        )
    case_id = f"senqa-{edition_year}-{body}"
    if not duplicate:
        return validate_case_id(case_id)
    if (
        isinstance(start_page, bool)
        or not isinstance(start_page, int)
        or start_page < 1
    ):
        raise ValueError("duplicate start page must be a positive integer")
    if title is None:
        raise ValueError("duplicate case IDs require a valid start page and title")
    try:
        suffix = title_hash(title)
    except ValueError as error:
        raise ValueError(
            "duplicate case IDs require a valid start page and title"
        ) from error
    return validate_case_id(f"{case_id}-p{start_page}-{suffix}")


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
    try:
        validate_case_id(case_id)
    except ValueError as error:
        raise ValueError("case ID must be a valid canonical identifier") from error


def make_release_id(released_at: datetime, git_sha: str) -> str:
    """Build a release ID from an explicit UTC instant and a valid Git SHA."""
    if released_at.tzinfo is None or released_at.utcoffset() != UTC.utcoffset(
        released_at
    ):
        raise ValueError("release timestamp must be explicit UTC")
    if len(git_sha) < 8:
        raise ValueError("git SHA must contain at least 8 hexadecimal characters")
    if _HEX_SHA.fullmatch(git_sha) is None:
        raise ValueError("git SHA must be hexadecimal")
    return f"corpus-{released_at.astimezone(UTC):%Y%m%d%H%M%S}-{git_sha[:8].lower()}"
