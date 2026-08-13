"""Hash-pinned quarantine policy for reviewed B10 NEIS lifelong schools."""

import csv
import hashlib
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


@dataclass(frozen=True)
class NeisUnclassifiedPolicy:
    counts: tuple[tuple[str, int], ...]
    sha256: str
    reviewed_as_of: str
    reviewer_role: str

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(label for label, _ in self.counts)


def load_neis_unclassified_policy(path: Path) -> NeisUnclassifiedPolicy:
    """Load the one hash-pinned B10 quarantine policy or fail closed."""
    resource = Path(path)
    if resource.is_symlink() or not resource.is_file():
        raise SourceDataError("NEIS unclassified policy must be a regular file")
    try:
        with resource.open("rb") as handle:
            content = handle.read(_MAX_POLICY_BYTES + 1)
    except OSError:
        raise SourceDataError("NEIS unclassified policy cannot be read") from None
    if len(content) > _MAX_POLICY_BYTES:
        raise SourceDataError("NEIS unclassified policy exceeds the size limit")
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
        reviewed_as_of=dict(_METADATA)["reviewedAsOf"],
        reviewer_role=dict(_METADATA)["reviewerRole"],
    )


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
