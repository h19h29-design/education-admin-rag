"""Conservative, provenance-preserving text normalization and correction records."""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_HORIZONTAL_WHITESPACE_RE = re.compile(r"[^\S\n]+")
_SOFT_LINE_END_HYPHEN_RE = re.compile(r"(?<=[A-Za-z])\u00ad\n(?=[A-Za-z])")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVIEWER_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
_REASON_CODE_RE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
_LEXICAL_FRAGMENT_RE = re.compile(r"[A-Za-z]{3,64}")
_SAFE_SEPARATOR_TRANSLATION = str.maketrans({"･": "·", "・": "·"})


class NormalizationModel(BaseModel):
    """Strict immutable base for derived normalization artifacts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class RepeatedLineEvidence(NormalizationModel):
    """Document-frequency and normalized page-coordinate evidence for one line."""

    text: str = Field(min_length=1)
    document_page_count: int = Field(gt=0)
    page_occurrence_count: int = Field(ge=2)
    y0_fraction: float = Field(ge=0.0, le=1.0)
    y1_fraction: float = Field(ge=0.0, le=1.0)
    line_index: int | None = Field(default=None, ge=0)
    raw_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def has_possible_frequency_and_ordered_coordinates(self) -> Self:
        if self.page_occurrence_count > self.document_page_count:
            raise ValueError("line occurrence count cannot exceed document page count")
        if self.y0_fraction > self.y1_fraction:
            raise ValueError("line coordinate fractions must be ordered")
        return self

    @property
    def supports_removal(self) -> bool:
        """Require a majority occurrence and a conservative top/bottom margin."""
        has_document_frequency = self.page_occurrence_count * 2 > self.document_page_count
        has_margin_coordinate = self.y1_fraction <= 0.10 or self.y0_fraction >= 0.90
        return has_document_frequency and has_margin_coordinate


class LexicalWrapEvidence(NormalizationModel):
    """Exact raw-page evidence that two ASCII word fragments are one lexical token."""

    raw_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    line_index: int = Field(ge=0)
    left_fragment: str = Field(min_length=3, max_length=64)
    right_fragment: str = Field(min_length=3, max_length=64)

    @model_validator(mode="after")
    def has_lexical_fragments_only(self) -> Self:
        if not _LEXICAL_FRAGMENT_RE.fullmatch(
            self.left_fragment
        ) or not _LEXICAL_FRAGMENT_RE.fullmatch(self.right_fragment):
            raise ValueError("lexical wrap fragments must contain ASCII letters only")
        return self


class Correction(NormalizationModel):
    """Append-only metadata for a reviewed change between two text hashes."""

    reviewer_id: str = Field(min_length=1, max_length=64)
    corrected_at: datetime
    reason_code: str = Field(min_length=1, max_length=64)
    before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def has_review_identity_utc_time_and_distinct_boundaries(self) -> Self:
        if not _REVIEWER_ID_RE.fullmatch(self.reviewer_id):
            raise ValueError("reviewer ID must use canonical bounded syntax")
        if not _REASON_CODE_RE.fullmatch(self.reason_code):
            raise ValueError("reason code must use canonical bounded syntax")
        if self.corrected_at.tzinfo is None or self.corrected_at.utcoffset() != UTC.utcoffset(
            self.corrected_at
        ):
            raise ValueError("correction timestamp must be an explicit UTC value")
        if hmac.compare_digest(self.before_sha256, self.after_sha256):
            raise ValueError("correction must have different SHA-256 boundaries")
        return self


class TextLayers(NormalizationModel):
    """Immutable raw, normalized, and reviewer-corrected text layers."""

    raw_text: str
    normalized_text: str
    corrected_text: str
    corrections: tuple[Correction, ...] = ()

    @classmethod
    def from_raw(
        cls,
        raw_text: str,
        *,
        repeated_lines: tuple[RepeatedLineEvidence, ...] = (),
        lexical_wraps: tuple[LexicalWrapEvidence, ...] = (),
    ) -> TextLayers:
        """Create derived layers while retaining the raw input byte-for-byte as text."""
        normalized = normalize_text(
            raw_text,
            repeated_lines=repeated_lines,
            lexical_wraps=lexical_wraps,
        )
        return cls(
            raw_text=raw_text,
            normalized_text=normalized,
            corrected_text=normalized,
        )

    @model_validator(mode="after")
    def has_contiguous_correction_hash_chain(self) -> Self:
        expected_before = _sha256_text(self.normalized_text)
        previous_timestamp: datetime | None = None
        for correction in self.corrections:
            if not hmac.compare_digest(correction.before_sha256, expected_before):
                raise ValueError("correction history has a broken SHA-256 boundary")
            if (
                previous_timestamp is not None
                and correction.corrected_at <= previous_timestamp
            ):
                raise ValueError("correction timestamps must strictly increase")
            expected_before = correction.after_sha256
            previous_timestamp = correction.corrected_at
        if self.corrections:
            if not hmac.compare_digest(expected_before, _sha256_text(self.corrected_text)):
                raise ValueError("corrected text does not match the final SHA-256 boundary")
        elif self.corrected_text != self.normalized_text:
            raise ValueError("corrected text requires a correction record")
        return self

    def with_correction(
        self,
        after_text: str,
        *,
        reviewer_id: str,
        corrected_at: datetime,
        reason_code: str,
        expected_before_sha256: str | None = None,
        expected_after_sha256: str | None = None,
    ) -> TextLayers:
        """Return a new layer set after checking optimistic hash boundaries."""
        before_sha256 = _sha256_text(self.corrected_text)
        after_sha256 = _sha256_text(after_text)
        if hmac.compare_digest(before_sha256, after_sha256):
            raise ValueError("correction must change text")
        if self.corrections and corrected_at <= self.corrections[-1].corrected_at:
            raise ValueError("correction timestamps must strictly increase")
        _check_expected_boundary(
            expected_before_sha256,
            before_sha256,
            label="before",
        )
        _check_expected_boundary(
            expected_after_sha256,
            after_sha256,
            label="after",
        )
        correction = Correction(
            reviewer_id=reviewer_id,
            corrected_at=corrected_at,
            reason_code=reason_code,
            before_sha256=before_sha256,
            after_sha256=after_sha256,
        )
        return self.model_copy(
            update={
                "corrected_text": after_text,
                "corrections": (*self.corrections, correction),
            }
        )


def normalize_text(
    text: str,
    *,
    repeated_lines: tuple[RepeatedLineEvidence, ...] = (),
    lexical_wraps: tuple[LexicalWrapEvidence, ...] = (),
) -> str:
    """Normalize a derived view without altering protected semantic entities."""
    raw_text_sha256 = _sha256_text(text)
    normalized = unicodedata.normalize("NFC", text).translate(_SAFE_SEPARATOR_TRANSLATION)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(
        _HORIZONTAL_WHITESPACE_RE.sub(" ", line).strip()
        for line in normalized.split("\n")
    )
    normalized = _apply_lexical_wraps(
        normalized,
        lexical_wraps=lexical_wraps,
        raw_text_sha256=raw_text_sha256,
    )
    normalized = _SOFT_LINE_END_HYPHEN_RE.sub("", normalized)

    lines = normalized.split("\n")
    removable_indexes: set[int] = set()
    for evidence in repeated_lines:
        if not evidence.supports_removal or not hmac.compare_digest(
            evidence.raw_text_sha256, raw_text_sha256
        ):
            continue
        evidence_text = _normalize_evidence_text(evidence.text)
        if evidence.line_index is not None:
            if (
                evidence.line_index < len(lines)
                and lines[evidence.line_index] == evidence_text
            ):
                removable_indexes.add(evidence.line_index)
            continue
        matching_indexes = [
            index for index, line in enumerate(lines) if line == evidence_text
        ]
        if len(matching_indexes) == 1:
            removable_indexes.add(matching_indexes[0])
    if removable_indexes:
        normalized = "\n".join(
            line for index, line in enumerate(lines) if index not in removable_indexes
        )

    normalized = _EXCESS_BLANK_LINES_RE.sub("\n\n", normalized)
    return normalized.strip("\n")


def _normalize_evidence_text(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text).translate(_SAFE_SEPARATOR_TRANSLATION)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return _HORIZONTAL_WHITESPACE_RE.sub(" ", normalized).strip()


def _apply_lexical_wraps(
    text: str,
    *,
    lexical_wraps: tuple[LexicalWrapEvidence, ...],
    raw_text_sha256: str,
) -> str:
    lines = text.split("\n")
    for evidence in sorted(
        lexical_wraps,
        key=lambda item: item.line_index,
        reverse=True,
    ):
        if not hmac.compare_digest(evidence.raw_text_sha256, raw_text_sha256):
            continue
        if evidence.line_index + 1 >= len(lines):
            continue
        left_line = lines[evidence.line_index]
        right_line = lines[evidence.line_index + 1]
        expected_left = f"{evidence.left_fragment}-"
        if not left_line.endswith(expected_left) or not right_line.startswith(
            evidence.right_fragment
        ):
            continue
        lines[evidence.line_index] = f"{left_line[:-1]}{right_line}"
        del lines[evidence.line_index + 1]
    return "\n".join(lines)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_expected_boundary(expected: str | None, actual: str, *, label: str) -> None:
    if expected is None:
        return
    if not _SHA256_RE.fullmatch(expected) or not hmac.compare_digest(expected, actual):
        raise ValueError(f"{label} SHA-256 boundary does not match text")
