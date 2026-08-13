import hashlib
import json
import re
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.institutions.models import (
    Institution,
    InstitutionSite,
    InstitutionStatus,
    SnapshotManifest,
)
from app.institutions.sources.neis_classification import (
    PINNED_POLICY_SHA256,
    NeisUnclassifiedPolicy,
)

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
    "enrichments",
    "institutionsSha256",
    "sitesSha256",
    "institutionCount",
    "siteCount",
    "quarantinedCount",
    "possibleMatchCount",
    "possibleMatches",
    "countsByType",
    "countsByFoundation",
    "countsByStatus",
    "coordinateQualityCounts",
    "schoolCountReconciliation",
    "diff",
}
_SOURCE_FIELDS = {
    "source",
    "endpoint",
    "licenseName",
    "attribution",
    "fetchedAt",
    "sourceAsOf",
    "sourceObservationDateCounts",
    "normalizedObservationDateCounts",
    "preservedObservationDateCounts",
    "rawSha256",
    "sourceNormalizedSha256",
    "normalizedSha256",
    "requestRegionCode",
    "requestTiming",
    "pageCount",
    "fetchedRowCount",
    "normalizedRowCount",
    "preservedRowCount",
    "rowCount",
    "unclassifiedSchoolKindCounts",
    "unclassifiedSchoolPolicySha256",
    "sourceCategoryCounts",
    "sourcePopulationRoleCounts",
    "sourcePopulationProfileSha256",
}
_ENRICHMENT_FIELDS = {
    "source",
    "endpoint",
    "licenseName",
    "attribution",
    "fetchedAt",
    "sourceAsOf",
    "rawSha256",
    "sourceNormalizedSha256",
    "normalizedSha256",
    "requestRegionCode",
    "requestTiming",
    "pageCount",
    "fetchedRowCount",
    "matchedRowCount",
    "preservedMatchedRowCount",
    "rowCount",
}
_DIFF_FIELDS = {
    "previousSnapshotId",
    "addedCount",
    "changedCount",
    "missingCount",
    "closedCandidateCount",
}
_POSSIBLE_MATCH_FIELDS = {"institutionIds", "reason"}
_INSTITUTION_FIELDS = {
    "institutionId",
    "officialName",
    "institutionType",
    "foundationType",
    "educationOffice",
    "status",
    "statusSource",
    "effectiveFrom",
    "effectiveTo",
    "lastSeenSnapshot",
    "aliases",
    "supersedes",
    "mergedInto",
    "source",
    "sourceRegionCode",
    "sourceAsOf",
}
_SITE_FIELDS = {
    "siteId",
    "institutionId",
    "siteName",
    "roadAddress",
    "district",
    "latitude",
    "longitude",
    "coordinateQuality",
    "routingAnchorLatitude",
    "routingAnchorLongitude",
    "isDefault",
    "status",
    "effectiveFrom",
    "effectiveTo",
}
_SAFE_SITE_SUFFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
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

    return _verify_snapshot_directory(root, snapshot_id)


def verify_snapshot_directory(
    snapshot_root: Path,
    snapshot_id: str,
) -> VerifiedSnapshot:
    root = _resolve_directory(Path(snapshot_root), "snapshot root")
    if _SAFE_SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise SnapshotIntegrityError("snapshotId must be a safe slug")
    return _verify_snapshot_directory(root, snapshot_id)


def _verify_snapshot_directory(root: Path, snapshot_id: str) -> VerifiedSnapshot:
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
        value = _strict_json_loads(path.read_text(encoding="utf-8"))
    except SnapshotIntegrityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError(f"{label} must be valid UTF-8 JSON") from exc
    if type(value) is not dict:
        raise SnapshotIntegrityError(f"{label} must contain a JSON object")
    return value


