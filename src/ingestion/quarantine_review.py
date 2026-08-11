"""Value-free human authority for resolving parser quarantine occurrences."""

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
from typing import Literal, NoReturn, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.ingestion.parse_common import (
    ParseResult,
    ParserPage,
    VerifiedParserAnnotation,
    parse_pages,
    parse_pages_with_verified_annotations,
    verify_parser_annotations,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_ID_RE = re.compile(r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_LOCATION_ID_RE = re.compile(r"^loc-[0-9a-f]{32}$")
_OCCURRENCE_ID_RE = re.compile(r"^qocc-[0-9a-f]{32}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_ACTOR_ID_RE = re.compile(r"^uid:[1-9][0-9]{0,9}:[A-Za-z0-9_.-]{1,80}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_MAX_BYTES = 16 * 1024 * 1024
_MAX_QUARANTINES = 10_000
_OCCURRENCE_PREFIX = b"sen-qa-parser-quarantine-occurrence-v1\0"
_EVENT_PREFIX = b"sen-qa-parser-quarantine-resolution-event-v1\0"

_DirectoryIdentity = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class _ParentDirectoryWalk:
    descriptor: int
    absolute_path: str
    leaf_name: str
    identities: tuple[_DirectoryIdentity, ...]


QuarantineReason = Literal[
    "ambiguous_boundary",
    "page-extraction-failed",
    "page-render-failed",
    "ocr-adapter-failed",
    "ocr-provenance-invalid",
]
ResolutionDisposition = Literal["unresolved", "confirmed_noncase", "corrected"]


class QuarantineResolutionError(ValueError):
    """A cause-free and value-free quarantine resolution failure."""


def _raise(code: str) -> NoReturn:
    raise QuarantineResolutionError(code) from None


class _DuplicateJsonKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise _DuplicateJsonKey
        output[key] = value
    return output


def _load_json(raw: bytes) -> object:
    return json.loads(
        raw.decode("ascii"),
        object_pairs_hook=_unique_json_object,
    )


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class ResolutionSourceSpan(_StrictModel):
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    bbox: tuple[float, float, float, float]
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def has_positive_bbox(self) -> Self:
        x0, y0, x1, y1 = self.bbox
        if x0 >= x1 or y0 >= y1:
            raise ValueError("invalid span geometry")
        return self


class ResolutionAnnotation(_StrictModel):
    """A future role annotation; drafts contain no annotations."""

    role: Literal[
        "domain",
        "part",
        "subtopic",
        "case_start",
        "case_no",
        "title",
        "question",
        "answer",
        "basis",
        "facts",
        "target",
        "situation",
        "case_end",
    ]
    source_span: ResolutionSourceSpan


class QuarantineResolution(_StrictModel):
    occurrence_id: str = Field(pattern=r"^qocc-[0-9a-f]{32}$")
    occurrence_ordinal: int = Field(ge=1)
    source_row_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    doc_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,160}$")
    edition_year: int = Field(ge=2020, le=2025)
    location_id: str = Field(pattern=r"^loc-[0-9a-f]{32}$")
    reason_code: QuarantineReason
    page_ids: tuple[int, ...] = Field(min_length=1)
    source_spans: tuple[ResolutionSourceSpan, ...]
    span_count: int = Field(ge=0)
    occurrence_count: int | None = Field(default=None, ge=1)
    disposition: ResolutionDisposition = "unresolved"
    annotations: tuple[ResolutionAnnotation, ...] = ()

    @model_validator(mode="after")
    def matches_reason_and_spans(self) -> Self:
        if self.page_ids != tuple(sorted(set(self.page_ids))):
            raise ValueError("invalid page set")
        if self.span_count != len(self.source_spans):
            raise ValueError("invalid span count")
        if self.reason_code == "ambiguous_boundary":
            if (
                not self.source_spans
                or self.occurrence_count is not None
                or tuple(sorted({span.pdf_page_index for span in self.source_spans}))
                != self.page_ids
            ):
                raise ValueError("invalid boundary quarantine")
        elif (
            self.source_spans
            or self.span_count != 0
            or len(self.page_ids) != 1
            or self.occurrence_count != 1
        ):
            raise ValueError("invalid upstream quarantine")
        if self.disposition == "unresolved" and self.annotations:
            raise ValueError("unresolved entry cannot be annotated")
        return self


class ResolutionEvent(_StrictModel):
    """Broker-compatible event envelope; event behavior is added separately."""

    event_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")
    occurrence_id: str = Field(pattern=r"^qocc-[0-9a-f]{32}$")
    actor_id: str = Field(pattern=r"^uid:[1-9][0-9]{0,9}:[A-Za-z0-9_.-]{1,80}$")
    occurred_at: str = Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
    )
    disposition: ResolutionDisposition
    annotations: tuple[ResolutionAnnotation, ...]
    reviewed_occurrence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_event_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _QuarantineResolutionAuthority(_StrictModel):
    schema_version: Literal["sen-qa-parser-quarantine-resolution/v1"]
    release_id: str = Field(pattern=r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_authority_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_authority_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_quarantines_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    quarantine_count: int = Field(ge=1, le=_MAX_QUARANTINES)
    resolutions: tuple[QuarantineResolution, ...]
    events: tuple[ResolutionEvent, ...] = ()

    @model_validator(mode="after")
    def has_exact_occurrence_set(self) -> Self:
        if len(self.resolutions) != self.quarantine_count:
            raise ValueError("incomplete resolution coverage")
        ids = tuple(item.occurrence_id for item in self.resolutions)
        if len(set(ids)) != len(ids) or ids != tuple(sorted(ids)):
            raise ValueError("invalid occurrence ordering")
        ordinals: dict[str, set[int]] = {}
        for resolution in self.resolutions:
            row_raw = _original_quarantine_row_bytes(resolution)
            row_sha256 = hashlib.sha256(row_raw).hexdigest()
            expected_id = (
                "qocc-"
                + hashlib.sha256(
                    _OCCURRENCE_PREFIX
                    + bytes.fromhex(row_sha256)
                    + resolution.occurrence_ordinal.to_bytes(8, "big")
                ).hexdigest()[:32]
            )
            if (
                resolution.source_row_sha256 != row_sha256
                or resolution.occurrence_id != expected_id
            ):
                raise ValueError("invalid occurrence provenance")
            ordinals.setdefault(row_sha256, set()).add(resolution.occurrence_ordinal)
        if any(
            values != set(range(1, len(values) + 1)) for values in ordinals.values()
        ):
            raise ValueError("invalid occurrence ordinals")
        by_id = {item.occurrence_id: item for item in self.resolutions}
        seen: set[str] = set()
        previous: str | None = None
        for event in self.events:
            event_resolution = by_id.get(event.occurrence_id)
            if event_resolution is None or event.occurrence_id in seen:
                raise ValueError("invalid resolution event coverage")
            if event.previous_event_sha256 != previous:
                raise ValueError("invalid resolution event chain")
            if event.reviewed_occurrence_sha256 != _occurrence_sha256(event_resolution):
                raise ValueError("invalid reviewed occurrence binding")
            if event.disposition == "unresolved":
                raise ValueError("invalid unresolved event")
            if (
                event_resolution.disposition != event.disposition
                or event_resolution.annotations != event.annotations
                or event.event_sha256 != _event_sha256(event)
            ):
                raise ValueError("invalid resolution event state")
            seen.add(event.occurrence_id)
            previous = event.event_sha256
        for resolution in self.resolutions:
            if (resolution.disposition == "unresolved") != (
                resolution.occurrence_id not in seen
            ):
                raise ValueError("resolution state lacks one event")
        return self

    def to_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))


