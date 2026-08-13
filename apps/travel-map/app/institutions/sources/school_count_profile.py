"""Hash-pinned reviewed school-count population profile."""

import csv
import errno
import hashlib
import os
import stat
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.institutions.sources.common import SourceDataError
from app.institutions.sources.neis_classification import (
    PINNED_POLICY_SHA256,
    NeisUnclassifiedPolicy,
)

PINNED_POPULATION_PROFILE_SHA256: Final = (
    "e904a254ab4f0fa264a0ec3894827e6bebbb2b94ab263bf635594c812dd7df06"
)
_MAX_PROFILE_BYTES: Final = 16 * 1024
_METADATA: Final = (
    ("schema_version", "1"),
    ("profile_status", "TEMPORARY_PRELIMINARY_VARIANCE"),
    ("reviewed_as_of", "2026-08-13"),
    ("reviewer_role", "data-steward"),
    ("neis_region_code", "B10"),
    ("neis_fetched_row_count", "1415"),
    ("neis_normalized_row_count", "1414"),
    ("kindergarten_timing", "20261"),
    ("kindergarten_source_as_of", "2026-04-01"),
    ("kindergarten_fetched_row_count", "706"),
    (
        "benchmark_source_url",
        "https://enews.sen.go.kr/uploads/img_smart//2026-06-08/20260608075519432.png",
    ),
    ("benchmark_source_as_of", "2026-03-10"),
    (
        "benchmark_raw_sha256",
        "6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a1705db508afeee15e70",
    ),
    ("unclassified_policy_sha256", PINNED_POLICY_SHA256),
    ("approved_variance_ELEMENTARY_SCHOOL", "1"),
    ("approved_variance_HIGH_SCHOOL", "0"),
    ("approved_variance_KINDERGARTEN", "-18"),
    ("approved_variance_MIDDLE_SCHOOL", "0"),
    ("approved_variance_MISC_SCHOOL", "4"),
    ("approved_variance_SPECIAL_SCHOOL", "0"),
)
_HEADER: Final = (
    "source",
    "source_category",
    "observed_count",
    "normalized_type",
    "reconciliation_role",
    "benchmark_type",
)
_APPROVED_VARIANCES: Final = (
    ("ELEMENTARY_SCHOOL", 1),
    ("HIGH_SCHOOL", 0),
    ("KINDERGARTEN", -18),
    ("MIDDLE_SCHOOL", 0),
    ("MISC_SCHOOL", 4),
    ("SPECIAL_SCHOOL", 0),
)
_REVIEWED_ROWS: Final = (
    ("KINDERGARTEN_INFO", "KINDERGARTEN_TOTAL", 706, "KINDERGARTEN", "BENCHMARK", "KINDERGARTEN"),
    ("NEIS", "각종학교(고)", 13, "MISC_SCHOOL", "BENCHMARK", "MISC_SCHOOL"),
    ("NEIS", "각종학교(중)", 7, "MISC_SCHOOL", "BENCHMARK", "MISC_SCHOOL"),
    ("NEIS", "각종학교(초)", 1, "MISC_SCHOOL", "BENCHMARK", "MISC_SCHOOL"),
    ("NEIS", "고등기술학교", 1, "MISC_SCHOOL", "BENCHMARK", "MISC_SCHOOL"),
    ("NEIS", "고등학교", 319, "HIGH_SCHOOL", "BENCHMARK", "HIGH_SCHOOL"),
    ("NEIS", "공동실습소", 1, None, "NONSELECTABLE", None),
    ("NEIS", "방송통신고등학교", 5, "HIGH_SCHOOL", "SUPPLEMENTARY", None),
    ("NEIS", "방송통신중학교", 1, "MIDDLE_SCHOOL", "SUPPLEMENTARY", None),
    ("NEIS", "외국인학교", 17, "MISC_SCHOOL", "SUPPLEMENTARY", None),
    ("NEIS", "중학교", 390, "MIDDLE_SCHOOL", "BENCHMARK", "MIDDLE_SCHOOL"),
    ("NEIS", "초등학교", 610, "ELEMENTARY_SCHOOL", "BENCHMARK", "ELEMENTARY_SCHOOL"),
    ("NEIS", "특수학교", 32, "SPECIAL_SCHOOL", "BENCHMARK", "SPECIAL_SCHOOL"),
    ("NEIS", "평생학교(고)-2년6학기", 7, "UNCLASSIFIED_SCHOOL", "QUARANTINED", None),
    ("NEIS", "평생학교(고)-3년6학기", 4, "UNCLASSIFIED_SCHOOL", "QUARANTINED", None),
    ("NEIS", "평생학교(중)-2년6학기", 5, "UNCLASSIFIED_SCHOOL", "QUARANTINED", None),
    ("NEIS", "평생학교(초)-3년6학기", 2, "UNCLASSIFIED_SCHOOL", "QUARANTINED", None),
)
_CANONICAL_FIELD_NAMES: Final = frozenset(
    {
        "sha256",
        "status",
        "reviewed_as_of",
        "reviewer_role",
        "neis_region_code",
        "kindergarten_timing",
        "kindergarten_source_as_of",
        "benchmark_source_url",
        "benchmark_source_as_of",
        "benchmark_raw_sha256",
        "unclassified_policy_sha256",
    }
)


