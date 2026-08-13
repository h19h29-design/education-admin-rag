"""Deterministic, value-free metadata for annual parser integration runs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.ingestion.extract_common import revalidate_source_document
from src.ingestion.extract_native import (
    ExtractedPageRecord,
    NativeExtractionError,
    NativePageRecord,
    QuarantinedPageRecord,
    validate_native_page_record,
)
from src.ingestion.extract_ocr import (
    AppleVisionRuntimeProvenance,
    ExtractedAppleVisionOcrPageRecord,
    ExtractedOcrPageRecord,
    OcrExtractionError,
    OcrPageRecord,
    ParsedOcrPageRecord,
    QuarantinedAppleVisionOcrPageRecord,
    QuarantinedOcrPageRecord,
    parse_ocr_page_record,
)
from src.ingestion.manifest import (
    SourceDocument,
    SourceManifest,
    page_label,
)
from src.ingestion.ocr_authority import (
    OcrAuthorityEntry,
    OcrAuthorityLock,
    OcrAuthorityLockError,
    load_ocr_authority_lock,
)
from src.ingestion.parse_2020 import parse_document as parse_2020_document
from src.ingestion.parse_2021_2022 import (
    parse_document as parse_2021_2022_document,
)
from src.ingestion.parse_2023 import parse_document as parse_2023_document
from src.ingestion.parse_2024_2025 import (
    parse_document as parse_2024_2025_document,
)
from src.ingestion.parse_common import (
    ParserContractError,
    ParseResult,
    ParserPage,
    VerifiedPageRolePolicy,
    canonical_result_bytes,
    parser_page_from_native_record,
    parser_page_from_ocr_record,
)

_PAGE_SET_HASH_PREFIX = b"sen-qa-page-set-v1\0"
_LAYOUT_BINDING_HASH_PREFIX = b"sen-qa-layout-binding-v1\0"
_LAYOUT_EVIDENCE_HASH_PREFIX = b"sen-qa-layout-evidence-v1\0"
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_MAX_BYTES = 1024 * 1024
_INPUT_JSONL_MAX_BYTES = 512 * 1024 * 1024
_JSONL_RECORD_MAX_BYTES = 8 * 1024 * 1024
_FILE_READ_CHUNK_BYTES = 1024 * 1024

ParseMetadataErrorCode = Literal[
    "authority_invalid",
    "manifest_invalid",
    "selection_invalid",
    "input_invalid",
    "policy_mismatch",
    "image_digest_invalid",
    "parse_failed",
]


class ParseMetadataError(ValueError):
    """Fixed-code error that never retains untrusted values or source paths."""

    def __init__(self, code: ParseMetadataErrorCode) -> None:
        self.code = code
        super().__init__(code)


class MetadataModel(BaseModel):
    """Strict immutable base for the public diagnostic contract."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class PageSetMetadata(MetadataModel):
    count: int = Field(ge=1)
    first: int = Field(ge=1)
    last: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HashMetadata(MetadataModel):
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parse_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ParseMetadata(MetadataModel):
    """Only hashes, stable identifiers, and aggregate counters may be emitted."""

    metadata_schema: Literal["sen-qa-parse-metadata-v1"] = "sen-qa-parse-metadata-v1"
    doc_id: str = Field(min_length=1)
    edition_year: int
    extraction_source: Literal["native", "ocr"]
    page_set: PageSetMetadata
    hashes: HashMetadata
    record_counts: dict[str, int]
    record_quarantine_reason_counts: dict[str, int]
    case_type_counts: dict[str, int]
    parser_quarantine_reason_counts: dict[str, int]
    transition_role_counts: dict[str, int]
    missing_required_role_counts: dict[str, int]
    role_absence_counts: dict[str, int]
    review_counts: dict[str, int]
    eligibility_counts: dict[str, int]
    layout_evidence_counts: dict[str, int]
    layout_sampling_counts: dict[str, int]
    layout_region_count: int = Field(ge=0)
    layout_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def has_only_fixed_nonnegative_counter_keys(self) -> Self:
        contracts = (
            (self.record_counts, {"extracted", "quarantined", "total"}),
            (
                self.record_quarantine_reason_counts,
                {
                    "ocr-adapter-failed",
                    "ocr-provenance-invalid",
                    "page-extraction-failed",
                    "page-render-failed",
                },
            ),
            (self.case_type_counts, {"audit", "qa", "total"}),
            (
                self.parser_quarantine_reason_counts,
                {
                    "ambiguous_boundary",
                    "ocr-adapter-failed",
                    "ocr-provenance-invalid",
                    "page-extraction-failed",
                    "page-render-failed",
                },
            ),
            (
                self.transition_role_counts,
                {"cover", "credits", "domain", "law_list", "part", "subtopic", "toc"},
            ),
            (
                self.missing_required_role_counts,
                {
                    "audit_answer",
                    "audit_facts",
                    "audit_title",
                    "qa_answer",
                    "qa_question",
                    "qa_title",
                },
            ),
            (
                self.role_absence_counts,
                {
                    "answer",
                    "basis",
                    "facts",
                    "question",
                    "situation",
                    "target",
                    "title",
                },
            ),
            (
                self.review_counts,
                {
                    "critical_not_applicable",
                    "critical_sampling_required",
                    "critical_unverified",
                    "review_machine_extracted",
                    "review_needs_review",
                    "upstream_machine_extracted",
                    "upstream_needs_review",
                },
            ),
            (
                self.eligibility_counts,
                {
                    "answer_eligible",
                    "answer_ineligible",
                    "search_eligible",
                    "search_ineligible",
                },
            ),
            (
                self.layout_evidence_counts,
                {
                    "detected",
                    "failed",
                    "no_evidence",
                    "not_applicable",
                    "not_detected",
                    "unavailable",
                },
            ),
            (
                self.layout_sampling_counts,
                {"all_cases_required", "no_segment", "sampling_required"},
            ),
        )
        if any(
            set(counter) != expected
            or any(type(count) is not int or count < 0 for count in counter.values())
            for counter, expected in contracts
        ):
            raise ValueError("parse metadata counter contract is invalid")
        record_total = self.record_counts["total"]
        quarantined = self.record_counts["quarantined"]
        case_total = self.case_type_counts["total"]
        audit_total = self.case_type_counts["audit"]
        qa_total = self.case_type_counts["qa"]
        if (
            record_total != self.record_counts["extracted"] + quarantined
            or record_total != self.page_set.count
            or case_total != audit_total + qa_total
            or sum(self.record_quarantine_reason_counts.values()) != quarantined
            or sum(self.layout_evidence_counts.values()) != record_total
            or self.page_set.first > self.page_set.last
            or self.page_set.count > self.page_set.last - self.page_set.first + 1
        ):
            raise ValueError("parse metadata aggregate contract is invalid")
        if self.extraction_source == "native":
            if (
                any(self.layout_sampling_counts.values())
                or self.page_set.first != 1
                or self.page_set.count != self.page_set.last
            ):
                raise ValueError("parse metadata source contract is invalid")
        elif sum(self.layout_sampling_counts.values()) != record_total:
            raise ValueError("parse metadata source contract is invalid")
        if (
            self.eligibility_counts["search_eligible"] != 0
            or self.eligibility_counts["answer_eligible"] != 0
            or self.eligibility_counts["search_ineligible"] != case_total
            or self.eligibility_counts["answer_ineligible"] != case_total
        ):
            raise ValueError("parse metadata eligibility contract is invalid")
        review_partitions = (
            (
                "critical_not_applicable",
                "critical_sampling_required",
                "critical_unverified",
            ),
            ("review_machine_extracted", "review_needs_review"),
            ("upstream_machine_extracted", "upstream_needs_review"),
        )
        if any(
            sum(self.review_counts[key] for key in partition) != case_total
            for partition in review_partitions
        ):
            raise ValueError("parse metadata review contract is invalid")
        missing_bounds = {
            "audit_answer": audit_total,
            "audit_facts": audit_total,
            "audit_title": audit_total,
            "qa_answer": qa_total,
            "qa_question": qa_total,
            "qa_title": qa_total,
        }
        if any(
            self.missing_required_role_counts[key] > bound
            for key, bound in missing_bounds.items()
        ) or any(count > case_total for count in self.role_absence_counts.values()):
            raise ValueError("parse metadata role contract is invalid")
        return self