@dataclass(frozen=True, slots=True, init=False)
class VerifiedQuarantineResolutionAuthority:
    """Init-disabled sidecar authority carrying an external file seal."""

    _authority: _QuarantineResolutionAuthority
    external_sha256: str

    def __new__(cls) -> Self:
        raise TypeError("resolution authority requires external verification")

    def __getattr__(self, name: str) -> object:
        if name in _QuarantineResolutionAuthority.model_fields:
            return getattr(self._authority, name)
        raise AttributeError(name)

    @property
    def resolutions(self) -> tuple[QuarantineResolution, ...]:
        return self._authority.resolutions

    @property
    def events(self) -> tuple[ResolutionEvent, ...]:
        return self._authority.events

    @property
    def quarantine_count(self) -> int:
        return self._authority.quarantine_count

    @property
    def registry_sha256(self) -> str:
        return self._authority.registry_sha256

    @property
    def manifest_sha256(self) -> str:
        return self._authority.manifest_sha256

    @property
    def raw_authority_sha256(self) -> str:
        return self._authority.raw_authority_sha256

    @property
    def parser_authority_sha256(self) -> str:
        return self._authority.parser_authority_sha256

    @property
    def parser_quarantines_sha256(self) -> str:
        return self._authority.parser_quarantines_sha256

    def to_bytes(self) -> bytes:
        return self._authority.to_bytes()