@dataclass(frozen=True)
class SchoolPopulationRow:
    source: str
    source_category: str
    observed_count: int
    normalized_type: str | None
    reconciliation_role: str
    benchmark_type: str | None

    def __post_init__(self) -> None:
        fields = (
            self.source,
            self.source_category,
            self.reconciliation_role,
        )
        optional_fields = (self.normalized_type, self.benchmark_type)
        if (
            any(not _is_canonical_string(value) for value in fields)
            or any(
                value is not None and not _is_canonical_string(value)
                for value in optional_fields
            )
            or type(self.observed_count) is not int
            or self.observed_count <= 0
        ):
            raise ValueError("school population row is not canonical")


@dataclass(frozen=True)
class SchoolCountPopulationProfile:
    sha256: str
    status: str
    reviewed_as_of: str
    reviewer_role: str
    neis_region_code: str
    neis_fetched_row_count: int
    neis_normalized_row_count: int
    kindergarten_timing: str
    kindergarten_source_as_of: str
    kindergarten_fetched_row_count: int
    benchmark_source_url: str
    benchmark_source_as_of: str
    benchmark_raw_sha256: str
    unclassified_policy_sha256: str
    approved_variances: tuple[tuple[str, int], ...]
    rows: tuple[SchoolPopulationRow, ...]

    def __post_init__(self) -> None:
        values = {
            "sha256": self.sha256,
            "status": self.status,
            "reviewed_as_of": self.reviewed_as_of,
            "reviewer_role": self.reviewer_role,
            "neis_region_code": self.neis_region_code,
            "kindergarten_timing": self.kindergarten_timing,
            "kindergarten_source_as_of": self.kindergarten_source_as_of,
            "benchmark_source_url": self.benchmark_source_url,
            "benchmark_source_as_of": self.benchmark_source_as_of,
            "benchmark_raw_sha256": self.benchmark_raw_sha256,
            "unclassified_policy_sha256": self.unclassified_policy_sha256,
        }
        expected = dict(_METADATA)
        expected.update(
            {
                "sha256": PINNED_POPULATION_PROFILE_SHA256,
                "status": expected.pop("profile_status"),
            }
        )
        if (
            any(not _is_canonical_string(value) for value in values.values())
            or type(self.neis_fetched_row_count) is not int
            or type(self.neis_normalized_row_count) is not int
            or type(self.kindergarten_fetched_row_count) is not int
            or self.neis_fetched_row_count != 1_415
            or self.neis_normalized_row_count != 1_414
            or self.kindergarten_fetched_row_count != 706
            or not _is_canonical_variances(self.approved_variances)
            or not _is_canonical_rows(self.rows)
            or self.approved_variances != _APPROVED_VARIANCES
            or tuple(_row_values(row) for row in self.rows) != _REVIEWED_ROWS
            or any(values[name] != expected[name] for name in _CANONICAL_FIELD_NAMES)
        ):
            raise ValueError("school count population profile is not the reviewed contract")

    def source_category_counts(self, source: str) -> dict[str, int]:
        return {
            row.source_category: row.observed_count
            for row in self.rows
            if row.source == source
        }

    def source_totals(self) -> dict[str, int]:
        totals: Counter[str] = Counter()
        for row in self.rows:
            totals[row.source] += row.observed_count
        return dict(sorted(totals.items()))

    def role_counts(self, source: str) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for row in self.rows:
            if row.source == source:
                counts[row.reconciliation_role] += row.observed_count
        return dict(sorted(counts.items()))


