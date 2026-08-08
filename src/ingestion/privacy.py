"""Bounded privacy detectors and fail-closed eligibility classification."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.corpus.ids import validate_case_id
from src.corpus.models import PiiClass

CaseType: TypeAlias = Literal["qa", "audit", "law_index", "credits"]
FindingKind: TypeAlias = Literal[
    "resident_registration_number",
    "phone",
    "email",
    "bank_account",
    "api_token",
    "jwt",
    "pem_private_key",
    "url_credentials",
    "name_organization_title",
    "audit_date",
    "audit_money",
    "audit_occupation",
    "audit_school_level",
    "anonymization_mark",
]

_ALLOWED_CASE_TYPES = frozenset({"qa", "audit", "law_index", "credits"})
_LOCATION_ID_RE = re.compile(
    r"(?P<scope>case|audit|chunk|page|doc|credits)-"
    r"(?P<entity>[a-z0-9]+(?:-[a-z0-9]+)*):"
    r"(?P<field>[a-z][a-z0-9]*(?:-[a-z0-9]+){0,7})"
)
_LOCATION_HASH_RE = re.compile(r"(?i:[0-9a-f]{32,})")
_LOCATION_RRN_RE = re.compile(r"(?<!\d)\d{6}(?:[- ]?[1-8])\d{6}(?!\d)")
_LOCATION_PHONE_RE = re.compile(
    r"(?<!\d)(?:(?:010|070)\d{7,8}|(?:010|070)[- .]\d{3,4}[- .]\d{4})(?!\d)"
)
_LOCATION_ACCOUNT_RE = re.compile(r"(?<!\d)\d{2,6}(?:[- ]\d{2,6}){2,5}(?!\d)")
_LOCATION_COMPACT_NUMBER_RE = re.compile(r"(?<!\d)\d{10,16}(?!\d)")
_LOCATION_PROVIDER_TOKEN_RE = re.compile(
    r"(?:^|[ -])sk-(?:(?:proj|ant)-)?[a-z0-9-]{16,256}(?:$|[ -])"
)
_MAX_LOCATION_ENTITY_LENGTH = 192
_MAX_LOCATION_FIELD_LENGTH = 32

_OCR_INLINE_GAP = r"[ \t]{0,8}"
_OCR_VALUE_GAP = r"[ \t]{0,12}(?:\r?\n[ \t]{0,12})?"
_OCR_REQUIRED_SEPARATOR = (
    r"(?:[ \t,;/]{1,16}|[ \t,;/]{0,8}\r?\n[ \t]{0,8})"
)
_OCR_FIELD_SEPARATOR = r"[ \t,;/]{0,16}(?:\r?\n[ \t]{0,8})?"
_OCR_DASH = r"[-\u2010-\u2015\u2212\uFE63\uFF0D]"
_OCR_MARKED_PHONE_SEPARATOR = (
    rf"(?:[ \t]{{0,4}}(?:{_OCR_DASH}|\.)[ \t]{{0,4}}"
    r"(?:\r?\n[ \t]{0,4})?)"
)
_OCR_LABELED_PHONE_SEPARATOR = (
    rf"(?:{_OCR_MARKED_PHONE_SEPARATOR}|"
    r"[ \t]{0,4}\r?\n[ \t]{0,4}|[ \t]{1,4})"
)
_OCR_ACCOUNT_SEPARATOR = (
    rf"(?:[ \t]{{0,4}}{_OCR_DASH}[ \t]{{0,4}}(?:\r?\n[ \t]{{0,4}})?|"
    r"[ \t]{0,4}\r?\n[ \t]{0,4}|[ \t]{1,4})"
)
_OCR_RRN_SEPARATOR = (
    rf"(?:[ \t]{{0,4}}{_OCR_DASH}[ \t]{{0,4}}(?:\r?\n[ \t]{{0,4}})?|"
    r"[ \t]{1,4})"
)
_OCR_LABELED_RRN_SEPARATOR = (
    rf"(?:{_OCR_RRN_SEPARATOR}|[ \t]{{0,4}}\r?\n[ \t]{{0,4}})"
)
_ACCOUNT_LABEL = (
    rf"(?:계좌(?:{_OCR_INLINE_GAP}번호)?|"
    rf"입금(?:{_OCR_INLINE_GAP}계좌(?:{_OCR_INLINE_GAP}번호)?)?|"
    rf"은행{_OCR_INLINE_GAP}계좌(?:{_OCR_INLINE_GAP}번호)?)"
)
_PHONE_LABEL = (
    rf"(?:연락처|전화(?:{_OCR_INLINE_GAP}번호)?|"
    rf"휴대(?:전화|폰)(?:{_OCR_INLINE_GAP}번호)?)"
)
_RRN_LABEL = (
    rf"(?:주민{_OCR_INLINE_GAP}(?:등록{_OCR_INLINE_GAP})?번호|"
    rf"외국인{_OCR_INLINE_GAP}등록{_OCR_INLINE_GAP}번호)"
)


class PrivacyModel(BaseModel):
    """Strict immutable model whose errors omit supplied input values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class PrivacyFinding(PrivacyModel):
    """A non-reversible aggregate; detected text and hashes are intentionally absent."""

    kind: FindingKind
    location_id: str = Field(min_length=1, max_length=256)
    count: int = Field(gt=0)

    @field_validator("location_id")
    @classmethod
    def has_safe_location_id(cls, value: str) -> str:
        if not _is_safe_location_id(value):
            raise ValueError("location ID must use canonical opaque structure")
        return value