@dataclass(frozen=True, slots=True, init=False)
class VerifiedParseRun:
    """One manifest/input-bound parse result retained for review staging."""

    document: SourceDocument
    records: tuple[PageRecord, ...]
    result: ParseResult
    pages: tuple[ParserPage, ...]
    manifest_bytes: bytes
    input_bytes: bytes


class _DuplicateJsonKey(ValueError):
    pass


def _duplicate_rejecting_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _raise(code: ParseMetadataErrorCode) -> NoReturn:
    raise ParseMetadataError(code) from None


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> bytes | None:
    """Read one stable regular-file descriptor without following path changes."""
    if type(max_bytes) is not int or max_bytes < 0:
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOCTTY", 0)
    )
    descriptor: int | None = None
    content: bytes | None = None
    close_failed = False
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            content = None
        else:
            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                byte_count = min(
                    _FILE_READ_CHUNK_BYTES,
                    max_bytes + 1 - total,
                )
                chunk = os.read(descriptor, byte_count)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            after = os.fstat(descriptor)
            stable = (
                stat.S_ISREG(after.st_mode)
                and before.st_dev == after.st_dev
                and before.st_ino == after.st_ino
                and before.st_mode == after.st_mode
                and before.st_size == after.st_size == total
                and before.st_mtime_ns == after.st_mtime_ns
                and before.st_ctime_ns == after.st_ctime_ns
            )
            if total <= max_bytes and stable:
                content = b"".join(chunks)
    except (OSError, OverflowError, TypeError, ValueError):
        content = None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                close_failed = True
    return None if close_failed else content


