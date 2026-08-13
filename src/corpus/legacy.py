"""Value-minimized legacy-title comparison for canonical ID migration."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_MAX_LEGACY_HTML_BYTES = 16 * 1024 * 1024
_MAX_LEGACY_CASES = 10_000
_RELEASE_RE = re.compile(r"^corpus-\d{14}-[0-9a-f]{8}$")


class LegacyError(RuntimeError):
    """A value-free failure at the legacy comparison boundary."""


def _raise(code: str) -> NoReturn:
    raise LegacyError(code) from None


class _LegacyModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        hide_input_in_errors=True,
    )


class LegacyIndexEntry(_LegacyModel):
    legacy_id: str = Field(pattern=r"^[A-Z]{2,4}-(?:\d{4}-)?\d{3,5}$")
    title: str = Field(min_length=1, max_length=2_000)
    edition_year: int = Field(ge=2020, le=2025)


class CanonicalTitleEntry(_LegacyModel):
    case_id: str = Field(pattern=r"^case-[a-z0-9][a-z0-9-]{2,190}[a-z0-9]$")
    title: str = Field(min_length=1, max_length=2_000)
    edition_year: int = Field(ge=2020, le=2025)


class LegacyMapEntry(_LegacyModel):
    legacy_id: str = Field(pattern=r"^[A-Z]{2,4}-(?:\d{4}-)?\d{3,5}$")
    title: str = Field(min_length=1, max_length=2_000)
    edition_year: int = Field(ge=2020, le=2025)
    case_id: str | None = Field(
        default=None,
        pattern=r"^case-[a-z0-9][a-z0-9-]{2,190}[a-z0-9]$",
    )
    mapping_confidence: Literal["exact_unique", "none", "ambiguous"]
    review_status: Literal["pending", "unmapped", "ambiguous", "verified"]

    @model_validator(mode="after")
    def mapping_state_is_coherent(self) -> LegacyMapEntry:
        if self.case_id is None:
            if self.mapping_confidence == "exact_unique" or self.review_status in {
                "pending",
                "verified",
            }:
                raise ValueError("legacy mapping state is invalid")
        elif self.mapping_confidence != "exact_unique" or self.review_status not in {
            "pending",
            "verified",
        }:
            raise ValueError("legacy mapping state is invalid")
        return self


class LegacyMapReport(_LegacyModel):
    items: tuple[LegacyMapEntry, ...] = Field(max_length=_MAX_LEGACY_CASES)
    mapped_count: int = Field(ge=0)
    unmapped_count: int = Field(ge=0)
    ambiguous_count: int = Field(ge=0)

    @model_validator(mode="after")
    def counts_match_items(self) -> LegacyMapReport:
        if (
            self.mapped_count != sum(item.case_id is not None for item in self.items)
            or self.unmapped_count
            != sum(item.review_status == "unmapped" for item in self.items)
            or self.ambiguous_count
            != sum(item.review_status == "ambiguous" for item in self.items)
        ):
            raise ValueError("legacy mapping counts are invalid")
        return self


def _read_html(path: Path) -> str:
    payload: bytes | None = None
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 1
            or before.st_size > _MAX_LEGACY_HTML_BYTES
        ):
            raise OSError
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                descriptor, min(1024 * 1024, _MAX_LEGACY_HTML_BYTES + 1 - total)
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_LEGACY_HTML_BYTES:
                raise OSError
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or total != before.st_size:
            raise OSError
        payload = b"".join(chunks)
        if payload.startswith(b"\xef\xbb\xbf"):
            payload = payload[3:]
        text = payload.decode("utf-8") if payload is not None else None
    except (OSError, UnicodeError):
        text = None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if text is None:
        _raise("legacy_index_invalid")
    return text


def load_legacy_index(path: Path) -> tuple[LegacyIndexEntry, ...]:
    """Load only legacy ID, title, and year from the embedded launcher index."""
    if not isinstance(path, Path):
        _raise("legacy_index_invalid")
    text = _read_html(path)
    marker = "window.APP ="
    start = text.find(marker)
    if start < 0:
        _raise("legacy_index_invalid")
    try:
        payload = text[start + len(marker) :].lstrip()
        decoded, _ = json.JSONDecoder().raw_decode(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        decoded = None
    if type(decoded) is not dict:
        _raise("legacy_index_invalid")
    raw_cases = decoded.get("cases")
    if type(raw_cases) is not list or not 1 <= len(raw_cases) <= _MAX_LEGACY_CASES:
        _raise("legacy_index_invalid")
    entries: list[LegacyIndexEntry] = []
    seen: set[str] = set()
    invalid = False
    try:
        for raw_case in raw_cases:
            if type(raw_case) is not dict:
                invalid = True
                break
            legacy_id = raw_case.get("id")
            title = raw_case.get("title")
            raw_year = raw_case.get("year")
            if type(raw_year) is str and len(raw_year) == 4 and raw_year.isascii():
                year: object = int(raw_year)
            else:
                year = raw_year
            entry = LegacyIndexEntry.model_validate(
                {
                    "legacy_id": legacy_id,
                    "title": title,
                    "edition_year": year,
                }
            )
            if entry.legacy_id in seen:
                invalid = True
                break
            seen.add(entry.legacy_id)
            entries.append(entry)
    except (ValidationError, TypeError, ValueError):
        invalid = True
    if invalid:
        _raise("legacy_index_invalid")
    return tuple(entries)


def _normalized_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _revalidate_model(
    value: object, model_type: type[_LegacyModel]
) -> _LegacyModel | None:
    if type(value) is not model_type or type(value.__dict__) is not dict:
        return None
    raw = dict(value.__dict__)
    if set(raw) != set(model_type.model_fields):
        return None
    try:
        return model_type.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return None


def build_legacy_map(
    legacy_entries: tuple[LegacyIndexEntry, ...],
    canonical_entries: tuple[CanonicalTitleEntry, ...],
) -> LegacyMapReport:
    """Create only unique year/title mappings and leave collisions for review."""
    if type(legacy_entries) is not tuple or type(canonical_entries) is not tuple:
        _raise("legacy_mapping_invalid")
    by_title: dict[tuple[int, str], list[str]] = defaultdict(list)
    seen_cases: set[str] = set()
    checked_legacy_raw = tuple(
        _revalidate_model(item, LegacyIndexEntry) for item in legacy_entries
    )
    checked_canonical_raw = tuple(
        _revalidate_model(item, CanonicalTitleEntry) for item in canonical_entries
    )
    if any(item is None for item in checked_legacy_raw + checked_canonical_raw):
        _raise("legacy_mapping_invalid")
    checked_legacy = tuple(
        item for item in checked_legacy_raw if isinstance(item, LegacyIndexEntry)
    )
    checked_canonical = tuple(
        item for item in checked_canonical_raw if isinstance(item, CanonicalTitleEntry)
    )
    if len({item.legacy_id for item in checked_legacy}) != len(checked_legacy):
        _raise("legacy_mapping_invalid")
    for canonical_item in checked_canonical:
        if canonical_item.case_id in seen_cases:
            _raise("legacy_mapping_invalid")
        seen_cases.add(canonical_item.case_id)
        by_title[
            (
                canonical_item.edition_year,
                _normalized_title(canonical_item.title),
            )
        ].append(canonical_item.case_id)
    mapped: list[LegacyMapEntry] = []
    for legacy_item in checked_legacy:
        candidates = by_title[
            (
                legacy_item.edition_year,
                _normalized_title(legacy_item.title),
            )
        ]
        if len(candidates) == 1:
            mapped.append(
                LegacyMapEntry(
                    legacy_id=legacy_item.legacy_id,
                    title=legacy_item.title,
                    edition_year=legacy_item.edition_year,
                    case_id=candidates[0],
                    mapping_confidence="exact_unique",
                    review_status="pending",
                )
            )
        elif not candidates:
            mapped.append(
                LegacyMapEntry(
                    legacy_id=legacy_item.legacy_id,
                    title=legacy_item.title,
                    edition_year=legacy_item.edition_year,
                    case_id=None,
                    mapping_confidence="none",
                    review_status="unmapped",
                )
            )
        else:
            mapped.append(
                LegacyMapEntry(
                    legacy_id=legacy_item.legacy_id,
                    title=legacy_item.title,
                    edition_year=legacy_item.edition_year,
                    case_id=None,
                    mapping_confidence="ambiguous",
                    review_status="ambiguous",
                )
            )
    return LegacyMapReport(
        items=tuple(mapped),
        mapped_count=sum(item.case_id is not None for item in mapped),
        unmapped_count=sum(item.review_status == "unmapped" for item in mapped),
        ambiguous_count=sum(item.review_status == "ambiguous" for item in mapped),
    )


def write_legacy_report(
    reports_root: Path,
    release_id: str,
    report: LegacyMapReport,
) -> Path:
    """Write a deterministic title-only mapping report without legacy body text."""
    if (
        not isinstance(reports_root, Path)
        or type(release_id) is not str
        or _RELEASE_RE.fullmatch(release_id) is None
        or type(report) is not LegacyMapReport
    ):
        _raise("legacy_report_invalid")
    target = reports_root / release_id / "legacy-map.jsonl"
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists() or target.is_symlink():
            _raise("legacy_report_exists")
        rows = b"".join(
            json.dumps(
                item.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
            for item in report.items
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(rows)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass
            raise
        return target
    except LegacyError:
        raise
    except (OSError, TypeError, ValueError):
        _raise("legacy_report_invalid")
