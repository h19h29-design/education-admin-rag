"""Hash-pinned quarantine policy for reviewed B10 NEIS lifelong schools."""

import csv
import errno
import hashlib
import os
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.institutions.sources.common import SourceDataError, SourceInstitutionRecord

PINNED_POLICY_SHA256: Final = (
    "2a9222d34083261c42ba51fd4430dd6b84b2210908a13e377a64cc69298c51a1"
)
_MAX_POLICY_BYTES: Final = 16 * 1024
_METADATA: Final = (
    ("schemaVersion", "1"),
    ("sourceUrl", "https://open.neis.go.kr/hub/schoolInfo"),
    ("sourceRegionCode", "B10"),
    ("reviewedAsOf", "2026-08-13"),
    ("reviewerRole", "data-steward"),
)
_HEADER: Final = ("school_kind", "expected_count", "reason_code")
_REASON_CODE: Final = "OFFICIAL_CLASSIFICATION_PENDING"
_EXPECTED_TOTAL: Final = 18
_REVIEWED_COUNTS: Final = (
    ("평생학교(고)-2년6학기", 7),
    ("평생학교(고)-3년6학기", 4),
    ("평생학교(중)-2년6학기", 5),
    ("평생학교(초)-3년6학기", 2),
)
_REVIEWED_AS_OF: Final = "2026-08-13"
_REVIEWER_ROLE: Final = "data-steward"


@dataclass(frozen=True)
class NeisUnclassifiedPolicy:
    counts: tuple[tuple[str, int], ...]
    sha256: str
    reviewed_as_of: str
    reviewer_role: str

    def __post_init__(self) -> None:
        if (
            type(self.counts) is not tuple
            or self.counts != _REVIEWED_COUNTS
            or self.sha256 != PINNED_POLICY_SHA256
            or self.reviewed_as_of != _REVIEWED_AS_OF
            or self.reviewer_role != _REVIEWER_ROLE
        ):
            raise SourceDataError(
                "NEIS unclassified policy is not the exact reviewed policy"
            )

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(label for label, _ in self.counts)


def load_neis_unclassified_policy(path: Path) -> NeisUnclassifiedPolicy:
    """Load the one hash-pinned B10 quarantine policy or fail closed."""
    content = _read_policy_bytes(Path(path))
    if hashlib.sha256(content).hexdigest() != PINNED_POLICY_SHA256:
        raise SourceDataError("NEIS unclassified policy SHA-256 is not reviewed")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceDataError("NEIS unclassified policy must be UTF-8") from None
    if not text.endswith("\n"):
        raise SourceDataError("NEIS unclassified policy must end with a newline")

    lines = text.splitlines()
    expected_metadata = [f"# {key}={value}" for key, value in _METADATA]
    if lines[: len(expected_metadata)] != expected_metadata:
        raise SourceDataError("NEIS unclassified policy metadata is invalid")
    data_lines = lines[len(expected_metadata) :]
    reader = csv.reader(data_lines)
    rows = list(reader)
    if not rows or tuple(rows[0]) != _HEADER:
        raise SourceDataError("NEIS unclassified policy columns are invalid")

    counts: list[tuple[str, int]] = []
    for row in rows[1:]:
        if len(row) != len(_HEADER):
            raise SourceDataError("NEIS unclassified policy rows are invalid")
        label, count_text, reason_code = row
        if not label or reason_code != _REASON_CODE:
            raise SourceDataError("NEIS unclassified policy rows are invalid")
        try:
            count = int(count_text)
        except ValueError:
            raise SourceDataError("NEIS unclassified policy counts are invalid") from None
        if type(count) is not int or count <= 0 or str(count) != count_text:
            raise SourceDataError("NEIS unclassified policy counts are invalid")
        counts.append((label, count))
    if not counts:
        raise SourceDataError("NEIS unclassified policy rows are invalid")
    labels = [label for label, _ in counts]
    if labels != sorted(labels) or len(labels) != len(set(labels)):
        raise SourceDataError("NEIS unclassified policy labels are invalid")
    if sum(count for _, count in counts) != _EXPECTED_TOTAL:
        raise SourceDataError("NEIS unclassified policy total is invalid")
    return NeisUnclassifiedPolicy(
        counts=tuple(counts),
        sha256=PINNED_POLICY_SHA256,
        reviewed_as_of=_REVIEWED_AS_OF,
        reviewer_role=_REVIEWER_ROLE,
    )


def _read_policy_bytes(resource: Path) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise SourceDataError("NEIS unclassified policy requires no-follow support")
    try:
        descriptor = os.open(resource, os.O_RDONLY | no_follow)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SourceDataError(
                "NEIS unclassified policy must not be a symlink"
            ) from None
        raise SourceDataError("NEIS unclassified policy cannot be read") from None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SourceDataError("NEIS unclassified policy must be a regular file")
        if before.st_size > _MAX_POLICY_BYTES:
            raise SourceDataError("NEIS unclassified policy exceeds the size limit")
        content = _read_exactly(descriptor, before.st_size)
        after = os.fstat(descriptor)
        if after.st_size != before.st_size:
            raise SourceDataError("NEIS unclassified policy changed while reading")
        return content
    except OSError:
        raise SourceDataError("NEIS unclassified policy cannot be read") from None
    finally:
        os.close(descriptor)


def _read_exactly(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise SourceDataError("NEIS unclassified policy changed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def validate_unclassified_school_counts(
    records: tuple[SourceInstitutionRecord, ...],
    policy: NeisUnclassifiedPolicy,
) -> dict[str, int]:
    """Ensure quarantined NEIS rows exactly match the reviewed raw-label counts."""
    labels = [
        record.source_kind_label
        for record in records
        if record.source_kind_label is not None
    ]
    if any(
        record.institution_type != "UNCLASSIFIED_SCHOOL"
        for record in records
        if record.source_kind_label is not None
    ):
        raise SourceDataError("NEIS unclassified rows must use the quarantine type")
    actual = Counter(labels)
    if set(actual) != policy.labels or dict(actual) != dict(policy.counts):
        raise SourceDataError("NEIS unclassified school counts do not match policy")
    return dict(sorted(actual.items()))