def load_school_count_population_profile(
    path: Path,
    *,
    unclassified_policy: NeisUnclassifiedPolicy,
) -> SchoolCountPopulationProfile:
    """Load the exact reviewed school-count profile or fail closed."""
    _validate_unclassified_policy(unclassified_policy)
    content = _read_profile_bytes(Path(path))
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceDataError("school count population profile must be UTF-8") from None
    if not text.endswith("\n"):
        raise SourceDataError("school count population profile must end with a newline")
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# normalized_sha256="):
        raise SourceDataError("school count population profile digest is invalid")
    digest = lines[0].removeprefix("# normalized_sha256=")
    if not _is_sha256(digest):
        raise SourceDataError("school count population profile digest is invalid")
    canonical = "\n".join(lines[1:]) + "\n"
    actual_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != actual_digest or digest != PINNED_POPULATION_PROFILE_SHA256:
        raise SourceDataError("school count population profile SHA-256 is not reviewed")

    metadata, data_lines = _parse_metadata(lines[1:])
    if tuple(metadata.items()) != _METADATA:
        raise SourceDataError("school count population profile metadata is invalid")
    rows = _parse_rows(data_lines)
    if tuple(_row_values(row) for row in rows) != _REVIEWED_ROWS:
        raise SourceDataError("school count population profile rows are invalid")
    _validate_quarantine_rows(rows, unclassified_policy)
    try:
        return SchoolCountPopulationProfile(
            sha256=digest,
            status=metadata["profile_status"],
            reviewed_as_of=metadata["reviewed_as_of"],
            reviewer_role=metadata["reviewer_role"],
            neis_region_code=metadata["neis_region_code"],
            neis_fetched_row_count=_parse_int(metadata["neis_fetched_row_count"]),
            neis_normalized_row_count=_parse_int(metadata["neis_normalized_row_count"]),
            kindergarten_timing=metadata["kindergarten_timing"],
            kindergarten_source_as_of=metadata["kindergarten_source_as_of"],
            kindergarten_fetched_row_count=_parse_int(
                metadata["kindergarten_fetched_row_count"]
            ),
            benchmark_source_url=metadata["benchmark_source_url"],
            benchmark_source_as_of=metadata["benchmark_source_as_of"],
            benchmark_raw_sha256=metadata["benchmark_raw_sha256"],
            unclassified_policy_sha256=metadata["unclassified_policy_sha256"],
            approved_variances=tuple(
                (kind, _parse_int(metadata[f"approved_variance_{kind}"]))
                for kind, _ in _APPROVED_VARIANCES
            ),
            rows=rows,
        )
    except (KeyError, ValueError):
        raise SourceDataError("school count population profile is invalid") from None