def _verified_authority(
    authority: _QuarantineResolutionAuthority, external_sha256: str
) -> VerifiedQuarantineResolutionAuthority:
    wrapper = object.__new__(VerifiedQuarantineResolutionAuthority)
    object.__setattr__(wrapper, "_authority", authority)
    object.__setattr__(wrapper, "external_sha256", external_sha256)
    return wrapper


def _occurrence_sha256(resolution: QuarantineResolution) -> str:
    payload = resolution.model_dump(mode="json", exclude={"disposition", "annotations"})
    return hashlib.sha256(
        b"sen-qa-parser-quarantine-reviewed-occurrence-v1\0"
        + _canonical_bytes(payload).rstrip(b"\n")
    ).hexdigest()


def _original_quarantine_row_bytes(resolution: QuarantineResolution) -> bytes:
    payload: dict[str, object] = {
        "doc_id": resolution.doc_id,
        "edition_year": resolution.edition_year,
        "location_id": resolution.location_id,
        "page_ids": list(resolution.page_ids),
        "reason_code": resolution.reason_code,
        "source_spans": [
            span.model_dump(mode="json") for span in resolution.source_spans
        ],
        "span_count": resolution.span_count,
    }
    if resolution.occurrence_count is not None:
        payload["occurrence_count"] = resolution.occurrence_count
    return _canonical_bytes(payload)


def _event_payload(event: ResolutionEvent) -> dict[str, object]:
    return event.model_dump(mode="json", exclude={"event_sha256"})


def _event_sha256(event: ResolutionEvent) -> str:
    return hashlib.sha256(
        _EVENT_PREFIX + _canonical_bytes(_event_payload(event)).rstrip(b"\n")
    ).hexdigest()


def _parse_quarantine_rows(raw: bytes) -> tuple[dict[str, object], ...]:
    if not raw or len(raw) > _MAX_BYTES or not raw.endswith(b"\n"):
        raise ValueError
    lines = raw.splitlines(keepends=True)
    if not lines or len(lines) > _MAX_QUARANTINES or b"".join(sorted(lines)) != raw:
        raise ValueError
    output: list[dict[str, object]] = []
    for line in lines:
        value = _load_json(line)
        if type(value) is not dict or _canonical_bytes(value) != line:
            raise ValueError
        row = value
        reason = row.get("reason_code")
        boundary_fields = {
            "doc_id",
            "edition_year",
            "location_id",
            "page_ids",
            "reason_code",
            "source_spans",
            "span_count",
        }
        upstream_fields = boundary_fields | {"occurrence_count"}
        if set(row) != (
            boundary_fields if reason == "ambiguous_boundary" else upstream_fields
        ):
            raise ValueError
        output.append(row)
    return tuple(output)


def _resolution_from_row(
    row: dict[str, object],
    *,
    line: bytes,
    ordinal: int,
) -> QuarantineResolution:
    row_sha256 = hashlib.sha256(line).hexdigest()
    occurrence_id = (
        "qocc-"
        + hashlib.sha256(
            _OCCURRENCE_PREFIX + bytes.fromhex(row_sha256) + ordinal.to_bytes(8, "big")
        ).hexdigest()[:32]
    )
    raw_page_ids = row["page_ids"]
    raw_spans = row["source_spans"]
    if type(raw_page_ids) is not list or type(raw_spans) is not list:
        raise ValueError
    spans: list[ResolutionSourceSpan] = []
    for raw_span in raw_spans:
        if type(raw_span) is not dict or type(raw_span.get("bbox")) is not list:
            raise ValueError
        spans.append(
            ResolutionSourceSpan.model_validate(
                {**raw_span, "bbox": tuple(raw_span["bbox"])}
            )
        )
    return QuarantineResolution.model_validate(
        {
            "occurrence_id": occurrence_id,
            "occurrence_ordinal": ordinal,
            "source_row_sha256": row_sha256,
            "doc_id": row["doc_id"],
            "edition_year": row["edition_year"],
            "location_id": row["location_id"],
            "reason_code": row["reason_code"],
            "page_ids": tuple(raw_page_ids),
            "source_spans": tuple(spans),
            "span_count": row["span_count"],
            "occurrence_count": row.get("occurrence_count"),
            "disposition": "unresolved",
            "annotations": (),
        }
    )