def _manifest(path: Path, edition_year: int) -> tuple[SourceDocument, bytes] | None:
    raw = _read_bounded_regular_file(path, max_bytes=_MANIFEST_MAX_BYTES)
    if raw is None or _json_object(raw) is None:
        return None
    try:
        manifest = SourceManifest.model_validate_json(raw)
    except (RecursionError, OverflowError, TypeError, ValueError):
        return None
    selected = tuple(
        document
        for document in manifest.documents
        if document.edition_year == edition_year
    )
    if len(selected) != 1:
        return None
    return selected[0], raw


def _json_object(line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(
            line,
            object_pairs_hook=_duplicate_rejecting_object,
        )
    except (
        RecursionError,
        OverflowError,
        UnicodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        TypeError,
    ):
        return None
    return value if type(value) is dict else None


def _load_native_record(line: bytes) -> NativePageRecord | None:
    payload = _json_object(line)
    if payload is None:
        return None
    model: type[ExtractedPageRecord | QuarantinedPageRecord]
    if payload.get("status") == "extracted":
        model = ExtractedPageRecord
    elif payload.get("status") == "quarantined":
        model = QuarantinedPageRecord
    else:
        return None
    try:
        parsed = model.model_validate_json(line)
    except (RecursionError, OverflowError, TypeError, ValueError):
        return None
    try:
        return validate_native_page_record(parsed)
    except NativeExtractionError:
        return None


def _bounded_jsonl_lines(raw: bytes) -> tuple[bytes, ...] | None:
    if not raw or not raw.endswith(b"\n") or b"\r" in raw:
        return None
    lines: list[bytes] = []
    start = 0
    while start < len(raw):
        search_end = min(len(raw), start + _JSONL_RECORD_MAX_BYTES + 1)
        end = raw.find(b"\n", start, search_end)
        if end < 0:
            return None
        record_size = end - start
        if record_size < 1 or record_size > _JSONL_RECORD_MAX_BYTES:
            return None
        lines.append(raw[start:end])
        start = end + 1
    return tuple(lines) if lines else None


def _load_native_jsonl(raw: bytes) -> tuple[NativePageRecord, ...] | None:
    lines = _bounded_jsonl_lines(raw)
    if lines is None:
        return None
    records = tuple(_load_native_record(line) for line in lines)
    if any(record is None for record in records):
        return None
    return tuple(record for record in records if record is not None)


def _load_ocr_record(line: bytes) -> ParsedOcrPageRecord | None:
    try:
        return parse_ocr_page_record(line)
    except (OcrExtractionError, RecursionError, OverflowError, TypeError, ValueError):
        return None


def _load_ocr_jsonl(raw: bytes) -> tuple[ParsedOcrPageRecord, ...] | None:
    lines = _bounded_jsonl_lines(raw)
    if lines is None:
        return None
    records = tuple(_load_ocr_record(line) for line in lines)
    if any(record is None for record in records):
        return None
    checked = tuple(record for record in records if record is not None)
    if len({record.schema_version for record in checked}) != 1:
        return None
    return checked


def _expected_page_label(document: SourceDocument, index: int) -> str | None:
    label = page_label(document.page_numbering, index)
    return str(label) if label is not None else None


def _native_records_match_document(
    records: tuple[NativePageRecord, ...], document: SourceDocument
) -> bool:
    return (
        document.extraction_method == "native"
        and len(records) == document.pdf_page_count
        and all(
            record.pdf_page_index == expected_index
            and record.doc_id == document.doc_id
            and record.edition_year == document.edition_year
            and record.source_sha256 == document.sha256
            and record.document_pdf_page_count == document.pdf_page_count
            and record.page_label
            == _expected_page_label(document, record.pdf_page_index)
            for expected_index, record in enumerate(records, start=1)
        )
    )


def _parse_page_selection(value: str, *, pdf_page_count: int) -> tuple[int, ...] | None:
    digit_count = len(str(pdf_page_count))
    maximum_length = pdf_page_count * (digit_count * 2 + 2)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum_length
        or value.count(",") + 1 > pdf_page_count
        or re.fullmatch(r"[0-9,-]+", value) is None
    ):
        return None
    intervals: list[tuple[int, int]] = []
    selected_count = 0
    previous_end = 0
    for part in value.split(","):
        if "-" in part:
            bounds = part.split("-")
            if (
                len(bounds) != 2
                or not all(bound.isdecimal() for bound in bounds)
                or any(len(bound) > digit_count for bound in bounds)
            ):
                return None
            start, end = (int(bound) for bound in bounds)
        elif part.isdecimal():
            if len(part) > digit_count:
                return None
            start = end = int(part)
        else:
            return None
        interval_count = end - start + 1
        if (
            start < 1
            or end < start
            or end > pdf_page_count
            or start <= previous_end
            or selected_count + interval_count > pdf_page_count
        ):
            return None
        intervals.append((start, end))
        selected_count += interval_count
        previous_end = end
    return tuple(index for start, end in intervals for index in range(start, end + 1))