class PrivacyDecision(PrivacyModel):
    """Privacy class plus eligibility after applying the mandatory lockouts."""

    pii_class: PiiClass
    search_eligible: bool
    answer_eligible: bool
    public_redistribution_approved: Literal[False] = False

    @model_validator(mode="after")
    def has_safe_eligibility(self) -> Self:
        if self.pii_class in {"restricted", "public_credit"} and (
            self.search_eligible or self.answer_eligible
        ):
            raise ValueError("unsafe privacy classes cannot be eligible")
        if self.answer_eligible and not self.search_eligible:
            raise ValueError("answer eligibility requires search eligibility")
        return self


_PATTERNS: dict[FindingKind, tuple[re.Pattern[str], ...]] = {
    "resident_registration_number": (
        re.compile(rf"(?<!\d)\d{{6}}{_OCR_RRN_SEPARATOR}[1-8]\d{{6}}(?!\d)"),
        re.compile(
            rf"{_RRN_LABEL}{_OCR_VALUE_GAP}[:：]?{_OCR_VALUE_GAP}"
            rf"\d{{6}}{_OCR_LABELED_RRN_SEPARATOR}[1-8]\d{{6}}(?!\d)"
        ),
        re.compile(r"(?<!\d)\d{6}[1-8]\d{6}(?!\d)"),
    ),
    "phone": (
        re.compile(
            rf"(?<!\d)(?:(?:010|070)\d{{7,8}}|"
            rf"(?:010|070){_OCR_MARKED_PHONE_SEPARATOR}\d{{3,4}}"
            rf"{_OCR_MARKED_PHONE_SEPARATOR}\d{{4}}|"
            rf"02{_OCR_MARKED_PHONE_SEPARATOR}\d{{3,4}}"
            rf"{_OCR_MARKED_PHONE_SEPARATOR}\d{{4}}|"
            rf"0[3-6][1-5]?{_OCR_MARKED_PHONE_SEPARATOR}\d{{3,4}}"
            rf"{_OCR_MARKED_PHONE_SEPARATOR}\d{{4}})(?!\d)"
        ),
        re.compile(
            rf"{_PHONE_LABEL}{_OCR_VALUE_GAP}[:：]?{_OCR_VALUE_GAP}"
            rf"(?:(?:010|070){_OCR_LABELED_PHONE_SEPARATOR}\d{{3,4}}"
            rf"{_OCR_LABELED_PHONE_SEPARATOR}\d{{4}}|"
            rf"02{_OCR_LABELED_PHONE_SEPARATOR}\d{{3,4}}"
            rf"{_OCR_LABELED_PHONE_SEPARATOR}\d{{4}}|"
            rf"0[3-6][1-5]?{_OCR_LABELED_PHONE_SEPARATOR}\d{{3,4}}"
            rf"{_OCR_LABELED_PHONE_SEPARATOR}\d{{4}})(?!\d)"
        ),
    ),
    "email": (
        re.compile(
            r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?){1,8}"
            r"(?![A-Za-z0-9-])"
        ),
    ),
    "bank_account": (
        re.compile(
            rf"{_ACCOUNT_LABEL}{_OCR_VALUE_GAP}[:：]?{_OCR_VALUE_GAP}"
            rf"\d{{2,6}}(?:{_OCR_ACCOUNT_SEPARATOR}\d{{2,6}}){{2,5}}(?!\d)"
        ),
        re.compile(
            rf"{_ACCOUNT_LABEL}{_OCR_VALUE_GAP}[:：]?{_OCR_VALUE_GAP}"
            r"\d{10,16}(?!\d)"
        ),
    ),
    "api_token": (
        re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,256}"),
        re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,255}"),
        re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,255}"),
        re.compile(r"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{16,255}"),
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
        re.compile(r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
        re.compile(
            r"(?<![A-Za-z0-9_])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,255}"
        ),
        re.compile(r"(?<![A-Za-z0-9_])(?:whsec|hf)_[A-Za-z0-9]{16,255}"),
        re.compile(
            r"(?<![A-Za-z0-9_.])SG\.[A-Za-z0-9_-]{16,128}\."
            r"[A-Za-z0-9_-]{16,128}"
        ),
        re.compile(
            r"(?i:(?:api[_-]?key|access[_-]?token))[ \t]{0,12}[:=][ \t]{0,12}"
            r"[A-Za-z0-9_.-]{16,256}"
        ),
    ),
    "jwt": (
        re.compile(
            r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,2048}\."
            r"[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{8,2048}"
            r"(?![A-Za-z0-9_-])"
        ),
    ),
    "pem_private_key": (
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    "url_credentials": (
        re.compile(
            r"(?i:https?)://[^\s/@:]{1,128}(?::[^\s/@]{1,256})?@"
            r"[^\s/?#]{1,253}"
        ),
    ),
    "name_organization_title": (
        re.compile(
            r"[가-힣A-Za-z0-9]{2,40}(?:교육청|지원청|학교|부서|과|팀)"
            rf"{_OCR_REQUIRED_SEPARATOR}"
            r"(?:교육장|교장|교감|장학사|교육연구사|주무관|사무관|과장|팀장|교사|직원)"
            rf"{_OCR_REQUIRED_SEPARATOR}"
            r"[가-힣]{2,4}(?![가-힣])"
        ),
        re.compile(
            r"(?<![가-힣])[가-힣]{2,4}"
            rf"{_OCR_REQUIRED_SEPARATOR}"
            r"[가-힣A-Za-z0-9]{2,40}(?:교육청|지원청|학교|부서|과|팀)"
            rf"{_OCR_REQUIRED_SEPARATOR}"
            r"(?:교육장|교장|교감|장학사|교육연구사|주무관|사무관|과장|팀장|교사|직원)"
            r"(?![가-힣])"
        ),
        re.compile(
            rf"성명{_OCR_VALUE_GAP}[:：]?{_OCR_VALUE_GAP}[가-힣]{{2,4}}"
            rf"{_OCR_FIELD_SEPARATOR}"
            rf"소속{_OCR_VALUE_GAP}[:：]?{_OCR_VALUE_GAP}"
            r"[가-힣A-Za-z0-9]{2,40}(?:교육청|지원청|학교|부서|과|팀)"
            rf"{_OCR_FIELD_SEPARATOR}직(?:위|책)"
            rf"{_OCR_VALUE_GAP}[:：]?{_OCR_VALUE_GAP}"
            r"(?:교육장|교장|교감|장학사|교육연구사|주무관|사무관|과장|팀장|교사|직원)"
            r"(?![가-힣])"
        ),
    ),
    "audit_date": (
        re.compile(
            r"(?<!\d)(?:19|20)\d{2}[.\-/][ \t]*(?:0?[1-9]|1[0-2])"
            r"[.\-/][ \t]*(?:0?[1-9]|[12]\d|3[01])\.?(?!\d)"
        ),
        re.compile(
            r"(?<!\d)(?:19|20)\d{2}년[ \t]*(?:0?[1-9]|1[0-2])월"
            r"[ \t]*(?:0?[1-9]|[12]\d|3[01])일(?!\d)"
        ),
    ),
    "audit_money": (
        re.compile(r"(?<!\d)(?:\d{1,3}(?:,\d{3})+|\d{1,12})[ \t]*원(?![가-힣])"),
    ),
    "audit_occupation": (
        re.compile(
            r"직종[ \t]{0,8}[:：]?[ \t]{0,8}"
            r"(?:교사|공무원|교육공무직|강사|직원|주무관|행정실장)"
        ),
    ),
    "audit_school_level": (
        re.compile(
            r"학교급[ \t]{0,8}[:：]?[ \t]{0,8}"
            r"(?:유치원|초등학교|중학교|고등학교|특수학교|각종학교)"
        ),
    ),
    "anonymization_mark": (
        re.compile(r"(?:○{2,}|△{2,}|□{2,}|◇{2,}|\*{2,})(?![○△□◇*])"),
        re.compile(
            r"(?:대상자|성명|직원|교사|공무원)[ \t]{0,8}[:：]?[ \t]{0,8}"
            r"[김이박최정강조윤장임한오서신권황안송전홍유고문양손배백허남심노하곽성차주우구민진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제탁국어은편용예봉]모"
            r"(?:[ \t]{0,8}(?:씨|교사|직원|주무관|공무원|학생))?"
        ),
        re.compile(
            r"[김이박최정강조윤장임한오서신권황안송전홍유고문양손배백허남심노하곽성차주우구민진지엄채원천방공현함변염여추도소석선설마길연위표명기반왕금옥육인맹제탁국어은편용예봉]모"
            r"[ \t]{0,8}(?:씨|교사|직원|주무관|공무원|학생)"
        ),
    ),
}

