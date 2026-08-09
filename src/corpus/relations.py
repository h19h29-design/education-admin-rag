"""Exact historical law citations and externally reviewed case relations."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal, NoReturn, cast

from src.corpus.ids import validate_case_id
from src.corpus.models import Case, CaseRelation, LawRef, SourceSpan

RelationType = Literal["related", "duplicate", "supersedes", "conflicts"]
_RELATION_TYPES = {"related", "duplicate", "supersedes", "conflicts"}
_SYMMETRIC_TYPES = {"related", "duplicate", "conflicts"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REV_ID_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_LAW_NAME_RE = re.compile(r"「(?P<name>[^」\n]{1,200})」")
_ABBREVIATION_RE = re.compile(
    r"이하\s*(?:[\"\u201c](?P<double>[^\"\u201d\n]{1,100})[\"\u201d]"
    r"|['\u2018](?P<single>[^'\u2019\n]{1,100})['\u2019])"
    r"(?:\s*이라\s*(?:한다|함))?"
)
_ARTICLE_RE = re.compile(
    r"(?P<article>제[0-9]+조)(?P<paragraph>제[0-9]+항)?(?P<item>제[0-9]+호)?"
)
_EFFECTIVE_DATE_RE = re.compile(
    r"시행\s*(?P<year>[0-9]{4})[.]\s*(?P<month>[0-9]{1,2})[.]\s*(?P<day>[0-9]{1,2})[.]?"
)
_RELATION_ID_RE = re.compile(r"^relation-[0-9a-f]{32}$")
_MAX_LAW_SOURCES = 10_000
_MAX_SOURCE_CHARS = 2_000_000


class RelationError(ValueError):
    """A value-free law or relation trust-boundary failure."""


def _raise(message: str) -> NoReturn:
    raise RelationError(message) from None


@dataclass(frozen=True, slots=True)
class LawSource:
    """Normalized citation text plus its exact raw source authority."""

    normalized_text: str
    raw_text: str
    source_span_index: int


@dataclass(frozen=True, slots=True)
class RelationCandidate:
    """A non-canonical relation proposal that never implies approval."""

    relation_id: str
    source_case_id: str
    target_case_id: str
    relation_type: RelationType
    confidence: float
    evidence_sha256: str
    source_content_sha256: str
    target_content_sha256: str


@dataclass(frozen=True, slots=True)
class RelationApproval:
    """Canonical reviewer decision whose bytes require an external digest pin."""

    relation_id: str
    source_case_id: str
    target_case_id: str
    relation_type: RelationType
    confidence: float
    source_content_sha256: str
    target_content_sha256: str
    evidence_sha256: str
    reviewer_id: str

    @classmethod
    def create(
        cls,
        candidate: RelationCandidate,
        *,
        reviewer_id: str,
        evidence_sha256: str,
    ) -> RelationApproval:
        """Create review bytes; callers must still supply an external digest pin."""
        approved_candidate = _revalidate_candidate(candidate)
        if approved_candidate is None:
            _raise("relation candidate is invalid")
        if (
            not isinstance(reviewer_id, str)
            or _REV_ID_RE.fullmatch(reviewer_id) is None
            or evidence_sha256 != approved_candidate.evidence_sha256
        ):
            _raise("relation review approval is invalid")
        return cls(
            relation_id=approved_candidate.relation_id,
            source_case_id=approved_candidate.source_case_id,
            target_case_id=approved_candidate.target_case_id,
            relation_type=approved_candidate.relation_type,
            confidence=approved_candidate.confidence,
            source_content_sha256=approved_candidate.source_content_sha256,
            target_content_sha256=approved_candidate.target_content_sha256,
            evidence_sha256=evidence_sha256,
            reviewer_id=reviewer_id,
        )

    def canonical_bytes(self) -> bytes:
        approved = _revalidate_approval(self)
        if approved is None:
            _raise("relation review approval is invalid")
        return _approval_canonical_bytes(approved)

    @property
    def fingerprint_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedCaseRelation:
    """A relation that retains externally pinned reviewer evidence and endpoints."""

    relation: CaseRelation
    approval: RelationApproval
    approval_sha256: str
    source_content_sha256: str
    target_content_sha256: str
    binding_sha256: str


@dataclass(frozen=True, slots=True, init=False)
class VerifiedLawRef:
    """A law reference retaining its exact canonical source-span index."""

    law_ref: LawRef
    source_span_index: int
    citation_ordinal: int
    case_content_sha256: str
    binding_sha256: str

    @property
    def law_ref_id(self) -> str:
        return self.law_ref.law_ref_id

    @property
    def case_id(self) -> str:
        return self.law_ref.case_id

    @property
    def display_name(self) -> str:
        return self.law_ref.display_name

    @property
    def abbreviation(self) -> str | None:
        return self.law_ref.abbreviation

    @property
    def article(self) -> str | None:
        return self.law_ref.article

    @property
    def paragraph(self) -> str | None:
        return self.law_ref.paragraph

    @property
    def item(self) -> str | None:
        return self.law_ref.item

    @property
    def cited_effective_date(self) -> date | None:
        return self.law_ref.cited_effective_date

    @property
    def quote(self) -> str:
        return self.law_ref.quote

    @property
    def source_span(self) -> SourceSpan:
        return self.law_ref.source_span


def _model_field_dict(
    value: object, expected_type: type[object]
) -> dict[str, object] | None:
    if type(value) is not expected_type:
        return None
    try:
        fields = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        return None
    if type(fields) is not dict:
        return None
    return cast(dict[str, object], fields.copy())


def _revalidate_source_span(value: object) -> SourceSpan | None:
    fields = _model_field_dict(value, SourceSpan)
    if fields is None or set(fields) != set(SourceSpan.model_fields):
        return None
    try:
        return SourceSpan.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _valid_case_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_case_id(value)
    except ValueError:
        return False
    return True


def _revalidate_case(value: object) -> Case | None:
    fields = _model_field_dict(value, Case)
    if fields is None or set(fields) != set(Case.model_fields):
        return None
    spans = fields.get("source_spans")
    if type(spans) is not tuple:
        return None
    checked_spans = tuple(_revalidate_source_span(span) for span in spans)
    if any(span is None for span in checked_spans):
        return None
    fields["source_spans"] = tuple(
        cast(SourceSpan, span).model_dump(mode="python") for span in checked_spans
    )
    try:
        approved = Case.model_validate(fields)
    except (TypeError, ValueError):
        return None
    if not _valid_case_id(approved.case_id):
        return None
    return approved


def canonical_case_sha256(case: object) -> str:
    """Return the domain-separated semantic hash used by relation approvals."""
    approved = _revalidate_case(case)
    if approved is None:
        _raise("canonical case is invalid")
    payload = json.dumps(
        approved.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(b"sen-qa-case-v1\0" + payload).hexdigest()


def _revalidate_law_ref(value: object) -> LawRef | None:
    fields = _model_field_dict(value, LawRef)
    if fields is None or set(fields) != set(LawRef.model_fields):
        return None
    span = _revalidate_source_span(fields.get("source_span"))
    if span is None:
        return None
    fields["source_span"] = span.model_dump(mode="python")
    try:
        return LawRef.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _law_ref_binding_sha256(
    law_ref: LawRef,
    source_span_index: int,
    citation_ordinal: int,
    case_content_sha256: str,
) -> str:
    payload = json.dumps(
        {
            "case_content_sha256": case_content_sha256,
            "citation_ordinal": citation_ordinal,
            "law_ref": law_ref.model_dump(mode="json"),
            "source_span_index": source_span_index,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(b"sen-qa-verified-law-ref-v1\0" + payload).hexdigest()


def _new_verified_law_ref(
    law_ref: LawRef,
    source_span_index: int,
    citation_ordinal: int,
    case_content_sha256: str,
) -> VerifiedLawRef:
    verified = object.__new__(VerifiedLawRef)
    object.__setattr__(verified, "law_ref", law_ref)
    object.__setattr__(verified, "source_span_index", source_span_index)
    object.__setattr__(verified, "citation_ordinal", citation_ordinal)
    object.__setattr__(verified, "case_content_sha256", case_content_sha256)
    object.__setattr__(
        verified,
        "binding_sha256",
        _law_ref_binding_sha256(
            law_ref,
            source_span_index,
            citation_ordinal,
            case_content_sha256,
        ),
    )
    return verified


def revalidate_verified_law_ref(value: object, case: object) -> VerifiedLawRef:
    """Recheck a law reference and its exact source-span index against its case."""
    if type(value) is not VerifiedLawRef:
        _raise("verified law reference is required")
    approved_case = _revalidate_case(case)
    law_ref = _revalidate_law_ref(value.law_ref)
    if approved_case is None or law_ref is None:
        _raise("verified law reference is invalid")
    if (
        isinstance(value.source_span_index, bool)
        or not isinstance(value.source_span_index, int)
        or value.source_span_index < 0
        or value.source_span_index >= len(approved_case.source_spans)
        or isinstance(value.citation_ordinal, bool)
        or not isinstance(value.citation_ordinal, int)
        or value.citation_ordinal < 1
        or law_ref.case_id != approved_case.case_id
        or law_ref.source_span != approved_case.source_spans[value.source_span_index]
    ):
        _raise("verified law reference source is invalid")
    raw_hash = hashlib.sha256(law_ref.quote.encode("utf-8")).hexdigest()
    raw_citations = _printed_citations(law_ref.quote)
    if value.citation_ordinal > len(raw_citations):
        _raise("verified law reference printed citation is invalid")
    printed = raw_citations[value.citation_ordinal - 1]
    expected_identity = hashlib.sha256(
        (
            "sen-qa-law-ref-v1\0"
            + approved_case.case_id
            + "\0"
            + str(value.source_span_index)
            + "\0"
            + law_ref.source_span.text_sha256
            + "\0"
            + str(value.citation_ordinal)
        ).encode("utf-8")
    ).hexdigest()[:24]
    if (
        raw_hash != law_ref.source_span.text_sha256
        or law_ref.law_ref_id != f"lawref-{expected_identity}"
        or law_ref.display_name != printed.display_name
        or law_ref.abbreviation != printed.abbreviation
        or law_ref.article != printed.article
        or law_ref.paragraph != printed.paragraph
        or law_ref.item != printed.item
        or law_ref.cited_effective_date != printed.effective_date
        or law_ref.parsing_confidence != 1.0
        or law_ref.currency_status != approved_case.currency_status
        or law_ref.review_status != "needs_review"
    ):
        _raise("verified law reference printed citation is invalid")
    case_hash = canonical_case_sha256(approved_case)
    if not isinstance(value.case_content_sha256, str) or not hmac.compare_digest(
        value.case_content_sha256, case_hash
    ):
        _raise("verified law reference case content is invalid")
    binding = _law_ref_binding_sha256(
        law_ref,
        value.source_span_index,
        value.citation_ordinal,
        case_hash,
    )
    if not isinstance(value.binding_sha256, str) or not hmac.compare_digest(
        value.binding_sha256, binding
    ):
        _raise("verified law reference binding is invalid")
    return _new_verified_law_ref(
        law_ref,
        value.source_span_index,
        value.citation_ordinal,
        case_hash,
    )


def _revalidate_law_source(value: object) -> LawSource | None:
    if type(value) is not LawSource:
        return None
    if (
        not isinstance(value.normalized_text, str)
        or not value.normalized_text.strip()
        or len(value.normalized_text) > _MAX_SOURCE_CHARS
        or not isinstance(value.raw_text, str)
        or not value.raw_text
        or len(value.raw_text) > _MAX_SOURCE_CHARS
        or isinstance(value.source_span_index, bool)
        or not isinstance(value.source_span_index, int)
        or value.source_span_index < 0
    ):
        return None
    return LawSource(
        normalized_text=value.normalized_text,
        raw_text=value.raw_text,
        source_span_index=value.source_span_index,
    )


def _source_date(text: str) -> date | None:
    match = _EFFECTIVE_DATE_RE.search(text)
    if match is None:
        return None
    invalid = False
    try:
        result = date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError:
        invalid = True
        result = None
    if invalid:
        _raise("law reference effective date is invalid")
    return result


@dataclass(frozen=True, slots=True)
class _PrintedCitation:
    display_name: str
    abbreviation: str | None
    article: str | None
    paragraph: str | None
    item: str | None
    effective_date: date | None


def _printed_citations(text: str) -> tuple[_PrintedCitation, ...]:
    name_matches = tuple(_LAW_NAME_RE.finditer(text))
    citations: list[_PrintedCitation] = []
    for index, match in enumerate(name_matches):
        next_start = (
            name_matches[index + 1].start()
            if index + 1 < len(name_matches)
            else len(text)
        )
        suffix = text[match.end() : next_start]
        article_match = _ARTICLE_RE.search(suffix)
        abbreviation_match = _ABBREVIATION_RE.search(suffix)
        citations.append(
            _PrintedCitation(
                display_name=match.group("name"),
                abbreviation=(
                    abbreviation_match.group("double")
                    or abbreviation_match.group("single")
                    if abbreviation_match
                    else None
                ),
                article=article_match.group("article") if article_match else None,
                paragraph=article_match.group("paragraph") if article_match else None,
                item=article_match.group("item") if article_match else None,
                effective_date=_source_date(suffix),
            )
        )
    return tuple(citations)


def extract_law_refs(case: object, sources: object) -> tuple[VerifiedLawRef, ...]:
    """Parse citations while preserving the exact historical printed quote."""
    approved_case = _revalidate_case(case)
    if approved_case is None:
        _raise("canonical case is invalid")
    if type(sources) is not tuple or len(sources) > _MAX_LAW_SOURCES:
        _raise("law reference source collection is invalid")
    checked = tuple(_revalidate_law_source(source) for source in sources)
    if any(source is None for source in checked):
        _raise("law reference source is invalid")
    approved_sources = cast(tuple[LawSource, ...], checked)
    aggregate = " ".join(source.normalized_text for source in approved_sources)
    if approved_case.basis_text is None or " ".join(aggregate.split()) != " ".join(
        approved_case.basis_text.split()
    ):
        _raise("law reference source does not match the canonical basis")

    law_refs: list[VerifiedLawRef] = []
    case_content_sha256 = canonical_case_sha256(approved_case)
    for source in approved_sources:
        if source.source_span_index >= len(approved_case.source_spans):
            _raise("law reference source span is invalid")
        span = approved_case.source_spans[source.source_span_index]
        if (
            hashlib.sha256(source.raw_text.encode("utf-8")).hexdigest()
            != span.text_sha256
        ):
            _raise("law reference raw hash does not match its source span")
        normalized_citations = _printed_citations(source.normalized_text)
        raw_citations = _printed_citations(source.raw_text)
        if normalized_citations != raw_citations:
            _raise("law reference does not preserve the printed citation")
        for ordinal, citation in enumerate(raw_citations, start=1):
            identity = hashlib.sha256(
                (
                    "sen-qa-law-ref-v1\0"
                    + approved_case.case_id
                    + "\0"
                    + str(source.source_span_index)
                    + "\0"
                    + span.text_sha256
                    + "\0"
                    + str(ordinal)
                ).encode("utf-8")
            ).hexdigest()[:24]
            law_ref = LawRef(
                law_ref_id=f"lawref-{identity}",
                case_id=approved_case.case_id,
                display_name=citation.display_name,
                abbreviation=citation.abbreviation,
                article=citation.article,
                paragraph=citation.paragraph,
                item=citation.item,
                cited_effective_date=citation.effective_date,
                quote=source.raw_text,
                source_span=span,
                parsing_confidence=1.0,
                currency_status=approved_case.currency_status,
                review_status="needs_review",
            )
            law_refs.append(
                _new_verified_law_ref(
                    law_ref,
                    source.source_span_index,
                    ordinal,
                    case_content_sha256,
                )
            )
    return tuple(law_refs)


def propose_relation(
    source_case: object,
    target_case: object,
    *,
    relation_type: str,
    confidence: float,
    evidence_sha256: str,
) -> RelationCandidate:
    """Create a deterministic candidate without promoting it to canonical data."""
    source = _revalidate_case(source_case)
    target = _revalidate_case(target_case)
    if source is None or target is None:
        _raise("relation case is invalid")
    if source.case_id == target.case_id:
        _raise("relation must connect distinct cases")
    if not isinstance(relation_type, str) or relation_type not in _RELATION_TYPES:
        _raise("relation type is invalid")
    if not _valid_confidence_input(confidence) or not _valid_sha256(evidence_sha256):
        _raise("relation evidence is invalid")
    relation_type = cast(RelationType, relation_type)
    canonical_confidence = float(confidence)
    source_hash = canonical_case_sha256(source)
    target_hash = canonical_case_sha256(target)
    if relation_type in _SYMMETRIC_TYPES and source.case_id > target.case_id:
        source, target = target, source
        source_hash, target_hash = target_hash, source_hash
    relation_id = _relation_id(
        relation_type=relation_type,
        source_case_id=source.case_id,
        target_case_id=target.case_id,
        confidence=canonical_confidence,
        evidence_sha256=evidence_sha256,
        source_content_sha256=source_hash,
        target_content_sha256=target_hash,
    )
    return RelationCandidate(
        relation_id=relation_id,
        source_case_id=source.case_id,
        target_case_id=target.case_id,
        relation_type=relation_type,
        confidence=canonical_confidence,
        evidence_sha256=evidence_sha256,
        source_content_sha256=source_hash,
        target_content_sha256=target_hash,
    )


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _valid_confidence(value: object) -> bool:
    return (
        type(value) is float
        and math.isfinite(value)
        and 0 <= value <= 1
        and not (value == 0 and math.copysign(1.0, value) < 0)
    )


def _valid_confidence_input(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    canonical = float(value)
    return (
        math.isfinite(canonical)
        and 0 <= canonical <= 1
        and not (canonical == 0 and math.copysign(1.0, canonical) < 0)
    )


def _relation_id(
    *,
    relation_type: RelationType,
    source_case_id: str,
    target_case_id: str,
    confidence: float,
    evidence_sha256: str,
    source_content_sha256: str,
    target_content_sha256: str,
) -> str:
    payload = {
        "confidence": confidence,
        "evidence_sha256": evidence_sha256,
        "relation_type": relation_type,
        "source_case_id": source_case_id,
        "source_content_sha256": source_content_sha256,
        "target_case_id": target_case_id,
        "target_content_sha256": target_content_sha256,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    digest = hashlib.sha256(b"sen-qa-relation-v2\0" + encoded).hexdigest()[:32]
    return f"relation-{digest}"


def _revalidate_candidate(value: object) -> RelationCandidate | None:
    if type(value) is not RelationCandidate:
        return None
    if (
        not isinstance(value.relation_id, str)
        or _RELATION_ID_RE.fullmatch(value.relation_id) is None
        or not isinstance(value.relation_type, str)
        or value.relation_type not in _RELATION_TYPES
        or not _valid_case_id(value.source_case_id)
        or not _valid_case_id(value.target_case_id)
        or not _valid_confidence(value.confidence)
        or not _valid_sha256(value.evidence_sha256)
        or not _valid_sha256(value.source_content_sha256)
        or not _valid_sha256(value.target_content_sha256)
        or value.source_case_id == value.target_case_id
    ):
        return None
    relation_type = value.relation_type
    if value.relation_id != _relation_id(
        relation_type=relation_type,
        source_case_id=value.source_case_id,
        target_case_id=value.target_case_id,
        confidence=value.confidence,
        evidence_sha256=value.evidence_sha256,
        source_content_sha256=value.source_content_sha256,
        target_content_sha256=value.target_content_sha256,
    ):
        return None
    return RelationCandidate(
        relation_id=value.relation_id,
        source_case_id=value.source_case_id,
        target_case_id=value.target_case_id,
        relation_type=relation_type,
        confidence=value.confidence,
        evidence_sha256=value.evidence_sha256,
        source_content_sha256=value.source_content_sha256,
        target_content_sha256=value.target_content_sha256,
    )


def _revalidate_approval(value: object) -> RelationApproval | None:
    if type(value) is not RelationApproval:
        return None
    if (
        not isinstance(value.relation_id, str)
        or _RELATION_ID_RE.fullmatch(value.relation_id) is None
        or not isinstance(value.relation_type, str)
        or value.relation_type not in _RELATION_TYPES
        or not _valid_case_id(value.source_case_id)
        or not _valid_case_id(value.target_case_id)
        or value.source_case_id == value.target_case_id
        or not _valid_confidence(value.confidence)
        or not _valid_sha256(value.source_content_sha256)
        or not _valid_sha256(value.target_content_sha256)
        or not _valid_sha256(value.evidence_sha256)
        or not isinstance(value.reviewer_id, str)
        or _REV_ID_RE.fullmatch(value.reviewer_id) is None
    ):
        return None
    relation_type = value.relation_type
    if value.relation_id != _relation_id(
        relation_type=relation_type,
        source_case_id=value.source_case_id,
        target_case_id=value.target_case_id,
        confidence=value.confidence,
        evidence_sha256=value.evidence_sha256,
        source_content_sha256=value.source_content_sha256,
        target_content_sha256=value.target_content_sha256,
    ):
        return None
    return RelationApproval(
        relation_id=value.relation_id,
        source_case_id=value.source_case_id,
        target_case_id=value.target_case_id,
        relation_type=relation_type,
        confidence=value.confidence,
        source_content_sha256=value.source_content_sha256,
        target_content_sha256=value.target_content_sha256,
        evidence_sha256=value.evidence_sha256,
        reviewer_id=value.reviewer_id,
    )


def _approval_canonical_bytes(approval: RelationApproval) -> bytes:
    payload = {
        "confidence": approval.confidence,
        "evidence_sha256": approval.evidence_sha256,
        "relation_id": approval.relation_id,
        "relation_type": approval.relation_type,
        "reviewer_id": approval.reviewer_id,
        "source_case_id": approval.source_case_id,
        "source_content_sha256": approval.source_content_sha256,
        "target_case_id": approval.target_case_id,
        "target_content_sha256": approval.target_content_sha256,
    }
    return (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _revalidate_case_relation(value: object) -> CaseRelation | None:
    fields = _model_field_dict(value, CaseRelation)
    if fields is None or set(fields) != set(CaseRelation.model_fields):
        return None
    try:
        return CaseRelation.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _relation_binding_sha256(
    relation: CaseRelation,
    approval: RelationApproval,
    approval_sha256: str,
) -> str:
    relation_bytes = json.dumps(
        relation.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(
        b"sen-qa-verified-relation-v1\0"
        + len(relation_bytes).to_bytes(8, "big")
        + relation_bytes
        + bytes.fromhex(approval_sha256)
        + _approval_canonical_bytes(approval)
    ).hexdigest()


def _new_verified_relation(
    relation: CaseRelation,
    approval: RelationApproval,
    approval_sha256: str,
) -> VerifiedCaseRelation:
    verified = object.__new__(VerifiedCaseRelation)
    object.__setattr__(verified, "relation", relation)
    object.__setattr__(verified, "approval", approval)
    object.__setattr__(verified, "approval_sha256", approval_sha256)
    object.__setattr__(
        verified, "source_content_sha256", approval.source_content_sha256
    )
    object.__setattr__(
        verified, "target_content_sha256", approval.target_content_sha256
    )
    object.__setattr__(
        verified,
        "binding_sha256",
        _relation_binding_sha256(relation, approval, approval_sha256),
    )
    return verified


def canonicalize_relation(
    candidate: object,
    *,
    approval: object | None,
    expected_approval_sha256: str,
) -> VerifiedCaseRelation:
    """Promote only a candidate exactly bound to an externally pinned approval."""
    approved_candidate = _revalidate_candidate(candidate)
    approved_review = _revalidate_approval(approval)
    if approved_candidate is None:
        _raise("relation candidate is invalid")
    if approved_review is None:
        _raise("relation review approval is required")
    if (
        not isinstance(expected_approval_sha256, str)
        or _SHA256_RE.fullmatch(expected_approval_sha256) is None
        or not hmac.compare_digest(
            approved_review.fingerprint_sha256, expected_approval_sha256
        )
    ):
        _raise("relation does not match the pinned approval")
    candidate_fields = (
        approved_candidate.relation_id,
        approved_candidate.source_case_id,
        approved_candidate.target_case_id,
        approved_candidate.relation_type,
        approved_candidate.confidence,
        approved_candidate.source_content_sha256,
        approved_candidate.target_content_sha256,
        approved_candidate.evidence_sha256,
    )
    review_fields = (
        approved_review.relation_id,
        approved_review.source_case_id,
        approved_review.target_case_id,
        approved_review.relation_type,
        approved_review.confidence,
        approved_review.source_content_sha256,
        approved_review.target_content_sha256,
        approved_review.evidence_sha256,
    )
    if candidate_fields != review_fields:
        _raise("relation review approval does not match the candidate")
    relation = CaseRelation(
        relation_id=approved_candidate.relation_id,
        source_case_id=approved_candidate.source_case_id,
        target_case_id=approved_candidate.target_case_id,
        relation_type=approved_candidate.relation_type,
        confidence=approved_candidate.confidence,
        review_status="approved",
    )
    return _new_verified_relation(
        relation,
        approved_review,
        expected_approval_sha256,
    )


def revalidate_verified_relation(
    value: object,
    source_case: object,
    target_case: object,
    *,
    expected_approval_sha256: str,
) -> VerifiedCaseRelation:
    """Recheck a relation, its approval bytes, and both current endpoint contents."""
    if type(value) is not VerifiedCaseRelation:
        _raise("verified relation is required")
    if not _valid_sha256(expected_approval_sha256):
        _raise("verified relation approval is invalid")
    relation = _revalidate_case_relation(value.relation)
    approval = _revalidate_approval(value.approval)
    if relation is None or approval is None:
        _raise("verified relation is invalid")
    approval_sha256 = hashlib.sha256(_approval_canonical_bytes(approval)).hexdigest()
    if (
        not isinstance(value.approval_sha256, str)
        or not hmac.compare_digest(value.approval_sha256, approval_sha256)
        or not hmac.compare_digest(approval_sha256, expected_approval_sha256)
    ):
        _raise("verified relation approval is invalid")
    relation_fields = (
        relation.relation_id,
        relation.source_case_id,
        relation.target_case_id,
        relation.relation_type,
        relation.confidence,
    )
    approval_fields = (
        approval.relation_id,
        approval.source_case_id,
        approval.target_case_id,
        approval.relation_type,
        approval.confidence,
    )
    if relation_fields != approval_fields or relation.review_status != "approved":
        _raise("verified relation does not match its approval")

    first = _revalidate_case(source_case)
    second = _revalidate_case(target_case)
    if first is None or second is None or first.case_id == second.case_id:
        _raise("verified relation endpoint is invalid")
    cases = {first.case_id: first, second.case_id: second}
    if set(cases) != {relation.source_case_id, relation.target_case_id}:
        _raise("verified relation endpoint is invalid")
    source_hash = canonical_case_sha256(cases[relation.source_case_id])
    target_hash = canonical_case_sha256(cases[relation.target_case_id])
    if (
        not isinstance(value.source_content_sha256, str)
        or not isinstance(value.target_content_sha256, str)
        or value.source_content_sha256 != source_hash
        or value.target_content_sha256 != target_hash
        or approval.source_content_sha256 != source_hash
        or approval.target_content_sha256 != target_hash
    ):
        _raise("verified relation endpoint content is invalid")
    binding = _relation_binding_sha256(relation, approval, approval_sha256)
    if not isinstance(value.binding_sha256, str) or not hmac.compare_digest(
        value.binding_sha256, binding
    ):
        _raise("verified relation binding is invalid")
    return _new_verified_relation(relation, approval, approval_sha256)