def _ocr_records_match_document(
    records: tuple[ParsedOcrPageRecord, ...],
    document: SourceDocument,
    indexes: tuple[int, ...],
) -> bool:
    return (
        document.extraction_method == "ocr"
        and document.render_dpi is not None
        and tuple(record.pdf_page_index for record in records) == indexes
        and indexes[-1] <= document.pdf_page_count
        and all(
            record.doc_id == document.doc_id
            and record.edition_year == document.edition_year
            and record.source_sha256 == document.sha256
            and record.render_dpi == document.render_dpi
            and record.page_label
            == _expected_page_label(document, record.pdf_page_index)
            for record in records
        )
    )


def _legacy_v2_digest_matches(
    records: tuple[ParsedOcrPageRecord, ...],
    expected_image_digest: str,
) -> bool:
    for record in records:
        if type(record) not in (ExtractedOcrPageRecord, QuarantinedOcrPageRecord):
            return False
        legacy_record = cast(OcrPageRecord, record)
        if not hmac.compare_digest(
            legacy_record.image_digest,
            expected_image_digest,
        ):
            return False
    return True


def _load_authority_lock(
    path: object,
    expected_sha256: object,
) -> OcrAuthorityLock | None:
    lock: OcrAuthorityLock | None = None
    try:
        lock = load_ocr_authority_lock(
            path,  # type: ignore[arg-type]
            expected_sha256=expected_sha256,  # type: ignore[arg-type]
        )
    except (
        OcrAuthorityLockError,
        RecursionError,
        OverflowError,
        TypeError,
        ValueError,
    ):
        lock = None
    return lock if type(lock) is OcrAuthorityLock else None


def _record_schema_and_engine(
    records: tuple[ParsedOcrPageRecord, ...],
) -> tuple[str, str] | None:
    if not records:
        return None
    schema_version = records[0].schema_version
    if schema_version == 2 and all(
        type(record) in (ExtractedOcrPageRecord, QuarantinedOcrPageRecord)
        for record in records
    ):
        return "sen-qa-ocr-page/v2", "paddleocr"
    if schema_version == 3 and all(
        type(record)
        in (
            ExtractedAppleVisionOcrPageRecord,
            QuarantinedAppleVisionOcrPageRecord,
        )
        for record in records
    ):
        return "sen-qa-ocr-page/v3", "apple-vision"
    return None


def _authority_entry(
    lock: OcrAuthorityLock,
    *,
    document: SourceDocument,
    record_schema: str,
    engine: str,
) -> OcrAuthorityEntry | None:
    if type(lock.entries) is not tuple:
        return None
    matches = tuple(
        entry
        for entry in lock.entries
        if type(entry) is OcrAuthorityEntry
        and type(entry.year) is int
        and entry.year == document.edition_year
    )
    if len(matches) != 1:
        return None
    entry = matches[0]
    expected = (
        document.doc_id,
        document.sha256,
        record_schema,
        engine,
    )
    actual = (
        entry.doc_id,
        entry.source_sha256,
        entry.record_schema,
        entry.engine,
    )
    if any(type(value) is not str for value in actual) or any(
        not hmac.compare_digest(left, right)
        for left, right in zip(actual, expected, strict=True)
    ):
        return None
    return entry


