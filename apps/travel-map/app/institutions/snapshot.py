import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.institutions.models import Institution, InstitutionSite, SnapshotManifest

_SAFE_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_MANIFEST_FIELDS = {
    "schemaVersion",
    "snapshotId",
    "createdAt",
    "snapshotAsOf",
    "approved",
    "approvedAt",
    "approvedByRole",
    "sources",
    "institutionsSha256",
    "sitesSha256",
    "institutionCount",
    "siteCount",
    "quarantinedCount",
    "possibleMatchCount",
    "countsByType",
    "countsByFoundation",
    "countsByStatus",
    "coordinateQualityCounts",
    "diff",
}
_SOURCE_FIELDS = {
    "source",
    "endpoint",
    "licenseName",
    "attribution",
    "fetchedAt",
    "sourceAsOf",
    "rawSha256",
    "pageCount",
    "rowCount",
}
_DIFF_FIELDS = {
    "previousSnapshotId",
    "addedCount",
    "changedCount",
    "missingCount",
    "closedCandidateCount",
}
_Model = TypeVar("_Model", bound=BaseModel)


class SnapshotIntegrityError(ValueError):
    """Raised when a selected institution snapshot is not exactly approved."""


@dataclass(frozen=True)
class VerifiedSnapshot:
    snapshot_path: Path
    manifest: SnapshotManifest
    institutions: tuple[Institution, ...]
    sites: tuple[InstitutionSite, ...]


def verify_snapshot(snapshot_root: Path) -> VerifiedSnapshot:
    root = _resolve_directory(Path(snapshot_root), "snapshot root")
    current_path = _resolve_file(root / "current.json", root, "current.json")
    current = _read_json_object(current_path, "current.json")
    if set(current) != {"snapshotId"}:
        raise SnapshotIntegrityError(
            "current.json must contain exactly the snapshotId field"
        )
    snapshot_id = current["snapshotId"]
    if (
        type(snapshot_id) is not str
        or _SAFE_SNAPSHOT_ID.fullmatch(snapshot_id) is None
    ):
        raise SnapshotIntegrityError("current snapshotId must be a safe slug")

    snapshot_path = _resolve_snapshot_directory(root, snapshot_id)
    manifest_path = _resolve_file(
        snapshot_path / "manifest.json",
        snapshot_path,
        "manifest.json",
    )
    manifest = _read_manifest(manifest_path)
    if manifest.snapshot_id != snapshot_id:
        raise SnapshotIntegrityError(
            "manifest snapshotId does not match current snapshotId"
        )

    institutions_path = _resolve_file(
        snapshot_path / "institutions.jsonl",
        snapshot_path,
        "institutions.jsonl",
    )
    sites_path = _resolve_file(
        snapshot_path / "sites.jsonl",
        snapshot_path,
        "sites.jsonl",
    )
    institution_bytes = _read_bytes(institutions_path, "institutions.jsonl")
    site_bytes = _read_bytes(sites_path, "sites.jsonl")
    _verify_hash(
        institution_bytes,
        manifest.institutions_sha256,
        "institutions.jsonl",
    )
    _verify_hash(site_bytes, manifest.sites_sha256, "sites.jsonl")

    institutions = _parse_jsonl(
        institution_bytes,
        Institution,
        "institutions.jsonl",
    )
    sites = _parse_jsonl(site_bytes, InstitutionSite, "sites.jsonl")
    _verify_records(snapshot_id, manifest, institutions, sites)
    return VerifiedSnapshot(
        snapshot_path=snapshot_path,
        manifest=manifest,
        institutions=institutions,
        sites=sites,
    )


def _resolve_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotIntegrityError(f"{label} does not exist") from exc
    if not resolved.is_dir():
        raise SnapshotIntegrityError(f"{label} must be a directory")
    return resolved