def create_resolution_draft(
    *,
    release_id: str,
    registry_sha256: str,
    manifest_sha256: str,
    raw_authority_sha256: str,
    parser_authority_sha256: str,
    parser_quarantines_bytes: bytes,
    parser_quarantines_sha256: str,
) -> bytes:
    """Create canonical unresolved bytes; this does not verify an external seal."""
    try:
        if (
            _RELEASE_ID_RE.fullmatch(release_id) is None
            or any(
                _SHA256_RE.fullmatch(value) is None
                for value in (
                    registry_sha256,
                    manifest_sha256,
                    raw_authority_sha256,
                    parser_authority_sha256,
                    parser_quarantines_sha256,
                )
            )
            or not hmac.compare_digest(
                hashlib.sha256(parser_quarantines_bytes).hexdigest(),
                parser_quarantines_sha256,
            )
        ):
            raise ValueError
        rows = _parse_quarantine_rows(parser_quarantines_bytes)
        counts: Counter[str] = Counter()
        resolutions: list[QuarantineResolution] = []
        for row, line in zip(
            rows,
            parser_quarantines_bytes.splitlines(keepends=True),
            strict=True,
        ):
            row_sha256 = hashlib.sha256(line).hexdigest()
            counts[row_sha256] += 1
            resolutions.append(
                _resolution_from_row(
                    row,
                    line=line,
                    ordinal=counts[row_sha256],
                )
            )
        authority = _QuarantineResolutionAuthority(
            schema_version="sen-qa-parser-quarantine-resolution/v1",
            release_id=release_id,
            registry_sha256=registry_sha256,
            manifest_sha256=manifest_sha256,
            raw_authority_sha256=raw_authority_sha256,
            parser_authority_sha256=parser_authority_sha256,
            parser_quarantines_sha256=parser_quarantines_sha256,
            quarantine_count=len(resolutions),
            resolutions=tuple(sorted(resolutions, key=lambda item: item.occurrence_id)),
            events=(),
        )
        return authority.to_bytes()
    except (KeyError, OverflowError, TypeError, ValueError, UnicodeError):
        _raise("resolution_draft_invalid")


def append_resolution_event(
    authority: VerifiedQuarantineResolutionAuthority,
    *,
    occurrence_id: str,
    disposition: Literal["confirmed_noncase", "corrected"],
    annotations: tuple[ResolutionAnnotation, ...],
    actor_id: str,
    event_id: str,
    occurred_at: str,
) -> bytes:
    """Append one broker-shaped terminal decision without accepting source text."""
    try:
        if (
            type(authority) is not VerifiedQuarantineResolutionAuthority
            or _OCCURRENCE_ID_RE.fullmatch(occurrence_id) is None
            or _ACTOR_ID_RE.fullmatch(actor_id) is None
            or _EVENT_ID_RE.fullmatch(event_id) is None
            or _UTC_RE.fullmatch(occurred_at) is None
            or type(annotations) is not tuple
        ):
            raise ValueError
        sealed = authority._authority
        if not hmac.compare_digest(
            hashlib.sha256(sealed.to_bytes()).hexdigest(), authority.external_sha256
        ):
            raise ValueError
        matches = tuple(
            item for item in sealed.resolutions if item.occurrence_id == occurrence_id
        )
        if len(matches) != 1:
            raise ValueError
        current = matches[0]
        if (
            current.disposition != "unresolved"
            or current.reason_code != "ambiguous_boundary"
        ):
            raise ValueError
        if disposition == "confirmed_noncase":
            if annotations:
                raise ValueError
        elif disposition == "corrected":
            if not annotations or len(annotations) != len(current.source_spans):
                raise ValueError
            allowed_spans = set(current.source_spans)
            if any(
                type(annotation) is not ResolutionAnnotation
                or annotation.source_span not in allowed_spans
                for annotation in annotations
            ):
                raise ValueError
            keys = tuple((item.role, item.source_span) for item in annotations)
            if (
                len(set(keys)) != len(keys)
                or {item.source_span for item in annotations} != allowed_spans
            ):
                raise ValueError
        else:
            raise ValueError
        updated = current.model_copy(
            update={"disposition": disposition, "annotations": annotations}
        )
        previous = sealed.events[-1].event_sha256 if sealed.events else None
        provisional = ResolutionEvent(
            event_id=event_id,
            occurrence_id=occurrence_id,
            actor_id=actor_id,
            occurred_at=occurred_at,
            disposition=disposition,
            annotations=annotations,
            reviewed_occurrence_sha256=_occurrence_sha256(current),
            previous_event_sha256=previous,
            event_sha256="0" * 64,
        )
        event = provisional.model_copy(
            update={"event_sha256": _event_sha256(provisional)}
        )
        resolutions = tuple(
            updated if item.occurrence_id == occurrence_id else item
            for item in sealed.resolutions
        )
        resolved = _QuarantineResolutionAuthority(
            **sealed.model_dump(
                exclude={"resolutions", "events"},
            ),
            resolutions=resolutions,
            events=sealed.events + (event,),
        )
        return resolved.to_bytes()
    except (KeyError, OverflowError, TypeError, ValueError, UnicodeError):
        _raise("resolution_event_invalid")


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