_POLICY_ORDER: tuple[FindingKind, ...] = tuple(_PATTERNS)
_DIRECT_HIGH_RISK_KINDS = frozenset(
    {
        "resident_registration_number",
        "phone",
        "email",
        "bank_account",
        "api_token",
        "jwt",
        "pem_private_key",
        "url_credentials",
    }
)
_AUDIT_QUASI_KINDS = frozenset(
    {"audit_date", "audit_money", "audit_occupation", "audit_school_level"}
)


def scan_text(
    text: str,
    *,
    location_id: str,
    case_type: CaseType = "qa",
) -> tuple[PrivacyFinding, ...]:
    """Scan the complete supplied text and return only safe aggregate metadata."""
    if not _is_safe_location_id(location_id):
        raise ValueError("location ID must use canonical opaque structure")
    _validate_case_type(case_type)

    findings: list[PrivacyFinding] = []
    for kind in _POLICY_ORDER:
        if kind in _AUDIT_QUASI_KINDS and case_type != "audit":
            continue
        spans = {
            match.span()
            for pattern in _PATTERNS[kind]
            for match in pattern.finditer(text)
        }
        count = _count_overlapping_clusters(spans)
        if count:
            findings.append(
                PrivacyFinding(kind=kind, location_id=location_id, count=count)
            )
    return tuple(findings)