def _resolve_snapshot_directory(root: Path, snapshot_id: str) -> Path:
    candidate = root / snapshot_id
    if candidate.is_symlink():
        raise SnapshotIntegrityError(
            "snapshot directory must remain inside the snapshot root"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotIntegrityError("selected snapshot directory does not exist") from exc
    if not resolved.is_relative_to(root) or not resolved.is_dir():
        raise SnapshotIntegrityError(
            "snapshot directory must remain inside the snapshot root"
        )
    return resolved


def _resolve_file(candidate: Path, parent: Path, label: str) -> Path:
    if candidate.is_symlink():
        raise SnapshotIntegrityError(
            f"{label} must remain inside the snapshot directory"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotIntegrityError(f"{label} does not exist") from exc
    if not resolved.is_relative_to(parent) or not resolved.is_file():
        raise SnapshotIntegrityError(
            f"{label} must remain inside the snapshot directory"
        )
    return resolved


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(f"{label} must be valid UTF-8 JSON") from exc
    if type(value) is not dict:
        raise SnapshotIntegrityError(f"{label} must contain a JSON object")
    return value


def _read_manifest(path: Path) -> SnapshotManifest:
    try:
        data = path.read_bytes()
        decoded = json.loads(data)
        _verify_manifest_fields(decoded)
        return SnapshotManifest.model_validate_json(data)
    except SnapshotIntegrityError:
        raise
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise SnapshotIntegrityError(f"manifest.json is invalid: {exc}") from exc


def _verify_manifest_fields(value: object) -> None:
    if type(value) is not dict or set(value) != _MANIFEST_FIELDS:
        raise SnapshotIntegrityError(
            "manifest.json fields must exactly match schema version 1"
        )
    sources = value["sources"]
    if type(sources) is list:
        for source in sources:
            if type(source) is not dict or set(source) != _SOURCE_FIELDS:
                raise SnapshotIntegrityError(
                    "manifest.json fields must exactly match schema version 1"
                )
    diff = value["diff"]
    if type(diff) is dict and set(diff) != _DIFF_FIELDS:
        raise SnapshotIntegrityError(
            "manifest.json fields must exactly match schema version 1"
        )


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise SnapshotIntegrityError(f"cannot read {label}") from exc


def _verify_hash(data: bytes, expected: str, label: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SnapshotIntegrityError(
            f"{label} sha256 mismatch: expected {expected}, got {actual}"
        )


def _parse_jsonl(
    data: bytes,
    model: type[_Model],
    label: str,
) -> tuple[_Model, ...]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotIntegrityError(f"{label} must be valid UTF-8") from exc
    lines = text.splitlines()
    if not lines:
        raise SnapshotIntegrityError(f"{label} must contain at least one record")

    records: list[_Model] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise SnapshotIntegrityError(f"{label} line {line_number} is blank")
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SnapshotIntegrityError(
                f"{label} line {line_number} is malformed JSON"
            ) from exc
        if type(decoded) is not dict:
            raise SnapshotIntegrityError(
                f"{label} line {line_number} must contain a JSON object"
            )
        try:
            records.append(model.model_validate_json(line))
        except ValidationError as exc:
            raise SnapshotIntegrityError(
                f"{label} line {line_number} contains invalid model data: {exc}"
            ) from exc
    return tuple(records)


def _verify_records(
    snapshot_id: str,
    manifest: SnapshotManifest,
    institutions: tuple[Institution, ...],
    sites: tuple[InstitutionSite, ...],
) -> None:
    if len(institutions) != manifest.institution_count:
        raise SnapshotIntegrityError(
            "institutionCount does not match institutions.jsonl row count"
        )
    if len(sites) != manifest.site_count:
        raise SnapshotIntegrityError("siteCount does not match sites.jsonl row count")

    institution_ids = _unique_ids(
        (item.institution_id for item in institutions),
        "institutionId",
    )
    _unique_ids((item.site_id for item in sites), "siteId")
    for institution in institutions:
        if institution.last_seen_snapshot != snapshot_id:
            raise SnapshotIntegrityError(
                f"institution {institution.institution_id} lastSeenSnapshot mismatch"
            )
    for site in sites:
        if site.institution_id not in institution_ids:
            raise SnapshotIntegrityError(
                f"site {site.site_id} references unknown institutionId "
                f"{site.institution_id}"
            )

    _verify_count_map(
        manifest.counts_by_type,
        Counter(item.institution_type for item in institutions),
        "countsByType",
    )
    _verify_count_map(
        manifest.counts_by_foundation,
        Counter(item.foundation_type for item in institutions),
        "countsByFoundation",
    )
    _verify_count_map(
        manifest.counts_by_status,
        Counter(item.status.value for item in institutions),
        "countsByStatus",
    )
    _verify_count_map(
        manifest.coordinate_quality_counts,
        Counter(item.coordinate_quality for item in sites),
        "coordinateQualityCounts",
    )
    _verify_source_counts(manifest, institutions)


def _unique_ids(values: Iterable[str], label: str) -> set[str]:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise SnapshotIntegrityError(f"duplicate {label}: {value}")
        seen.add(value)
    return seen


def _verify_count_map(
    declared: dict[str, int],
    actual: Counter[str],
    label: str,
) -> None:
    if declared != dict(actual):
        raise SnapshotIntegrityError(
            f"{label} does not match loaded records: expected {dict(actual)}"
        )


def _verify_source_counts(
    manifest: SnapshotManifest,
    institutions: tuple[Institution, ...],
) -> None:
    declared: dict[str, int] = {}
    for source in manifest.sources:
        if source.source in declared:
            raise SnapshotIntegrityError(f"duplicate manifest source: {source.source}")
        declared[source.source] = source.row_count
    actual = Counter(item.source for item in institutions)
    if declared.keys() != actual.keys():
        raise SnapshotIntegrityError("manifest sources do not match institution sources")
    for source_name, row_count in declared.items():
        if row_count != actual[source_name]:
            raise SnapshotIntegrityError(
                f"source {source_name} rowCount does not match institution records"
            )
    if sum(declared.values()) != len(institutions):
        raise SnapshotIntegrityError(
            "source rowCount sum does not match institutionCount"
        )