def _read_private(path: Path) -> bytes | None:
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
            or before.st_size > _MAX_BYTES
        ):
            return None
        chunks: list[bytes] = []
        remaining = _MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(raw) > _MAX_BYTES
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                stat.S_IMODE(before.st_mode),
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                stat.S_IMODE(after.st_mode),
            )
            or not _parent_directory_is_current(parent)
        ):
            return None
        return raw
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent.descriptor)


def load_resolution_authority(
    path: Path,
    *,
    expected_sha256: str,
) -> VerifiedQuarantineResolutionAuthority:
    """Load canonical sidecar bytes under an externally supplied SHA-256."""
    authority: VerifiedQuarantineResolutionAuthority | None = None
    try:
        if (
            not isinstance(path, Path)
            or type(expected_sha256) is not str
            or _SHA256_RE.fullmatch(expected_sha256) is None
        ):
            raise ValueError
        raw = _read_private(path)
        if raw is None or not hmac.compare_digest(
            hashlib.sha256(raw).hexdigest(), expected_sha256
        ):
            raise ValueError
        _load_json(raw)
        candidate = _QuarantineResolutionAuthority.model_validate_json(raw)
        if candidate.to_bytes() != raw:
            raise ValueError
        authority = _verified_authority(candidate, expected_sha256)
    except (
        KeyError,
        OSError,
        OverflowError,
        TypeError,
        ValueError,
        UnicodeError,
    ):
        authority = None
    if authority is None:
        _raise("resolution_authority_invalid")
    return authority


def _revalidated_page_with_lines(
    page: ParserPage, lines: tuple[object, ...]
) -> ParserPage:
    fields = {name: getattr(page, name) for name in ParserPage.model_fields}
    fields["lines"] = lines
    return ParserPage.model_validate(fields)