def classify_privacy(
    findings: Sequence[PrivacyFinding],
    *,
    case_type: CaseType,
    audit_masked: bool = False,
    proposed_search_eligible: bool = False,
    proposed_answer_eligible: bool = False,
) -> PrivacyDecision:
    """Apply the reviewed privacy truth table without granting public release."""
    _validate_case_type(case_type)
    kinds = {finding.kind for finding in findings}
    if kinds & _DIRECT_HIGH_RISK_KINDS:
        pii_class: PiiClass = "restricted"
    elif case_type == "credits":
        pii_class = "public_credit"
    elif "name_organization_title" in kinds:
        pii_class = "restricted"
    elif case_type == "audit" and audit_masked:
        pii_class = "anonymized_case"
    elif case_type == "audit" and len(kinds & _AUDIT_QUASI_KINDS) >= 2:
        pii_class = "quasi_identifier"
    else:
        pii_class = "none"

    if pii_class in {"restricted", "public_credit"}:
        search_eligible = False
        answer_eligible = False
    else:
        if proposed_answer_eligible and not proposed_search_eligible:
            raise ValueError("answer eligibility requires search eligibility")
        search_eligible = proposed_search_eligible
        answer_eligible = proposed_answer_eligible

    return PrivacyDecision(
        pii_class=pii_class,
        search_eligible=search_eligible,
        answer_eligible=answer_eligible,
    )


def _count_overlapping_clusters(spans: set[tuple[int, int]]) -> int:
    """Count overlapping detector hits once without retaining any detected value."""
    count = 0
    cluster_end = -1
    for start, end in sorted(spans):
        if start >= cluster_end:
            count += 1
            cluster_end = end
        elif end > cluster_end:
            cluster_end = end
    return count


def _validate_case_type(case_type: object) -> None:
    if case_type not in _ALLOWED_CASE_TYPES:
        raise ValueError("unsupported case type")


def _is_safe_location_id(value: str) -> bool:
    match = _LOCATION_ID_RE.fullmatch(value)
    if match is None:
        return False
    entity = match.group("entity")
    field = match.group("field")
    scope = match.group("scope")
    if len(field) > _MAX_LOCATION_FIELD_LENGTH:
        return False
    if scope == "case":
        try:
            validate_case_id(entity)
        except ValueError:
            return False
        components = field
    else:
        if len(entity) > _MAX_LOCATION_ENTITY_LENGTH:
            return False
        components = f"{entity} {field}"
    return not (
        _LOCATION_HASH_RE.search(components)
        or _LOCATION_RRN_RE.search(components)
        or _LOCATION_PHONE_RE.search(components)
        or _LOCATION_ACCOUNT_RE.search(components)
        or _LOCATION_COMPACT_NUMBER_RE.search(components)
        or _LOCATION_PROVIDER_TOKEN_RE.search(components)
        or any(pattern.search(components) for pattern in _PATTERNS["api_token"])
    )