def _parse_metadata(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    metadata: dict[str, str] = {}
    data_start = 0
    for data_start, line in enumerate(lines):
        if not line.startswith("# "):
            break
        key, separator, value = line[2:].partition("=")
        if not separator or not _is_canonical_string(key) or not _is_canonical_string(value):
            raise SourceDataError("school count population profile metadata is invalid")
        if key in metadata:
            raise SourceDataError("school count population profile metadata is invalid")
        metadata[key] = value
    else:
        raise SourceDataError("school count population profile columns are invalid")
    return metadata, lines[data_start:]


def _parse_rows(data_lines: list[str]) -> tuple[SchoolPopulationRow, ...]:
    reader = csv.reader(data_lines)
    parsed = list(reader)
    if not parsed or tuple(parsed[0]) != _HEADER:
        raise SourceDataError("school count population profile columns are invalid")
    rows: list[SchoolPopulationRow] = []
    for fields in parsed[1:]:
        if len(fields) != len(_HEADER) or any(
            not _is_canonical_string(value) for value in fields if value
        ):
            raise SourceDataError("school count population profile rows are invalid")
        source, source_category, count_text, normalized_type, role, benchmark_type = fields
        try:
            count = _parse_int(count_text)
            row = SchoolPopulationRow(
                source=source,
                source_category=source_category,
                observed_count=count,
                normalized_type=normalized_type or None,
                reconciliation_role=role,
                benchmark_type=benchmark_type or None,
            )
        except ValueError:
            raise SourceDataError("school count population profile rows are invalid") from None
        rows.append(row)
    keys = [(row.source, row.source_category) for row in rows]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise SourceDataError("school count population profile rows are not sorted and unique")
    return tuple(rows)


def _validate_unclassified_policy(policy: NeisUnclassifiedPolicy) -> None:
    if (
        type(policy) is not NeisUnclassifiedPolicy
        or policy.sha256 != PINNED_POLICY_SHA256
        or policy.counts
        != (
            ("평생학교(고)-2년6학기", 7),
            ("평생학교(고)-3년6학기", 4),
            ("평생학교(중)-2년6학기", 5),
            ("평생학교(초)-3년6학기", 2),
        )
    ):
        raise SourceDataError("school count population profile policy is not reviewed")


def _validate_quarantine_rows(
    rows: tuple[SchoolPopulationRow, ...], policy: NeisUnclassifiedPolicy
) -> None:
    quarantine = tuple(
        (row.source_category, row.observed_count)
        for row in rows
        if row.source == "NEIS" and row.reconciliation_role == "QUARANTINED"
    )
    if quarantine != policy.counts:
        raise SourceDataError("school count population profile quarantine rows are invalid")


def _read_profile_bytes(resource: Path) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise SourceDataError("school count population profile requires no-follow support")
    try:
        descriptor = os.open(resource, os.O_RDONLY | no_follow)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SourceDataError("school count population profile must not be a symlink") from None
        raise SourceDataError("school count population profile cannot be read") from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceDataError("school count population profile must be a regular file")
        if before.st_size > _MAX_PROFILE_BYTES:
            raise SourceDataError("school count population profile exceeds the size limit")
        content = _read_exactly(descriptor, before.st_size)
        after = os.fstat(descriptor)
        if after.st_size != before.st_size:
            raise SourceDataError("school count population profile changed while reading")
        return content
    except OSError:
        raise SourceDataError("school count population profile cannot be read") from None
    finally:
        os.close(descriptor)


def _read_exactly(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise SourceDataError("school count population profile changed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parse_int(value: str) -> int:
    if not _is_canonical_string(value):
        raise ValueError("not canonical")
    try:
        number = int(value)
    except ValueError:
        raise ValueError("not an integer") from None
    if type(number) is not int or str(number) != value:
        raise ValueError("not canonical")
    return number


def _is_canonical_string(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and unicodedata.normalize("NFC", value) == value
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _is_canonical_variances(value: object) -> bool:
    return type(value) is tuple and all(
        type(entry) is tuple
        and len(entry) == 2
        and _is_canonical_string(entry[0])
        and type(entry[1]) is int
        for entry in value
    )


def _is_canonical_rows(value: object) -> bool:
    return type(value) is tuple and all(type(row) is SchoolPopulationRow for row in value)


def _row_values(row: SchoolPopulationRow) -> tuple[str, str, int, str | None, str, str | None]:
    return (
        row.source,
        row.source_category,
        row.observed_count,
        row.normalized_type,
        row.reconciliation_role,
        row.benchmark_type,
    )