def _canonical_runtime_fingerprint(
    runtime: AppleVisionRuntimeProvenance,
) -> str | None:
    rendered: bytes | None = None
    try:
        if type(runtime) is not AppleVisionRuntimeProvenance:
            raise TypeError
        rendered = (
            json.dumps(
                runtime.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (RecursionError, TypeError, ValueError, UnicodeError):
        rendered = None
    if rendered is None:
        return None
    return "sha256:" + hashlib.sha256(rendered).hexdigest()


def _records_match_authority(
    records: tuple[ParsedOcrPageRecord, ...],
    *,
    document: SourceDocument,
    lock: OcrAuthorityLock,
) -> bool:
    selected = _record_schema_and_engine(records)
    if selected is None:
        return False
    record_schema, engine = selected
    entry = _authority_entry(
        lock,
        document=document,
        record_schema=record_schema,
        engine=engine,
    )
    if entry is None or type(entry.authority) is not str:
        return False
    if record_schema == "sen-qa-ocr-page/v2":
        _, separator, container_digest = entry.authority.rpartition("@")
        return (
            separator == "@"
            and _IMAGE_DIGEST_RE.fullmatch(container_digest) is not None
            and _legacy_v2_digest_matches(records, container_digest)
        )
    for record in records:
        if type(record) not in (
            ExtractedAppleVisionOcrPageRecord,
            QuarantinedAppleVisionOcrPageRecord,
        ):
            return False
        vision_record = cast(
            ExtractedAppleVisionOcrPageRecord | QuarantinedAppleVisionOcrPageRecord,
            record,
        )
        fingerprint = _canonical_runtime_fingerprint(vision_record.runtime_provenance)
        if fingerprint is None or not hmac.compare_digest(
            fingerprint,
            entry.authority,
        ):
            return False
    return True


def _page_role_policy(document: SourceDocument) -> VerifiedPageRolePolicy:
    body_start = document.page_numbering.body_start_pdf_page
    body_end = document.page_numbering.body_end_pdf_page
    cover = (1,) if body_start > 1 else ()
    toc = tuple(index for index in range(1, body_start) if index not in cover)
    credits = tuple(range(body_end + 1, document.pdf_page_count + 1))
    return VerifiedPageRolePolicy.from_source_document(
        document,
        cover_page_indexes=cover,
        toc_page_indexes=toc,
        credits_page_indexes=credits,
    )


def _page_set_metadata(indexes: tuple[int, ...]) -> PageSetMetadata:
    canonical = json.dumps(list(indexes), separators=(",", ":")).encode("ascii")
    return PageSetMetadata(
        count=len(indexes),
        first=indexes[0],
        last=indexes[-1],
        sha256=hashlib.sha256(_PAGE_SET_HASH_PREFIX + canonical).hexdigest(),
    )


def _fixed_counts(names: tuple[str, ...], values: Counter[str]) -> dict[str, int]:
    return {name: values[name] for name in names}


PageRecord = NativePageRecord | ParsedOcrPageRecord


def _layout_diagnostics(
    records: tuple[PageRecord, ...],
) -> tuple[dict[str, int], dict[str, int], int, str]:
    evidence = Counter[str]()
    sampling = Counter[str]()
    region_count = 0
    bindings: list[dict[str, object]] = []
    for record in records:
        if isinstance(
            record,
            (
                QuarantinedPageRecord,
                QuarantinedOcrPageRecord,
                QuarantinedAppleVisionOcrPageRecord,
            ),
        ):
            evidence["no_evidence"] += 1
            if isinstance(
                record,
                (QuarantinedOcrPageRecord, QuarantinedAppleVisionOcrPageRecord),
            ):
                sampling["no_segment"] += 1
            continue
        evidence[record.raw_page.layout_evidence.status] += 1
        region_count += len(record.raw_page.layout_evidence.regions)
        if isinstance(
            record,
            (ExtractedOcrPageRecord, ExtractedAppleVisionOcrPageRecord),
        ):
            segment = record.layout_segment_provenance
            sampling[
                segment.sampling_status if segment is not None else "no_segment"
            ] += 1
            canonical_evidence = json.dumps(
                record.raw_page.layout_evidence.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            bindings.append(
                {
                    "detector_version": record.raw_page.layout_evidence.detector_version,
                    "evidence_sha256": hashlib.sha256(
                        _LAYOUT_EVIDENCE_HASH_PREFIX + canonical_evidence
                    ).hexdigest(),
                    "evidence_status": record.raw_page.layout_evidence.status,
                    "page": record.pdf_page_index,
                    "region_count": len(record.raw_page.layout_evidence.regions),
                    "render_sha256": record.render_sha256,
                    "segment": (
                        segment.model_dump(mode="json") if segment is not None else None
                    ),
                }
            )
    canonical_bindings = json.dumps(
        bindings,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return (
        _fixed_counts(
            (
                "detected",
                "failed",
                "no_evidence",
                "not_applicable",
                "not_detected",
                "unavailable",
            ),
            evidence,
        ),
        _fixed_counts(
            ("all_cases_required", "no_segment", "sampling_required"),
            sampling,
        ),
        region_count,
        hashlib.sha256(_LAYOUT_BINDING_HASH_PREFIX + canonical_bindings).hexdigest(),
    )


def _metadata(
    *,
    document: SourceDocument,
    manifest_bytes: bytes,
    input_bytes: bytes,
    records: tuple[PageRecord, ...],
    result: ParseResult,
) -> ParseMetadata:
    record_status = Counter(record.status for record in records)
    record_reasons: Counter[str] = Counter(
        record.reason_code
        for record in records
        if isinstance(
            record,
            (
                QuarantinedPageRecord,
                QuarantinedOcrPageRecord,
                QuarantinedAppleVisionOcrPageRecord,
            ),
        )
    )
    case_types = Counter(case.case_type for case in result.cases)
    parser_reasons: Counter[str] = Counter(
        item.reason_code for item in result.quarantines
    )
    transition_roles: Counter[str] = Counter(item.role for item in result.transitions)
    review_counts: Counter[str] = Counter()
    for case in result.cases:
        review_counts[f"upstream_{case.upstream_review_status}"] += 1
        review_counts[f"critical_{case.critical_field_review}"] += 1
        review_counts[f"review_{case.review_status}"] += 1
    missing_required = Counter[str]()
    absence = Counter[str]()
    role_fields = {
        "title": "title",
        "question": "question",
        "answer": "answer",
        "facts": "facts",
        "basis": "basis_text",
        "target": "target_text",
        "situation": "situation_text",
    }
    for case in result.cases:
        for role, field_name in role_fields.items():
            if not getattr(case, field_name):
                absence[role] += 1
        required = (
            ("title", "facts", "answer")
            if case.case_type == "audit"
            else (
                "title",
                "question",
                "answer",
            )
        )
        for role in required:
            field_name = role_fields[role]
            if not getattr(case, field_name):
                missing_required[f"{case.case_type}_{role}"] += 1
    indexes = tuple(record.pdf_page_index for record in records)
    (
        layout_evidence_counts,
        layout_sampling_counts,
        layout_region_count,
        layout_binding_sha256,
    ) = _layout_diagnostics(records)
    return ParseMetadata(
        doc_id=document.doc_id,
        edition_year=document.edition_year,
        extraction_source=document.extraction_method,
        page_set=_page_set_metadata(indexes),
        hashes=HashMetadata(
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            source_sha256=document.sha256,
            input_sha256=hashlib.sha256(input_bytes).hexdigest(),
            parse_sha256=hashlib.sha256(canonical_result_bytes(result)).hexdigest(),
        ),
        record_counts={
            "extracted": record_status["extracted"],
            "quarantined": record_status["quarantined"],
            "total": len(records),
        },
        record_quarantine_reason_counts=_fixed_counts(
            (
                "ocr-adapter-failed",
                "ocr-provenance-invalid",
                "page-extraction-failed",
                "page-render-failed",
            ),
            record_reasons,
        ),
        case_type_counts={
            "audit": case_types["audit"],
            "qa": case_types["qa"],
            "total": len(result.cases),
        },
        parser_quarantine_reason_counts=_fixed_counts(
            (
                "ambiguous_boundary",
                "ocr-adapter-failed",
                "ocr-provenance-invalid",
                "page-extraction-failed",
                "page-render-failed",
            ),
            parser_reasons,
        ),
        transition_role_counts=_fixed_counts(
            ("cover", "credits", "domain", "law_list", "part", "subtopic", "toc"),
            transition_roles,
        ),
        missing_required_role_counts=_fixed_counts(
            (
                "audit_answer",
                "audit_facts",
                "audit_title",
                "qa_answer",
                "qa_question",
                "qa_title",
            ),
            missing_required,
        ),
        role_absence_counts=_fixed_counts(
            ("answer", "basis", "facts", "question", "situation", "target", "title"),
            absence,
        ),
        review_counts=_fixed_counts(
            (
                "critical_not_applicable",
                "critical_sampling_required",
                "critical_unverified",
                "review_machine_extracted",
                "review_needs_review",
                "upstream_machine_extracted",
                "upstream_needs_review",
            ),
            review_counts,
        ),
        eligibility_counts={
            "answer_eligible": sum(case.answer_eligible for case in result.cases),
            "answer_ineligible": sum(not case.answer_eligible for case in result.cases),
            "search_eligible": sum(case.search_eligible for case in result.cases),
            "search_ineligible": sum(not case.search_eligible for case in result.cases),
        },
        layout_evidence_counts=layout_evidence_counts,
        layout_sampling_counts=layout_sampling_counts,
        layout_region_count=layout_region_count,
        layout_binding_sha256=layout_binding_sha256,
    )


def _contiguous_runs(
    records: tuple[ParsedOcrPageRecord, ...],
) -> tuple[tuple[ParsedOcrPageRecord, ...], ...]:
    runs: list[list[ParsedOcrPageRecord]] = []
    for record in records:
        if not runs or record.pdf_page_index != runs[-1][-1].pdf_page_index + 1:
            runs.append([record])
        else:
            runs[-1].append(record)
    return tuple(tuple(run) for run in runs)


def _parse_ocr_document(
    records: tuple[ParsedOcrPageRecord, ...],
    *,
    document: SourceDocument,
) -> ParseResult:
    policy = _page_role_policy(document)
    results: list[ParseResult] = []
    for run in _contiguous_runs(records):
        if document.edition_year == 2023:
            results.append(
                parse_2023_document(
                    cast(tuple[OcrPageRecord, ...], run),
                    page_role_policy=policy,
                )
            )
        else:
            year = cast(Literal[2024, 2025], document.edition_year)
            results.append(
                parse_2024_2025_document(
                    cast(tuple[OcrPageRecord, ...], run),
                    edition_year=year,
                    page_role_policy=policy,
                )
            )
    return ParseResult(
        cases=tuple(case for result in results for case in result.cases),
        quarantines=tuple(item for result in results for item in result.quarantines),
        transitions=tuple(item for result in results for item in result.transitions),
    )


def build_parse_run(
    input_path: Path,
    *,
    manifest_path: Path,
    edition_year: int,
    pages: str,
    expected_image_digest: str | None = None,
    ocr_authority_lock_path: Path | None = None,
    expected_ocr_authority_lock_sha256: str | None = None,
) -> VerifiedParseRun:
    """Validate one extractor JSONL and retain its exact parser staging inputs."""
    if type(edition_year) is not int:
        _raise("selection_invalid")
    loaded_manifest = _manifest(manifest_path, edition_year)
    if loaded_manifest is None:
        _raise("manifest_invalid")
    manifest_document, manifest_bytes = loaded_manifest
    document = revalidate_source_document(manifest_document)
    if document is None:
        _raise("manifest_invalid")
    input_bytes = _read_bounded_regular_file(
        input_path,
        max_bytes=_INPUT_JSONL_MAX_BYTES,
    )
    if input_bytes is None:
        _raise("input_invalid")
    records: tuple[PageRecord, ...]
    if document.extraction_method == "native":
        if (
            ocr_authority_lock_path is not None
            or expected_ocr_authority_lock_sha256 is not None
        ):
            _raise("authority_invalid")
        if pages != "all":
            _raise("selection_invalid")
        native_records = _load_native_jsonl(input_bytes)
        if native_records is None:
            _raise("input_invalid")
        if not _native_records_match_document(native_records, document):
            _raise("policy_mismatch")
        records = native_records
    else:
        authority_supplied = (
            ocr_authority_lock_path is not None
            or expected_ocr_authority_lock_sha256 is not None
        )
        if authority_supplied and expected_image_digest is not None:
            _raise("authority_invalid")
        indexes = _parse_page_selection(
            pages,
            pdf_page_count=document.pdf_page_count,
        )
        if indexes is None or pages == "all":
            _raise("selection_invalid")
        authority_lock: OcrAuthorityLock | None = None
        if authority_supplied:
            if (
                ocr_authority_lock_path is None
                or expected_ocr_authority_lock_sha256 is None
            ):
                _raise("authority_invalid")
            authority_lock = _load_authority_lock(
                ocr_authority_lock_path,
                expected_ocr_authority_lock_sha256,
            )
            if authority_lock is None:
                _raise("authority_invalid")
        elif (
            type(expected_image_digest) is not str
            or _IMAGE_DIGEST_RE.fullmatch(expected_image_digest) is None
        ):
            _raise("image_digest_invalid")
        ocr_records = _load_ocr_jsonl(input_bytes)
        if ocr_records is None:
            _raise("input_invalid")
        if not _ocr_records_match_document(
            ocr_records,
            document,
            indexes,
        ):
            _raise("policy_mismatch")
        if authority_lock is not None:
            if not _records_match_authority(
                ocr_records,
                document=document,
                lock=authority_lock,
            ):
                _raise("policy_mismatch")
        else:
            if ocr_records[0].schema_version != 2:
                _raise("image_digest_invalid")
            if not _legacy_v2_digest_matches(
                ocr_records,
                cast(str, expected_image_digest),
            ):
                _raise("policy_mismatch")
        records = ocr_records
    parse_failed = False
    try:
        if document.extraction_method == "native":
            native_for_parse = cast(tuple[NativePageRecord, ...], records)
            role_policy = _page_role_policy(document)
            if document.edition_year == 2020:
                result = parse_2020_document(
                    native_for_parse,
                    page_role_policy=role_policy,
                )
            else:
                native_year = cast(Literal[2021, 2022], document.edition_year)
                result = parse_2021_2022_document(
                    native_for_parse,
                    edition_year=native_year,
                    page_role_policy=role_policy,
                )
        else:
            result = _parse_ocr_document(
                cast(tuple[ParsedOcrPageRecord, ...], records),
                document=document,
            )
    except (RecursionError, OverflowError, ParserContractError, TypeError, ValueError):
        parse_failed = True
        result = ParseResult()
    if parse_failed:
        _raise("parse_failed")
    try:
        role_policy = _page_role_policy(document)
        if document.extraction_method == "native":
            pages_for_review = tuple(
                parser_page_from_native_record(
                    record,
                    page_role_policy=role_policy,
                )
                for record in cast(tuple[NativePageRecord, ...], records)
            )
        else:
            pages_for_review = tuple(
                parser_page_from_ocr_record(
                    record,
                    page_role_policy=role_policy,
                )
                for record in cast(tuple[ParsedOcrPageRecord, ...], records)
            )
    except (RecursionError, OverflowError, ParserContractError, TypeError, ValueError):
        _raise("parse_failed")
    verified = object.__new__(VerifiedParseRun)
    object.__setattr__(verified, "document", document)
    object.__setattr__(verified, "records", records)
    object.__setattr__(verified, "result", result)
    object.__setattr__(verified, "pages", pages_for_review)
    object.__setattr__(verified, "manifest_bytes", manifest_bytes)
    object.__setattr__(verified, "input_bytes", input_bytes)
    return verified


def build_parse_metadata(
    input_path: Path,
    *,
    manifest_path: Path,
    edition_year: int,
    pages: str,
    expected_image_digest: str | None = None,
    ocr_authority_lock_path: Path | None = None,
    expected_ocr_authority_lock_sha256: str | None = None,
) -> ParseMetadata:
    """Validate one extractor JSONL and return only aggregate parser diagnostics."""
    run = build_parse_run(
        input_path,
        manifest_path=manifest_path,
        edition_year=edition_year,
        pages=pages,
        expected_image_digest=expected_image_digest,
        ocr_authority_lock_path=ocr_authority_lock_path,
        expected_ocr_authority_lock_sha256=expected_ocr_authority_lock_sha256,
    )
    metadata: ParseMetadata | None = None
    try:
        metadata = _metadata(
            document=run.document,
            manifest_bytes=run.manifest_bytes,
            input_bytes=run.input_bytes,
            records=run.records,
            result=run.result,
        )
    except (RecursionError, OverflowError, TypeError, ValueError):
        metadata = None
    if metadata is None:
        _raise("parse_failed")
    return metadata


def canonical_metadata_bytes(metadata: ParseMetadata) -> bytes:
    """Render the fixed public metadata schema as sorted canonical JSON."""
    validated: ParseMetadata | None = None
    if type(metadata) is ParseMetadata:
        raw_fields = object.__getattribute__(metadata, "__dict__")
        if type(raw_fields) is dict and set(raw_fields) == set(
            ParseMetadata.model_fields
        ):
            fields = dict(raw_fields)
            page_set = fields["page_set"]
            hashes = fields["hashes"]
            if type(page_set) is PageSetMetadata and type(hashes) is HashMetadata:
                page_fields = object.__getattribute__(page_set, "__dict__")
                hash_fields = object.__getattribute__(hashes, "__dict__")
                if (
                    type(page_fields) is dict
                    and set(page_fields) == set(PageSetMetadata.model_fields)
                    and type(hash_fields) is dict
                    and set(hash_fields) == set(HashMetadata.model_fields)
                ):
                    try:
                        fields["page_set"] = PageSetMetadata.model_validate(
                            dict(page_fields)
                        )
                        fields["hashes"] = HashMetadata.model_validate(
                            dict(hash_fields)
                        )
                        validated = ParseMetadata.model_validate(fields)
                    except (RecursionError, OverflowError, TypeError, ValueError):
                        validated = None
    if validated is None:
        _raise("input_invalid")
    return json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