def _read_manifest(path: Path) -> SnapshotManifest:
    try:
        data = path.read_bytes()
        decoded = _strict_json_loads(data)
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
    enrichments = value["enrichments"]
    if type(enrichments) is not list:
        raise SnapshotIntegrityError(
            "manifest.json fields must exactly match schema version 1"
        )
    for enrichment in enrichments:
        if type(enrichment) is not dict or set(enrichment) != _ENRICHMENT_FIELDS:
            raise SnapshotIntegrityError(
                "manifest.json fields must exactly match schema version 1"
            )
    diff = value["diff"]
    if type(diff) is dict and set(diff) != _DIFF_FIELDS:
        raise SnapshotIntegrityError(
            "manifest.json fields must exactly match schema version 1"
        )
    possible_matches = value["possibleMatches"]
    if type(possible_matches) is not list:
        raise SnapshotIntegrityError(
            "manifest.json fields must exactly match schema version 1"
        )
    for possible_match in possible_matches:
        if (
            type(possible_match) is not dict
            or set(possible_match) != _POSSIBLE_MATCH_FIELDS
        ):
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
            decoded = _strict_json_loads(line)
        except SnapshotIntegrityError:
            raise
        except json.JSONDecodeError as exc:
            raise SnapshotIntegrityError(
                f"{label} line {line_number} is malformed JSON"
            ) from exc
        if type(decoded) is not dict:
            raise SnapshotIntegrityError(
                f"{label} line {line_number} must contain a JSON object"
            )
        expected_fields = (
            _INSTITUTION_FIELDS if model is Institution else _SITE_FIELDS
        )
        if set(decoded) != expected_fields:
            raise SnapshotIntegrityError(
                f"{label} line {line_number} fields must exactly match "
                "schema version 1"
            )
        try:
            records.append(model.model_validate_json(line))
        except ValidationError as exc:
            raise SnapshotIntegrityError(
                f"{label} line {line_number} contains invalid model data: {exc}"
            ) from exc
    return tuple(records)