def reparse_with_resolution(
    page_runs: tuple[tuple[ParserPage, ...], ...],
    *,
    authority: VerifiedQuarantineResolutionAuthority,
    expected_registry_sha256: str,
    expected_manifest_sha256: str,
    expected_raw_authority_sha256: str,
    expected_parser_authority_sha256: str,
    parser_quarantines_bytes: bytes,
    expected_parser_quarantines_sha256: str,
) -> tuple[ParseResult, ...]:
    """Apply one fully resolved sidecar through closed annual parser dispatch."""
    try:
        if (
            type(page_runs) is not tuple
            or not page_runs
            or type(authority) is not VerifiedQuarantineResolutionAuthority
            or not hmac.compare_digest(
                hashlib.sha256(authority.to_bytes()).hexdigest(),
                authority.external_sha256,
            )
            or any(
                _SHA256_RE.fullmatch(value) is None
                for value in (
                    expected_registry_sha256,
                    expected_manifest_sha256,
                    expected_raw_authority_sha256,
                    expected_parser_authority_sha256,
                )
            )
            or not hmac.compare_digest(
                authority.registry_sha256, expected_registry_sha256
            )
            or not hmac.compare_digest(
                authority.manifest_sha256, expected_manifest_sha256
            )
            or not hmac.compare_digest(
                authority.raw_authority_sha256, expected_raw_authority_sha256
            )
            or not hmac.compare_digest(
                authority.parser_authority_sha256,
                expected_parser_authority_sha256,
            )
            or _SHA256_RE.fullmatch(expected_parser_quarantines_sha256) is None
            or not hmac.compare_digest(
                expected_parser_quarantines_sha256,
                authority.parser_quarantines_sha256,
            )
            or not hmac.compare_digest(
                hashlib.sha256(parser_quarantines_bytes).hexdigest(),
                expected_parser_quarantines_sha256,
            )
            or any(item.disposition == "unresolved" for item in authority.resolutions)
        ):
            raise ValueError
        expected_rows = b"".join(
            sorted(
                _original_quarantine_row_bytes(item) for item in authority.resolutions
            )
        )
        if expected_rows != parser_quarantines_bytes:
            raise ValueError

        ignored_by_document: dict[
            tuple[str, int],
            set[tuple[int, str | None, tuple[float, float, float, float], str]],
        ] = {}
        annotations_by_document: dict[
            tuple[str, int], list[VerifiedParserAnnotation]
        ] = {}
        for resolution in authority.resolutions:
            key = (resolution.doc_id, resolution.edition_year)
            if resolution.disposition == "confirmed_noncase":
                ignored_by_document.setdefault(key, set()).update(
                    (
                        span.pdf_page_index,
                        span.page_label,
                        span.bbox,
                        span.text_sha256,
                    )
                    for span in resolution.source_spans
                )
                continue
            if resolution.disposition != "corrected" or not resolution.annotations:
                raise ValueError
            if len(resolution.annotations) != len(resolution.source_spans) or {
                item.source_span for item in resolution.annotations
            } != set(resolution.source_spans):
                raise ValueError
            annotations_by_document.setdefault(key, []).extend(
                VerifiedParserAnnotation(
                    role=item.role,
                    pdf_page_index=item.source_span.pdf_page_index,
                    bbox=item.source_span.bbox,
                    text_sha256=item.source_span.text_sha256,
                )
                for item in resolution.annotations
            )

        results: list[ParseResult] = []
        seen_documents: set[tuple[str, int]] = set()
        for run in page_runs:
            if type(run) is not tuple or not run:
                raise ValueError
            doc_id = run[0].doc_id
            year = run[0].edition_year
            key = (doc_id, year)
            if key in seen_documents or year not in {
                2020,
                2021,
                2022,
                2023,
                2024,
                2025,
            }:
                raise ValueError
            seen_documents.add(key)
            ignored = ignored_by_document.get(key, set())
            projected: list[ParserPage] = []
            matched_ignored: set[
                tuple[int, str | None, tuple[float, float, float, float], str]
            ] = set()
            for page in run:
                retained = []
                for line in page.lines:
                    location = (
                        page.pdf_page_index,
                        page.page_label,
                        line.bbox,
                        line.raw_text_sha256,
                    )
                    if location in ignored:
                        matched_ignored.add(location)
                    else:
                        retained.append(line)
                projected.append(_revalidated_page_with_lines(page, tuple(retained)))
            if matched_ignored != ignored:
                raise ValueError
            checked_run = tuple(projected)
            raw_annotations = tuple(annotations_by_document.get(key, ()))
            if raw_annotations:
                verified = verify_parser_annotations(
                    checked_run,
                    annotations=raw_annotations,
                    expected_source_sha256=run[0].source_sha256 or "",
                )
                result = parse_pages_with_verified_annotations(
                    checked_run,
                    edition_year=year,
                    verified_annotations=verified,
                )
            else:
                result = parse_pages(checked_run, edition_year=year)
            if result.quarantines:
                raise ValueError
            results.append(result)
        if seen_documents != (set(ignored_by_document) | set(annotations_by_document)):
            raise ValueError
        return tuple(results)
    except (AttributeError, KeyError, TypeError, ValueError):
        _raise("resolution_reparse_invalid")
