"""Authority-bound deterministic segment sampling for manual corpus review.

Only identifiers, hashes, policy labels, and approved layout provenance cross
this boundary. Native documents are never grouped by year or filename because
those values are not authoritative layout segments.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn, Self

from src.corpus.models import PiiClass
from src.ingestion.manifest import NativeReviewLayoutSegment
from src.ingestion.parse_common import LayoutSegmentProvenance
from src.ingestion.review import ReviewReference, VerifiedCanonicalReviewRegistry

_RELEASE_RE = re.compile(r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_SAMPLE_DOMAIN = b"sen-qa-review-layout-sample-v1\0"
_MAX_AUTHORITY_BYTES = 16 * 1024 * 1024
_NATIVE_SEAM = (
    "native_layout_segment_provenance_missing_at_"
    "ParsedCaseCandidate.layout_segment_provenances"
)

SamplingOutcome = Literal["passed", "error"]
ExtractionSource = Literal["native", "ocr"]


class SamplingValidationError(ValueError):
    """A value-free sampling contract failure."""


def _fail(code: str) -> NoReturn:
    raise SamplingValidationError(code) from None


def _canonical(payload: object) -> bytes:
    try:
        raw = (
            json.dumps(
                payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (RecursionError, TypeError, UnicodeError, ValueError):
        _fail("sampling_input_invalid")
    if len(raw) > _MAX_AUTHORITY_BYTES:
        _fail("sampling_input_invalid")
    return raw


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _reference_payload(reference: ReviewReference) -> dict[str, str]:
    return {
        "case_id": reference.case_id,
        "content_sha256": reference.content_sha256,
    }


@dataclass(frozen=True, slots=True)
class SamplingCandidate:
    """Value-free case binding supplied by canonical staging."""

    reference: ReviewReference
    edition_year: int
    extraction_source: ExtractionSource
    pii_class: PiiClass
    review_status: Literal[
        "machine_extracted", "needs_review", "search_approved", "approved", "rejected"
    ]
    layout_segment_provenances: tuple[LayoutSegmentProvenance, ...]
    native_layout_segment: NativeReviewLayoutSegment | None = None
    doc_id: str | None = None
    source_sha256: str | None = None
    quarantined: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.reference) is not ReviewReference
            or isinstance(self.edition_year, bool)
            or not isinstance(self.edition_year, int)
            or self.extraction_source not in {"native", "ocr"}
            or self.pii_class
            not in {
                "none",
                "anonymized_case",
                "quasi_identifier",
                "public_credit",
                "restricted",
            }
            or self.review_status
            not in {
                "machine_extracted",
                "needs_review",
                "search_approved",
                "approved",
                "rejected",
            }
            or type(self.layout_segment_provenances) is not tuple
            or any(
                type(item) is not LayoutSegmentProvenance
                for item in self.layout_segment_provenances
            )
            or type(self.quarantined) is not bool
            or (
                self.native_layout_segment is not None
                and type(self.native_layout_segment) is not NativeReviewLayoutSegment
            )
            or (
                self.extraction_source == "ocr"
                and self.native_layout_segment is not None
            )
            or (
                self.native_layout_segment is not None
                and (
                    not self.doc_id
                    or self.source_sha256 is None
                    or _HASH_RE.fullmatch(self.source_sha256) is None
                    or self.layout_segment_provenances
                )
            )
        ):
            _fail("sampling_input_invalid")

    @property
    def approval_eligible(self) -> bool:
        return (
            not self.quarantined
            and self.pii_class not in {"public_credit", "restricted"}
            and self.review_status not in {"rejected", "search_approved", "approved"}
        )


@dataclass(frozen=True, slots=True)
class SegmentLayoutAuthority:
    """One exact OCR registry binding plus every rendered member page."""

    segment_id: str
    doc_id: str
    edition_year: int
    segment_key: str
    segment_start_pdf_page: int
    segment_end_pdf_page: int
    registry_policy_version: str
    registry_sha256: str
    detector_version: str
    source_sha256: str
    sampling_status: str
    pages: tuple[tuple[int, int, str], ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "detector_version": self.detector_version,
            "doc_id": self.doc_id,
            "edition_year": self.edition_year,
            "layout_provenance": [
                {
                    "pdf_page_index": page,
                    "region_count": count,
                    "render_sha256": render,
                }
                for page, count, render in self.pages
            ],
            "registry_policy_version": self.registry_policy_version,
            "registry_sha256": self.registry_sha256,
            "sampling_status": self.sampling_status,
            "segment_end_pdf_page": self.segment_end_pdf_page,
            "segment_id": self.segment_id,
            "segment_key": self.segment_key,
            "segment_start_pdf_page": self.segment_start_pdf_page,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class NativeSegmentLayoutAuthority:
    """One exact source-manifest native layout segment."""

    segment_id: str
    doc_id: str
    edition_year: int
    source_sha256: str
    manifest_sha256: str
    segment_start_pdf_page: int
    segment_end_pdf_page: int
    sampling_policy: str
    policy_version: str

    def to_payload(self) -> dict[str, object]:
        return {
            "doc_id": self.doc_id,
            "edition_year": self.edition_year,
            "manifest_sha256": self.manifest_sha256,
            "policy_version": self.policy_version,
            "sampling_policy": self.sampling_policy,
            "segment_end_pdf_page": self.segment_end_pdf_page,
            "segment_id": self.segment_id,
            "segment_start_pdf_page": self.segment_start_pdf_page,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class SamplingSegment:
    layout: SegmentLayoutAuthority | NativeSegmentLayoutAuthority
    members: tuple[ReviewReference, ...]
    excluded: tuple[tuple[str, str], ...]

    def to_payload(self) -> dict[str, object]:
        return {
            **self.layout.to_payload(),
            "excluded": [
                {"case_id": case_id, "reason_code": reason}
                for case_id, reason in self.excluded
            ],
            "members": [_reference_payload(item) for item in self.members],
        }


@dataclass(frozen=True, slots=True)
class SamplingInventory:
    release_id: str
    registry_sha256: str
    parser_authority_sha256: str
    raw_authority_sha256: str
    manifest_sha256: str
    segments: tuple[SamplingSegment, ...]
    all_fields_case_ids: tuple[str, ...]
    blockers: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "all_fields_case_ids": list(self.all_fields_case_ids),
            "blockers": list(self.blockers),
            "parser_authority_sha256": self.parser_authority_sha256,
            "raw_authority_sha256": self.raw_authority_sha256,
            "manifest_sha256": self.manifest_sha256,
            "registry_sha256": self.registry_sha256,
            "release_id": self.release_id,
            "schema_version": "sen-qa-review-segment-inventory/v1",
            "segments": [segment.to_payload() for segment in self.segments],
        }

    def to_bytes(self) -> bytes:
        return _canonical(self.to_payload())

    @property
    def sha256(self) -> str:
        return _digest(self.to_bytes())


@dataclass(frozen=True, slots=True)
class SegmentSamplePlan:
    release_id: str
    inventory_sha256: str
    registry_sha256: str
    parser_authority_sha256: str
    raw_authority_sha256: str
    manifest_sha256: str
    segment_id: str
    sample_rate: float
    minimum_per_segment: int
    members: tuple[ReviewReference, ...]
    selected: tuple[ReviewReference, ...]

    @property
    def sample_size(self) -> int:
        return len(self.selected)

    def to_payload(self) -> dict[str, object]:
        return {
            "inventory_sha256": self.inventory_sha256,
            "members": [_reference_payload(item) for item in self.members],
            "minimum_per_segment": self.minimum_per_segment,
            "manifest_sha256": self.manifest_sha256,
            "parser_authority_sha256": self.parser_authority_sha256,
            "raw_authority_sha256": self.raw_authority_sha256,
            "registry_sha256": self.registry_sha256,
            "release_id": self.release_id,
            "sample_rate": self.sample_rate,
            "schema_version": "sen-qa-review-sample-plan/v1",
            "segment_id": self.segment_id,
            "selected": [_reference_payload(item) for item in self.selected],
        }

    def to_bytes(self) -> bytes:
        return _canonical(self.to_payload())

    @property
    def sha256(self) -> str:
        return _digest(self.to_bytes())


@dataclass(frozen=True, slots=True)
class SamplingAuthority:
    inventory: SamplingInventory
    plans: tuple[SegmentSamplePlan, ...]

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "inventory": self.inventory.to_payload(),
                "inventory_sha256": self.inventory.sha256,
                "plans": [
                    {**plan.to_payload(), "plan_sha256": plan.sha256}
                    for plan in self.plans
                ],
                "schema_version": "sen-qa-review-sampling-authority/v1",
            }
        )

    @property
    def sha256(self) -> str:
        return _digest(self.to_bytes())


@dataclass(frozen=True, slots=True, init=False)
class VerifiedSamplingAuthority:
    """Init-disabled sampling authority carrying an external file seal."""

    _authority: SamplingAuthority
    _canonical_bytes: bytes
    external_sha256: str

    def __new__(cls) -> Self:
        raise TypeError("sampling authority requires external verification")

    @property
    def inventory(self) -> SamplingInventory:
        return self._authority.inventory

    @property
    def plans(self) -> tuple[SegmentSamplePlan, ...]:
        return self._authority.plans

    def to_bytes(self) -> bytes:
        return self._authority.to_bytes()


def _verified_sampling_authority(
    authority: SamplingAuthority, canonical_bytes: bytes, external_sha256: str
) -> VerifiedSamplingAuthority:
    wrapper = object.__new__(VerifiedSamplingAuthority)
    object.__setattr__(wrapper, "_authority", authority)
    object.__setattr__(wrapper, "_canonical_bytes", canonical_bytes)
    object.__setattr__(wrapper, "external_sha256", external_sha256)
    return wrapper


def _exclusion_reason(candidate: SamplingCandidate) -> str:
    if candidate.quarantined:
        return "quarantined"
    if candidate.pii_class == "restricted":
        return "restricted"
    if candidate.pii_class == "public_credit":
        return "public_credit"
    return "terminal_review_state"


def _layout_key(provenance: LayoutSegmentProvenance) -> tuple[object, ...]:
    return (
        provenance.segment_id,
        provenance.doc_id,
        provenance.edition_year,
        provenance.segment_key,
        provenance.segment_start_pdf_page,
        provenance.segment_end_pdf_page,
        provenance.registry_policy_version,
        provenance.registry_sha256,
        provenance.detector_version,
        provenance.source_sha256,
        provenance.sampling_status,
    )


def _layout_authority(
    provenances: tuple[LayoutSegmentProvenance, ...],
) -> SegmentLayoutAuthority:
    if not provenances or len({_layout_key(item) for item in provenances}) != 1:
        _fail("layout_segment_authority_invalid")
    first = provenances[0]
    pages = tuple(
        sorted(
            {
                (item.pdf_page_index, item.region_count, item.render_sha256)
                for item in provenances
            }
        )
    )
    if len({page for page, _, _ in pages}) != len(pages):
        _fail("layout_segment_authority_invalid")
    return SegmentLayoutAuthority(
        segment_id=first.segment_id,
        doc_id=first.doc_id,
        edition_year=first.edition_year,
        segment_key=first.segment_key,
        segment_start_pdf_page=first.segment_start_pdf_page,
        segment_end_pdf_page=first.segment_end_pdf_page,
        registry_policy_version=first.registry_policy_version,
        registry_sha256=first.registry_sha256,
        detector_version=first.detector_version,
        source_sha256=first.source_sha256,
        sampling_status=first.sampling_status,
        pages=pages,
    )


def _selection_score(
    *, inventory: SamplingInventory, segment_id: str, reference: ReviewReference
) -> str:
    payload = _canonical(
        {
            "case_id": reference.case_id,
            "content_sha256": reference.content_sha256,
            "parser_authority_sha256": inventory.parser_authority_sha256,
            "raw_authority_sha256": inventory.raw_authority_sha256,
            "manifest_sha256": inventory.manifest_sha256,
            "registry_sha256": inventory.registry_sha256,
            "release_id": inventory.release_id,
            "segment_id": segment_id,
        }
    )
    return _digest(_SAMPLE_DOMAIN + payload)


def build_sampling_authority(
    *,
    release_id: str,
    registry: VerifiedCanonicalReviewRegistry,
    parser_authority_sha256: str,
    raw_authority_sha256: str,
    manifest_sha256: str,
    candidates: tuple[SamplingCandidate, ...],
) -> SamplingAuthority:
    """Build canonical inventory and deterministic 10%/minimum-five plans."""
    if (
        type(release_id) is not str
        or _RELEASE_RE.fullmatch(release_id) is None
        or type(registry) is not VerifiedCanonicalReviewRegistry
        or type(parser_authority_sha256) is not str
        or _HASH_RE.fullmatch(parser_authority_sha256) is None
        or type(raw_authority_sha256) is not str
        or _HASH_RE.fullmatch(raw_authority_sha256) is None
        or type(manifest_sha256) is not str
        or _HASH_RE.fullmatch(manifest_sha256) is None
        or type(candidates) is not tuple
        or not candidates
        or any(type(item) is not SamplingCandidate for item in candidates)
    ):
        _fail("sampling_input_invalid")
    references = {item.case_id: item for item in registry.cases}
    if len(candidates) != len({item.reference.case_id for item in candidates}):
        _fail("sampling_input_invalid")
    for candidate in candidates:
        reference = references.get(candidate.reference.case_id)
        if (
            reference is None
            or reference.content_sha256 != candidate.reference.content_sha256
        ):
            _fail("sampling_registry_drift")

    all_fields = tuple(
        sorted(
            candidate.reference.case_id
            for candidate in candidates
            if candidate.edition_year in {2023, 2024} and candidate.approval_eligible
        )
    )
    blockers: set[str] = set()
    grouped: dict[tuple[object, ...], list[SamplingCandidate]] = {}
    for candidate in candidates:
        if candidate.edition_year not in {2020, 2021, 2022, 2025}:
            continue
        if candidate.extraction_source == "native":
            native_spec = candidate.native_layout_segment
            if native_spec is None:
                if candidate.approval_eligible:
                    blockers.add(_NATIVE_SEAM)
                continue
            if candidate.doc_id is None or candidate.source_sha256 is None:
                _fail("native_layout_segment_authority_invalid")
            native_key: tuple[object, ...] = (
                "native",
                candidate.doc_id,
                candidate.edition_year,
                candidate.source_sha256,
                native_spec.segment_id,
                native_spec.start_pdf_page,
                native_spec.end_pdf_page,
                native_spec.sampling_policy,
                native_spec.policy_version,
            )
            grouped.setdefault(native_key, []).append(candidate)
            continue
        if candidate.edition_year != 2025 or not candidate.layout_segment_provenances:
            if candidate.approval_eligible:
                _fail("layout_segment_authority_invalid")
            continue
        keys = {_layout_key(item) for item in candidate.layout_segment_provenances}
        if len(keys) != 1:
            _fail("layout_segment_authority_invalid")
        grouped.setdefault(("ocr", *next(iter(keys))), []).append(candidate)

    segments: list[SamplingSegment] = []
    for key in sorted(grouped, key=lambda item: str(item)):
        group = grouped[key]
        if key[0] == "native":
            first_candidate = group[0]
            native = first_candidate.native_layout_segment
            if (
                native is None
                or first_candidate.doc_id is None
                or first_candidate.source_sha256 is None
            ):
                _fail("native_layout_segment_authority_invalid")
            layout: SegmentLayoutAuthority | NativeSegmentLayoutAuthority = (
                NativeSegmentLayoutAuthority(
                    segment_id=native.segment_id,
                    doc_id=first_candidate.doc_id,
                    edition_year=first_candidate.edition_year,
                    source_sha256=first_candidate.source_sha256,
                    manifest_sha256=manifest_sha256,
                    segment_start_pdf_page=native.start_pdf_page,
                    segment_end_pdf_page=native.end_pdf_page,
                    sampling_policy=native.sampling_policy,
                    policy_version=native.policy_version,
                )
            )
        else:
            layout = _layout_authority(
                tuple(
                    item
                    for candidate in group
                    for item in candidate.layout_segment_provenances
                )
            )
        members = tuple(
            sorted(
                (
                    candidate.reference
                    for candidate in group
                    if candidate.approval_eligible
                ),
                key=lambda item: item.case_id,
            )
        )
        excluded = tuple(
            sorted(
                (candidate.reference.case_id, _exclusion_reason(candidate))
                for candidate in group
                if not candidate.approval_eligible
            )
        )
        segments.append(
            SamplingSegment(layout=layout, members=members, excluded=excluded)
        )
    inventory = SamplingInventory(
        release_id=release_id,
        registry_sha256=registry.fingerprint_sha256,
        parser_authority_sha256=parser_authority_sha256,
        raw_authority_sha256=raw_authority_sha256,
        manifest_sha256=manifest_sha256,
        segments=tuple(segments),
        all_fields_case_ids=all_fields,
        blockers=tuple(sorted(blockers)),
    )
    plans: list[SegmentSamplePlan] = []
    for inventory_segment in inventory.segments:
        if not inventory_segment.members:
            continue
        sample_size = min(
            len(inventory_segment.members),
            max(5, math.ceil(len(inventory_segment.members) * 0.10)),
        )
        selected = tuple(
            sorted(
                inventory_segment.members,
                key=lambda reference: (
                    _selection_score(
                        inventory=inventory,
                        segment_id=inventory_segment.layout.segment_id,
                        reference=reference,
                    ),
                    reference.case_id,
                ),
            )[:sample_size]
        )
        plans.append(
            SegmentSamplePlan(
                release_id=release_id,
                inventory_sha256=inventory.sha256,
                registry_sha256=registry.fingerprint_sha256,
                parser_authority_sha256=parser_authority_sha256,
                raw_authority_sha256=raw_authority_sha256,
                manifest_sha256=manifest_sha256,
                segment_id=inventory_segment.layout.segment_id,
                sample_rate=0.10,
                minimum_per_segment=5,
                members=inventory_segment.members,
                selected=selected,
            )
        )
    return SamplingAuthority(
        inventory=inventory,
        plans=tuple(sorted(plans, key=lambda item: item.segment_id)),
    )


@dataclass(frozen=True, slots=True)
class SampleReviewEvent:
    sequence: int
    plan_sha256: str
    segment_id: str
    case_id: str
    reviewed_content_sha256: str
    reviewer_id: str
    outcome: SamplingOutcome
    reason_code: str

    def to_payload(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "outcome": self.outcome,
            "plan_sha256": self.plan_sha256,
            "reason_code": self.reason_code,
            "reviewed_content_sha256": self.reviewed_content_sha256,
            "reviewer_id": self.reviewer_id,
            "schema_version": "sen-qa-review-sample-event/v1",
            "segment_id": self.segment_id,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class SampleSearchApprovalManifest:
    release_id: str
    registry_sha256: str
    inventory_sha256: str
    plan_sha256: str
    sample_events_sha256: str
    segment_id: str
    members: tuple[ReviewReference, ...]
    search_eligible: Literal[True] = True
    answer_eligible: Literal[False] = False

    def to_bytes(self) -> bytes:
        return _canonical(
            {
                "answer_eligible": self.answer_eligible,
                "inventory_sha256": self.inventory_sha256,
                "members": [_reference_payload(item) for item in self.members],
                "plan_sha256": self.plan_sha256,
                "registry_sha256": self.registry_sha256,
                "release_id": self.release_id,
                "sample_events_sha256": self.sample_events_sha256,
                "schema_version": "sen-qa-review-sample-search-manifest/v1",
                "search_eligible": self.search_eligible,
                "segment_id": self.segment_id,
            }
        )


@dataclass(frozen=True, slots=True)
class CriticalFieldsEscalation:
    release_id: str
    plan_sha256: str
    segment_id: str
    members: tuple[ReviewReference, ...]
    mode: Literal["critical-fields-all"] = "critical-fields-all"


_DirectoryIdentity = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _ParentDirectoryWalk:
    descriptor: int
    absolute_path: str
    leaf_name: str
    identities: tuple[_DirectoryIdentity, ...]


def _directory_identity(metadata: os.stat_result) -> _DirectoryIdentity:
    return (metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode))


def _open_parent_directory(path: Path) -> _ParentDirectoryWalk | None:
    descriptor: int | None = None
    pending_descriptor: int | None = None
    try:
        if (
            type(path) is not type(Path())
            or not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "O_NOFOLLOW")
        ):
            return None
        absolute_path = os.path.abspath(os.fspath(path))
        absolute = Path(absolute_path)
        if (
            not absolute.is_absolute()
            or absolute.name in {"", ".", ".."}
            or not absolute.parent.parts
            or absolute.parent.parts[0] != os.sep
        ):
            return None
        flags = (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(os.sep, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            return None
        identities = [_directory_identity(metadata)]
        for component in absolute.parent.parts[1:]:
            if component in {"", ".", ".."} or os.sep in component:
                return None
            pending_descriptor = os.open(component, flags, dir_fd=descriptor)
            next_metadata = os.fstat(pending_descriptor)
            if not stat.S_ISDIR(next_metadata.st_mode):
                os.close(pending_descriptor)
                pending_descriptor = None
                return None
            os.close(descriptor)
            descriptor = pending_descriptor
            pending_descriptor = None
            identities.append(_directory_identity(next_metadata))
        walk = _ParentDirectoryWalk(
            descriptor=descriptor,
            absolute_path=absolute_path,
            leaf_name=absolute.name,
            identities=tuple(identities),
        )
        descriptor = None
        return walk
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    finally:
        if pending_descriptor is not None:
            try:
                os.close(pending_descriptor)
            except OSError:
                pass
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _parent_directory_is_current(walk: _ParentDirectoryWalk) -> bool:
    current = _open_parent_directory(Path(walk.absolute_path))
    if current is None:
        return False
    try:
        return (
            current.leaf_name == walk.leaf_name
            and current.identities == walk.identities
        )
    finally:
        try:
            os.close(current.descriptor)
        except OSError:
            pass


def _read_private_authority(path: Path) -> bytes | None:
    descriptor: int | None = None
    parent = _open_parent_directory(path)
    if parent is None:
        return None
    try:
        flags = (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(parent.leaf_name, flags, dir_fd=parent.descriptor)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > _MAX_AUTHORITY_BYTES
        ):
            return None
        chunks: list[bytes] = []
        remaining = _MAX_AUTHORITY_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            len(raw) != before.st_size
            or len(raw) > _MAX_AUTHORITY_BYTES
            or identity_before != identity_after
            or not _parent_directory_is_current(parent)
        ):
            return None
        return raw
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(parent.descriptor)
        except OSError:
            pass


def _unique_json_object(raw: bytes) -> dict[str, object] | None:
    duplicate = False
    invalid_constant = False

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                duplicate = True
            output[key] = value
        return output

    def reject_constant(_: str) -> None:
        nonlocal invalid_constant
        invalid_constant = True

    try:
        payload = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (RecursionError, UnicodeError, ValueError):
        return None
    if duplicate or invalid_constant or type(payload) is not dict:
        return None
    return payload


def _string(payload: dict[str, object], key: str) -> str:
    value = payload[key]
    if type(value) is not str:
        raise ValueError
    return value


def _integer(payload: dict[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int:
        raise ValueError
    return value


def _hash(payload: dict[str, object], key: str) -> str:
    value = _string(payload, key)
    if _HASH_RE.fullmatch(value) is None:
        raise ValueError
    return value


def _reference_from_payload(payload: object) -> ReviewReference:
    if type(payload) is not dict or set(payload) != {"case_id", "content_sha256"}:
        raise ValueError
    return ReviewReference(
        case_id=_string(payload, "case_id"),
        content_sha256=_hash(payload, "content_sha256"),
    )


def _references_from_payload(
    payload: object, *, require_case_order: bool = True
) -> tuple[ReviewReference, ...]:
    if type(payload) is not list:
        raise ValueError
    references = tuple(_reference_from_payload(item) for item in payload)
    if len({item.case_id for item in references}) != len(references) or (
        require_case_order
        and tuple(sorted(references, key=lambda item: item.case_id)) != references
    ):
        raise ValueError
    return references


def _excluded_from_payload(payload: object) -> tuple[tuple[str, str], ...]:
    if type(payload) is not list:
        raise ValueError
    excluded: list[tuple[str, str]] = []
    for item in payload:
        if type(item) is not dict or set(item) != {"case_id", "reason_code"}:
            raise ValueError
        case_id = _string(item, "case_id")
        reason = _string(item, "reason_code")
        if _IDENTIFIER_RE.fullmatch(case_id) is None or reason not in {
            "quarantined",
            "restricted",
            "public_credit",
            "terminal_review_state",
        }:
            raise ValueError
        excluded.append((case_id, reason))
    result = tuple(excluded)
    if result != tuple(sorted(result)) or len({item[0] for item in result}) != len(
        result
    ):
        raise ValueError
    return result


def _segment_from_payload(payload: object, manifest_sha256: str) -> SamplingSegment:
    if type(payload) is not dict:
        raise ValueError
    common = {
        "doc_id",
        "edition_year",
        "segment_id",
        "segment_start_pdf_page",
        "segment_end_pdf_page",
        "source_sha256",
        "members",
        "excluded",
    }
    native_keys = common | {"manifest_sha256", "sampling_policy", "policy_version"}
    ocr_keys = common | {
        "detector_version",
        "layout_provenance",
        "registry_policy_version",
        "registry_sha256",
        "sampling_status",
        "segment_key",
    }
    members = _references_from_payload(payload.get("members"))
    excluded = _excluded_from_payload(payload.get("excluded"))
    if {item.case_id for item in members} & {item[0] for item in excluded}:
        raise ValueError
    if set(payload) == native_keys:
        native_manifest_sha256 = _hash(payload, "manifest_sha256")
        if native_manifest_sha256 != manifest_sha256:
            raise ValueError
        specification = NativeReviewLayoutSegment.model_validate(
            {
                "segment_id": _string(payload, "segment_id"),
                "start_pdf_page": _integer(payload, "segment_start_pdf_page"),
                "end_pdf_page": _integer(payload, "segment_end_pdf_page"),
                "sampling_policy": _string(payload, "sampling_policy"),
                "policy_version": _string(payload, "policy_version"),
            }
        )
        layout: SegmentLayoutAuthority | NativeSegmentLayoutAuthority = (
            NativeSegmentLayoutAuthority(
                segment_id=specification.segment_id,
                doc_id=_string(payload, "doc_id"),
                edition_year=_integer(payload, "edition_year"),
                source_sha256=_hash(payload, "source_sha256"),
                manifest_sha256=native_manifest_sha256,
                segment_start_pdf_page=specification.start_pdf_page,
                segment_end_pdf_page=specification.end_pdf_page,
                sampling_policy=specification.sampling_policy,
                policy_version=specification.policy_version,
            )
        )
    elif set(payload) == ocr_keys:
        pages = payload["layout_provenance"]
        if type(pages) is not list or not pages:
            raise ValueError
        provenances: list[LayoutSegmentProvenance] = []
        for page in pages:
            if type(page) is not dict or set(page) != {
                "pdf_page_index",
                "region_count",
                "render_sha256",
            }:
                raise ValueError
            provenances.append(
                LayoutSegmentProvenance.model_validate(
                    {
                        "segment_id": _string(payload, "segment_id"),
                        "segment_key": _string(payload, "segment_key"),
                        "segment_start_pdf_page": _integer(
                            payload, "segment_start_pdf_page"
                        ),
                        "segment_end_pdf_page": _integer(
                            payload, "segment_end_pdf_page"
                        ),
                        "registry_policy_version": _string(
                            payload, "registry_policy_version"
                        ),
                        "registry_sha256": _hash(payload, "registry_sha256"),
                        "detector_version": _string(payload, "detector_version"),
                        "region_count": _integer(page, "region_count"),
                        "sampling_status": _string(payload, "sampling_status"),
                        "doc_id": _string(payload, "doc_id"),
                        "edition_year": _integer(payload, "edition_year"),
                        "source_sha256": _hash(payload, "source_sha256"),
                        "pdf_page_index": _integer(page, "pdf_page_index"),
                        "render_sha256": _hash(page, "render_sha256"),
                    }
                )
            )
        layout = _layout_authority(tuple(provenances))
        if layout.to_payload() != {
            key: value
            for key, value in payload.items()
            if key not in {"members", "excluded"}
        }:
            raise ValueError
    else:
        raise ValueError
    return SamplingSegment(layout=layout, members=members, excluded=excluded)


def _inventory_from_payload(payload: object) -> SamplingInventory:
    keys = {
        "all_fields_case_ids",
        "blockers",
        "manifest_sha256",
        "parser_authority_sha256",
        "raw_authority_sha256",
        "registry_sha256",
        "release_id",
        "schema_version",
        "segments",
    }
    if (
        type(payload) is not dict
        or set(payload) != keys
        or _string(payload, "schema_version") != "sen-qa-review-segment-inventory/v1"
    ):
        raise ValueError
    release_id = _string(payload, "release_id")
    if _RELEASE_RE.fullmatch(release_id) is None:
        raise ValueError
    manifest_sha256 = _hash(payload, "manifest_sha256")
    raw_segments = payload["segments"]
    raw_all_fields = payload["all_fields_case_ids"]
    raw_blockers = payload["blockers"]
    if (
        type(raw_segments) is not list
        or type(raw_all_fields) is not list
        or type(raw_blockers) is not list
    ):
        raise ValueError
    segments = tuple(
        _segment_from_payload(item, manifest_sha256) for item in raw_segments
    )
    all_fields = tuple(raw_all_fields)
    blockers = tuple(raw_blockers)
    if (
        any(
            type(item) is not str or _IDENTIFIER_RE.fullmatch(item) is None
            for item in all_fields
        )
        or all_fields != tuple(sorted(set(all_fields)))
        or any(type(item) is not str or item != _NATIVE_SEAM for item in blockers)
        or blockers != tuple(sorted(set(blockers)))
        or len({item.layout.segment_id for item in segments}) != len(segments)
    ):
        raise ValueError
    member_ids = [member.case_id for segment in segments for member in segment.members]
    excluded_ids = [case_id for segment in segments for case_id, _ in segment.excluded]
    if (
        len(set(member_ids)) != len(member_ids)
        or len(set(excluded_ids)) != len(excluded_ids)
        or set(member_ids) & set(excluded_ids)
        or set(all_fields) & (set(member_ids) | set(excluded_ids))
    ):
        raise ValueError
    return SamplingInventory(
        release_id=release_id,
        registry_sha256=_hash(payload, "registry_sha256"),
        parser_authority_sha256=_hash(payload, "parser_authority_sha256"),
        raw_authority_sha256=_hash(payload, "raw_authority_sha256"),
        manifest_sha256=manifest_sha256,
        segments=segments,
        all_fields_case_ids=all_fields,
        blockers=blockers,
    )


def _plan_from_payload(
    payload: object, inventory: SamplingInventory
) -> SegmentSamplePlan:
    keys = {
        "inventory_sha256",
        "members",
        "minimum_per_segment",
        "manifest_sha256",
        "parser_authority_sha256",
        "plan_sha256",
        "raw_authority_sha256",
        "registry_sha256",
        "release_id",
        "sample_rate",
        "schema_version",
        "segment_id",
        "selected",
    }
    if (
        type(payload) is not dict
        or set(payload) != keys
        or _string(payload, "schema_version") != "sen-qa-review-sample-plan/v1"
    ):
        raise ValueError
    sample_rate = payload["sample_rate"]
    if (
        type(sample_rate) is not float
        or sample_rate != 0.10
        or _integer(payload, "minimum_per_segment") != 5
    ):
        raise ValueError
    segment_id = _string(payload, "segment_id")
    matches = tuple(
        item for item in inventory.segments if item.layout.segment_id == segment_id
    )
    if len(matches) != 1:
        raise ValueError
    members = _references_from_payload(payload["members"])
    selected = _references_from_payload(payload["selected"], require_case_order=False)
    if (
        members != matches[0].members
        or _string(payload, "release_id") != inventory.release_id
        or _hash(payload, "inventory_sha256") != inventory.sha256
        or _hash(payload, "registry_sha256") != inventory.registry_sha256
        or _hash(payload, "parser_authority_sha256")
        != inventory.parser_authority_sha256
        or _hash(payload, "raw_authority_sha256") != inventory.raw_authority_sha256
        or _hash(payload, "manifest_sha256") != inventory.manifest_sha256
    ):
        raise ValueError
    sample_size = min(len(members), max(5, math.ceil(len(members) * 0.10)))
    expected_selected = tuple(
        sorted(
            members,
            key=lambda reference: (
                _selection_score(
                    inventory=inventory, segment_id=segment_id, reference=reference
                ),
                reference.case_id,
            ),
        )[:sample_size]
    )
    if selected != expected_selected:
        raise ValueError
    plan = SegmentSamplePlan(
        release_id=inventory.release_id,
        inventory_sha256=inventory.sha256,
        registry_sha256=inventory.registry_sha256,
        parser_authority_sha256=inventory.parser_authority_sha256,
        raw_authority_sha256=inventory.raw_authority_sha256,
        manifest_sha256=inventory.manifest_sha256,
        segment_id=segment_id,
        sample_rate=sample_rate,
        minimum_per_segment=5,
        members=members,
        selected=selected,
    )
    if _hash(payload, "plan_sha256") != plan.sha256:
        raise ValueError
    return plan


def _sampling_authority_from_payload(payload: dict[str, object]) -> SamplingAuthority:
    if (
        set(payload) != {"inventory", "inventory_sha256", "plans", "schema_version"}
        or _string(payload, "schema_version") != "sen-qa-review-sampling-authority/v1"
    ):
        raise ValueError
    inventory = _inventory_from_payload(payload["inventory"])
    if (
        _hash(payload, "inventory_sha256") != inventory.sha256
        or type(payload["plans"]) is not list
    ):
        raise ValueError
    plans = tuple(_plan_from_payload(item, inventory) for item in payload["plans"])
    expected_segments = tuple(
        item.layout.segment_id for item in inventory.segments if item.members
    )
    if plans != tuple(sorted(plans, key=lambda item: item.segment_id)) or tuple(
        plan.segment_id for plan in plans
    ) != tuple(sorted(expected_segments)):
        raise ValueError
    return SamplingAuthority(inventory=inventory, plans=plans)


def load_sampling_authority(
    path: Path, *, expected_sha256: str
) -> VerifiedSamplingAuthority:
    """Load canonical owner-only sampling bytes under an external SHA-256 pin."""
    authority: VerifiedSamplingAuthority | None = None
    try:
        if (
            type(path) is not type(Path())
            or type(expected_sha256) is not str
            or _HASH_RE.fullmatch(expected_sha256) is None
        ):
            raise ValueError
        raw = _read_private_authority(path)
        if raw is None or not hmac.compare_digest(_digest(raw), expected_sha256):
            raise ValueError
        payload = _unique_json_object(raw)
        if payload is None:
            raise ValueError
        candidate = _sampling_authority_from_payload(payload)
        if candidate.to_bytes() != raw:
            raise ValueError
        authority = _verified_sampling_authority(candidate, raw, expected_sha256)
    except (
        KeyError,
        OSError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        authority = None
    if authority is None:
        _fail("sampling_authority_invalid")
    return authority


class SamplingReviewLedger:
    """Append-only broker state for one staged sample plan."""

    def __init__(
        self, *, authority: VerifiedSamplingAuthority, plan_sha256: str
    ) -> None:
        if (
            type(authority) is not VerifiedSamplingAuthority
            or type(plan_sha256) is not str
            or _HASH_RE.fullmatch(plan_sha256) is None
        ):
            _fail("sampling_input_invalid")
        sealed: object | None = None
        canonical_bytes: object | None = None
        external_sha256: object | None = None
        recursively_checked: SamplingAuthority | None = None
        try:
            sealed = authority._authority
            canonical_bytes = authority._canonical_bytes
            external_sha256 = authority.external_sha256
            if type(canonical_bytes) is bytes:
                payload = _unique_json_object(canonical_bytes)
                recursively_checked = (
                    _sampling_authority_from_payload(payload)
                    if payload is not None
                    else None
                )
        except (
            AttributeError,
            KeyError,
            OSError,
            OverflowError,
            RecursionError,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            pass
        if (
            type(sealed) is not SamplingAuthority
            or type(canonical_bytes) is not bytes
            or type(external_sha256) is not str
            or _HASH_RE.fullmatch(external_sha256) is None
            or recursively_checked is None
            or recursively_checked.to_bytes() != canonical_bytes
            or sealed.to_bytes() != canonical_bytes
            or not hmac.compare_digest(_digest(canonical_bytes), external_sha256)
        ):
            _fail("sampling_input_invalid")
        matches = tuple(
            plan for plan in recursively_checked.plans if plan.sha256 == plan_sha256
        )
        if len(matches) != 1:
            _fail("sample_plan_not_found")
        self.authority = recursively_checked
        self.plan = matches[0]
        self._events: list[SampleReviewEvent] = []

    @property
    def events(self) -> tuple[SampleReviewEvent, ...]:
        return tuple(self._events)

    @property
    def events_sha256(self) -> str:
        return _digest(
            _canonical(
                {
                    "events": [event.to_payload() for event in self._events],
                    "plan_sha256": self.plan.sha256,
                    "schema_version": "sen-qa-review-sample-event-set/v1",
                }
            )
        )

    def record(
        self,
        *,
        case_id: str,
        reviewed_content_sha256: str,
        reviewer_id: str,
        outcome: SamplingOutcome,
        reason_code: str,
    ) -> SampleReviewEvent:
        selected = {item.case_id: item for item in self.plan.selected}
        reference = selected.get(case_id)
        if (
            reference is None
            or reference.content_sha256 != reviewed_content_sha256
            or _IDENTIFIER_RE.fullmatch(reviewer_id) is None
            or outcome not in {"passed", "error"}
            or _IDENTIFIER_RE.fullmatch(reason_code) is None
        ):
            _fail("sample_event_invalid")
        if any(event.case_id == case_id for event in self._events):
            _fail("sample_event_replay")
        event = SampleReviewEvent(
            sequence=len(self._events) + 1,
            plan_sha256=self.plan.sha256,
            segment_id=self.plan.segment_id,
            case_id=case_id,
            reviewed_content_sha256=reviewed_content_sha256,
            reviewer_id=reviewer_id,
            outcome=outcome,
            reason_code=reason_code,
        )
        self._events.append(event)
        return event

    def search_approval_manifest(self) -> SampleSearchApprovalManifest:
        if any(event.outcome == "error" for event in self._events):
            _fail("segment_escalated")
        if {event.case_id for event in self._events} != {
            item.case_id for item in self.plan.selected
        }:
            _fail("sample_events_incomplete")
        return SampleSearchApprovalManifest(
            release_id=self.plan.release_id,
            registry_sha256=self.plan.registry_sha256,
            inventory_sha256=self.plan.inventory_sha256,
            plan_sha256=self.plan.sha256,
            sample_events_sha256=self.events_sha256,
            segment_id=self.plan.segment_id,
            members=self.plan.members,
        )

    def critical_fields_escalation(self) -> CriticalFieldsEscalation:
        if not any(event.outcome == "error" for event in self._events):
            _fail("segment_not_escalated")
        return CriticalFieldsEscalation(
            release_id=self.plan.release_id,
            plan_sha256=self.plan.sha256,
            segment_id=self.plan.segment_id,
            members=self.plan.members,
        )


def write_sampling_authority(path: Path, authority: SamplingAuthority) -> None:
    """Write one canonical owner-only authority through a held parent FD."""
    raw: bytes | None = None
    try:
        if type(path) is not type(Path()) or type(authority) is not SamplingAuthority:
            raise ValueError
        candidate = authority.to_bytes()
        if not candidate or len(candidate) > _MAX_AUTHORITY_BYTES:
            raise ValueError
        raw = candidate
    except (
        OSError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        pass
    if raw is None:
        _fail("sampling_write_invalid")
    parent = _open_parent_directory(path)
    if parent is None:
        _fail("sampling_write_invalid")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor: int | None = None
    created = False
    valid = False
    try:
        descriptor = os.open(
            parent.leaf_name,
            flags,
            0o600,
            dir_fd=parent.descriptor,
        )
        created = True
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError
            offset += written
        os.fsync(descriptor)
        if not _parent_directory_is_current(parent):
            raise OSError
        os.fsync(parent.descriptor)
        if not _parent_directory_is_current(parent):
            raise OSError
        valid = True
    except (OSError, OverflowError, TypeError, ValueError):
        pass
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                valid = False
        if created and not valid:
            try:
                os.unlink(parent.leaf_name, dir_fd=parent.descriptor)
                os.fsync(parent.descriptor)
            except OSError:
                pass
        try:
            os.close(parent.descriptor)
        except OSError:
            pass
    if not valid:
        _fail("sampling_write_invalid")