def _strict_json_loads(data: str | bytes) -> object:
    return json.loads(
        data,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_nonstandard_constant,
    )


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotIntegrityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonstandard_constant(value: str) -> object:
    raise SnapshotIntegrityError(f"nonstandard JSON constant: {value}")


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
    quarantined_count = sum(
        institution.status.value == "REVIEW_REQUIRED"
        for institution in institutions
    )
    if manifest.quarantined_count != quarantined_count:
        raise SnapshotIntegrityError(
            "quarantinedCount does not match institution records"
        )

    institution_ids = _unique_ids(
        (item.institution_id for item in institutions),
        "institutionId",
    )
    institutions_by_id = {
        institution.institution_id: institution for institution in institutions
    }
    _unique_ids((item.site_id for item in sites), "siteId")
    _verify_lineage(institutions, institution_ids)
    for institution in institutions:
        if institution.last_seen_snapshot != snapshot_id:
            raise SnapshotIntegrityError(
                f"institution {institution.institution_id} lastSeenSnapshot mismatch"
            )
        if institution.status.value == "ACTIVE" and not _is_effective_on(
            institution.effective_from,
            institution.effective_to,
            manifest.snapshot_as_of,
        ):
            raise SnapshotIntegrityError(
                f"ACTIVE institution {institution.institution_id} is not effective "
                "on snapshotAsOf"
            )
        if institution.institution_type == "UNCLASSIFIED_SCHOOL" and (
            institution.source not in {"NEIS", "TEST_NEIS"}
            or institution.status is not InstitutionStatus.REVIEW_REQUIRED
            or institution.status_source != "OFFICIAL_CLASSIFICATION_PENDING"
        ):
            raise SnapshotIntegrityError(
                "unclassified institution must remain pending official classification"
            )
    for site in sites:
        if site.institution_id not in institution_ids:
            raise SnapshotIntegrityError(
                f"site {site.site_id} references unknown institutionId "
                f"{site.institution_id}"
            )
        site_prefix = f"{site.institution_id}:"
        site_suffix = site.site_id.removeprefix(site_prefix)
        if (
            not site.site_id.startswith(site_prefix)
            or _SAFE_SITE_SUFFIX.fullmatch(site_suffix) is None
        ):
            raise SnapshotIntegrityError(
                f"site {site.site_id} siteId must begin with parent institutionId "
                "and a safe suffix"
            )
        if site.status.value == "ACTIVE" and not _is_effective_on(
            site.effective_from,
            site.effective_to,
            manifest.snapshot_as_of,
        ):
            raise SnapshotIntegrityError(
                f"ACTIVE site {site.site_id} is not effective on snapshotAsOf"
            )
        if (
            institutions_by_id[site.institution_id].institution_type
            == "UNCLASSIFIED_SCHOOL"
            and site.status is not InstitutionStatus.REVIEW_REQUIRED
        ):
            raise SnapshotIntegrityError(
                "unclassified institution sites must remain review required"
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
    _verify_school_count_reconciliation(manifest, institutions)
    _verify_possible_matches(manifest, institution_ids)


def _verify_possible_matches(
    manifest: SnapshotManifest,
    institution_ids: set[str],
) -> None:
    if manifest.possible_match_count != len(manifest.possible_matches):
        raise SnapshotIntegrityError(
            "possibleMatchCount does not match possibleMatches"
        )
    pairs: set[tuple[str, str]] = set()
    for possible_match in manifest.possible_matches:
        pair = possible_match.institution_ids
        if pair in pairs:
            raise SnapshotIntegrityError("duplicate possible institution match")
        pairs.add(pair)
        if any(institution_id not in institution_ids for institution_id in pair):
            raise SnapshotIntegrityError(
                "possible institution match references unknown institutionId"
            )


def _unique_ids(values: Iterable[str], label: str) -> set[str]:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise SnapshotIntegrityError(f"duplicate {label}: {value}")
        seen.add(value)
    return seen


def _verify_lineage(
    institutions: tuple[Institution, ...],
    institution_ids: set[str],
) -> None:
    graph: dict[str, set[str]] = {
        institution_id: set() for institution_id in institution_ids
    }
    for institution in institutions:
        targets = list(institution.supersedes)
        if institution.merged_into is not None:
            targets.append(institution.merged_into)
        if len(targets) != len(set(targets)):
            raise SnapshotIntegrityError(
                f"institution {institution.institution_id} has a duplicate "
                "lineage reference"
            )
        for target in targets:
            if target == institution.institution_id:
                raise SnapshotIntegrityError(
                    f"institution {institution.institution_id} has a self "
                    "lineage reference"
                )
            if target not in institution_ids:
                raise SnapshotIntegrityError(
                    f"institution {institution.institution_id} has unknown "
                    f"lineage target {target}"
                )
        if institution.merged_into is not None:
            graph[institution.institution_id].add(institution.merged_into)
        for predecessor in institution.supersedes:
            graph[predecessor].add(institution.institution_id)

    incoming = dict.fromkeys(institution_ids, 0)
    for outgoing_targets in graph.values():
        for target in outgoing_targets:
            incoming[target] += 1
    ready = deque(
        institution_id
        for institution_id, count in incoming.items()
        if count == 0
    )
    visited = 0
    while ready:
        institution_id = ready.popleft()
        visited += 1
        for target in graph[institution_id]:
            incoming[target] -= 1
            if incoming[target] == 0:
                ready.append(target)
    if visited != len(institution_ids):
        raise SnapshotIntegrityError("institution lineage cycle detected")


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
    for source in manifest.sources:
        if source.normalized_row_count + source.preserved_row_count != source.row_count:
            raise SnapshotIntegrityError(
                f"source {source.source} normalized/preserved row counts "
                "do not match rowCount"
            )
    for source in manifest.sources:
        current = [
            item
            for item in institutions
            if item.source == source.source
            and item.status is not InstitutionStatus.MISSING_FROM_SOURCE
        ]
        preserved = [
            item
            for item in institutions
            if item.source == source.source
            and item.status is InstitutionStatus.MISSING_FROM_SOURCE
        ]
        _assert_observation_histogram(
            source.normalized_observation_date_counts,
            current,
            source=source.source,
            label="normalized",
        )
        _assert_observation_histogram(
            source.preserved_observation_date_counts,
            preserved,
            source=source.source,
            label="preserved",
        )
        unclassified_rows = [
            institution
            for institution in institutions
            if institution.source == source.source
            and institution.institution_type == "UNCLASSIFIED_SCHOOL"
        ]
        if source.source != "NEIS":
            if (
                source.unclassified_school_kind_counts
                or source.unclassified_school_policy_sha256 is not None
            ):
                raise SnapshotIntegrityError(
                    "unclassified provenance is reserved for NEIS"
                )
            continue
        if (
            sum(source.unclassified_school_kind_counts.values())
            != len(unclassified_rows)
            or not _matches_pinned_unclassified_policy(
                source.unclassified_school_kind_counts,
                source.unclassified_school_policy_sha256,
            )
        ):
            raise SnapshotIntegrityError(
                "unclassified provenance does not match reviewed policy"
            )
    if sum(declared.values()) != len(institutions):
        raise SnapshotIntegrityError(
            "source rowCount sum does not match institutionCount"
        )


def _verify_school_count_reconciliation(
    manifest: SnapshotManifest,
    institutions: tuple[Institution, ...],
) -> None:
    reconciliation = manifest.school_count_reconciliation
    if reconciliation is None:
        return
    manifest_sources = {source.source for source in manifest.sources}
    if not manifest_sources & {"NEIS", "KINDERGARTEN_INFO"}:
        return
    expected_type_counts = {
        "KINDERGARTEN_INFO": {"KINDERGARTEN": 706},
        "NEIS": {
            "ELEMENTARY_SCHOOL": 610,
            "HIGH_SCHOOL": 324,
            "MIDDLE_SCHOOL": 391,
            "MISC_SCHOOL": 39,
            "SPECIAL_SCHOOL": 32,
            "UNCLASSIFIED_SCHOOL": 18,
        },
    }
    for source_name, expected in expected_type_counts.items():
        current = [
            institution
            for institution in institutions
            if institution.source == source_name
            and institution.status is not InstitutionStatus.MISSING_FROM_SOURCE
        ]
        actual = dict(
            sorted(Counter(row.institution_type for row in current).items())
        )
        if actual != expected:
            raise SnapshotIntegrityError(
                "persisted normalized population does not match reconciliation"
            )
    quarantined = [
        institution
        for institution in institutions
        if institution.source == "NEIS"
        and institution.status is not InstitutionStatus.MISSING_FROM_SOURCE
        and institution.institution_type == "UNCLASSIFIED_SCHOOL"
    ]
    if len(quarantined) != 18 or any(
        institution.status is not InstitutionStatus.REVIEW_REQUIRED
        or institution.status_source != "OFFICIAL_CLASSIFICATION_PENDING"
        for institution in quarantined
    ):
        raise SnapshotIntegrityError(
            "persisted population status does not match reconciliation"
        )


def _matches_pinned_unclassified_policy(
    counts: dict[str, int],
    sha256: str | None,
) -> bool:
    if sha256 != PINNED_POLICY_SHA256:
        return False
    try:
        NeisUnclassifiedPolicy(
            counts=tuple(counts.items()),
            sha256=sha256,
            reviewed_as_of="2026-08-13",
            reviewer_role="data-steward",
        )
    except ValueError:
        return False
    return True


def _assert_observation_histogram(
    declared: dict[str, int],
    rows: list[Institution],
    *,
    source: str,
    label: str,
) -> None:
    actual = dict(sorted(Counter(item.source_as_of for item in rows).items()))
    if declared != actual:
        raise SnapshotIntegrityError(
            f"source {source} {label} observation date counts do not match "
            "institution records"
        )


def _is_effective_on(
    effective_from: str,
    effective_to: str | None,
    on_date: str,
) -> bool:
    selected = date.fromisoformat(on_date)
    return date.fromisoformat(effective_from) <= selected and (
        effective_to is None or selected <= date.fromisoformat(effective_to)
    )
