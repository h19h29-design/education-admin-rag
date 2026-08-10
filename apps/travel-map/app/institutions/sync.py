import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.institutions.models import (
    Institution,
    InstitutionSite,
    InstitutionStatus,
    SnapshotManifest,
)
from app.institutions.snapshot import VerifiedSnapshot, verify_snapshot
from app.institutions.sources.common import (
    EnrichmentProvenance,
    SourceInstitutionRecord,
    SourceProvenance,
    normalized_records_sha256,
)
from app.policy.coverage import CoverageService
from app.policy.models import CoverageState
from app.providers.kakao_local import KakaoLocalClient
from app.routing.models import Coordinate

_SAFE_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_EXPECTED_REGION_CODES = {
    "NEIS": "B10",
    "KINDERGARTEN_INFO": "11",
    "SEN_REVIEWED_CSV": "SEOUL",
}
_EXPECTED_ID_PREFIXES = {
    "NEIS": "neis:B10:",
    "KINDERGARTEN_INFO": "kinder:",
    "SEN_REVIEWED_CSV": "sen:",
}
_ALLOWED_TYPES_BY_SOURCE = {
    "NEIS": {
        "ELEMENTARY_SCHOOL",
        "MIDDLE_SCHOOL",
        "HIGH_SCHOOL",
        "SPECIAL_SCHOOL",
        "MISC_SCHOOL",
    },
    "KINDERGARTEN_INFO": {"KINDERGARTEN"},
    "SEN_REVIEWED_CSV": {
        "HEADQUARTERS",
        "DISTRICT_OFFICE",
        "DIRECT_AGENCY",
        "LIBRARY",
        "LIFELONG_LEARNING_CENTER",
    },
}
_ALLOWED_FOUNDATION_TYPES = {"NATIONAL", "PUBLIC", "PRIVATE"}
_ALLOWED_COORDINATE_QUALITIES = {
    "MISSING",
    "SOURCE_COORDINATE",
    "OFFICIAL_STANDARD_COORDINATE",
    "GEOCODED",
    "MANUALLY_VERIFIED",
}
_SOURCE_ENDPOINTS = {
    "NEIS": "https://open.neis.go.kr/hub/schoolInfo",
    "KINDERGARTEN_INFO": (
        "https://e-childschoolinfo.moe.go.kr/api/notice/basicInfo2.do"
    ),
    "SEN_REVIEWED_CSV": "https://www.sen.go.kr/www/website.jsp",
}
_SOURCE_LICENSES = {
    "NEIS": "PUBLIC_DATA_NO_USE_RESTRICTION",
    "KINDERGARTEN_INFO": "ATTRIBUTION_COMMERCIAL_AND_MODIFICATION_ALLOWED",
    "SEN_REVIEWED_CSV": "KOGL_TYPE_1_ATTRIBUTION",
}
_SOURCE_ATTRIBUTIONS = {
    "NEIS": "Ministry of Education NEIS education data",
    "KINDERGARTEN_INFO": "Ministry of Education Kindergarten Info",
    "SEN_REVIEWED_CSV": "Seoul Metropolitan Office of Education",
}
_SnapshotModel = TypeVar("_SnapshotModel", bound=BaseModel)


class SnapshotQualityError(ValueError):
    """Raised when a candidate snapshot fails a promotion gate."""


@dataclass(frozen=True)
class SnapshotBuildResult:
    snapshot_id: str
    candidate_path: Path
    approved: bool
    issues: tuple[str, ...]


async def geocode_missing_records(
    records: tuple[SourceInstitutionRecord, ...],
    client: KakaoLocalClient,
) -> tuple[SourceInstitutionRecord, ...]:
    geocoded: list[SourceInstitutionRecord] = []
    for record in records:
        if (record.latitude is None) != (record.longitude is None):
            raise SnapshotQualityError("source coordinate pair is incomplete")
        if record.latitude is not None:
            geocoded.append(record)
            continue
        result = await client.geocode(record.road_address)
        if result is None:
            geocoded.append(record)
            continue
        geocoded.append(
            replace(
                record,
                road_address=result.road_address,
                latitude=result.latitude,
                longitude=result.longitude,
                coordinate_quality="GEOCODED",
            )
        )
    return tuple(geocoded)


def build_candidate_snapshot(
    *,
    records: tuple[SourceInstitutionRecord, ...],
    previous: VerifiedSnapshot | None,
    output_root: Path,
    snapshot_id: str,
    coverage: CoverageService | None = None,
    source_provenance: Mapping[str, SourceProvenance] | None = None,
    enrichment_provenance: tuple[EnrichmentProvenance, ...] = (),
) -> SnapshotBuildResult:
    if _SAFE_SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise SnapshotQualityError("snapshot ID is unsafe")
    if coverage is None:
        raise SnapshotQualityError(
            "CoverageService is required for Seoul coordinate validation"
        )
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    candidate_path = root / f".{snapshot_id}.candidate"
    final_path = root / snapshot_id
    if candidate_path.exists() or final_path.exists():
        raise SnapshotQualityError("snapshot ID already exists")

    duplicate_ids = _duplicate_ids(record.institution_id for record in records)
    if duplicate_ids:
        raise SnapshotQualityError("duplicate source ID")
    issues: list[str] = []
    for record in records:
        _validate_source_record(record)
    if source_provenance is not None:
        expected_sources = {record.source for record in records}
        if set(source_provenance) != expected_sources:
            raise SnapshotQualityError("source provenance does not match record sources")
        if any(
            key != provenance.source
            for key, provenance in source_provenance.items()
        ):
            raise SnapshotQualityError("source provenance name mismatch")

    institutions, sites = _build_current_records(records, snapshot_id, coverage)
    current_coordinate_rate = (
        sum(site.status is InstitutionStatus.ACTIVE for site in sites)
        / len(records)
        if records
        else 0.0
    )
    if previous is not None:
        institutions, sites = _preserve_missing_records(
            institutions,
            sites,
            previous,
            snapshot_id,
        )
        previous_active = sum(
            item.status is InstitutionStatus.ACTIVE
            for item in previous.institutions
        )
        current_active = sum(
            item.status is InstitutionStatus.ACTIVE for item in institutions
        )
        if previous_active and current_active < previous_active * 0.9:
            issues.append("record count drop exceeds 10 percent")

    if current_coordinate_rate < 0.98:
        issues.append("coordinate validation success rate is below 98 percent")

    candidate_path.mkdir()
    institution_bytes = _jsonl_bytes(institutions)
    site_bytes = _jsonl_bytes(sites)
    (candidate_path / "institutions.jsonl").write_bytes(institution_bytes)
    (candidate_path / "sites.jsonl").write_bytes(site_bytes)
    now = _utc_now()
    snapshot_as_of = max(
        (
            [item.source_as_of for item in institutions]
            + [item.source_as_of for item in enrichment_provenance]
        ),
        default=now[:10],
    )
    possible_match_count = _possible_match_count(records)
    manifest = _candidate_manifest(
        snapshot_id=snapshot_id,
        created_at=now,
        snapshot_as_of=snapshot_as_of,
        institutions=institutions,
        sites=sites,
        institution_bytes=institution_bytes,
        site_bytes=site_bytes,
        possible_match_count=possible_match_count,
        previous=previous,
        source_provenance=source_provenance,
        source_records=records,
        enrichment_provenance=enrichment_provenance,
    )
    _write_json(candidate_path / "manifest.json", manifest)
    return SnapshotBuildResult(
        snapshot_id=snapshot_id,
        candidate_path=candidate_path,
        approved=False,
        issues=tuple(issues),
    )


def promote_snapshot(candidate: SnapshotBuildResult, output_root: Path) -> None:
    if candidate.issues:
        raise SnapshotQualityError("; ".join(candidate.issues))
    root = Path(output_root)
    candidate_path = candidate.candidate_path
    final_path = root / candidate.snapshot_id
    selected_path = candidate_path if candidate_path.exists() else final_path
    if not selected_path.is_dir():
        raise SnapshotQualityError("candidate snapshot is missing")
    manifest_path = selected_path / "manifest.json"
    manifest = _read_json_object(manifest_path)
    if selected_path == candidate_path and manifest.get("approved") is not False:
        raise SnapshotQualityError("candidate manifest must remain approved=false")
    if selected_path == final_path and manifest.get("approved") not in (False, True):
        raise SnapshotQualityError("recoverable final manifest approval is invalid")
    institutions, sites = _recheck_candidate(
        selected_path,
        manifest,
        candidate.snapshot_id,
    )
    _recheck_promotion_quality(root, manifest, institutions, sites)
    if selected_path == candidate_path:
        os.replace(candidate_path, final_path)
        selected_path = final_path
        manifest_path = final_path / "manifest.json"
    if manifest.get("approved") is False:
        manifest["approved"] = True
        manifest["approvedAt"] = _utc_now()
        manifest["approvedByRole"] = "data-steward"
        temporary_manifest = selected_path / ".manifest.json.tmp"
        _write_json(temporary_manifest, manifest, durable=True)
        os.replace(temporary_manifest, manifest_path)

    temporary_pointer = root / ".current.json.tmp"
    _write_json(
        temporary_pointer,
        {"snapshotId": candidate.snapshot_id},
        durable=True,
    )
    os.replace(temporary_pointer, root / "current.json")


def _build_current_records(
    records: tuple[SourceInstitutionRecord, ...],
    snapshot_id: str,
    coverage: CoverageService,
) -> tuple[list[Institution], list[InstitutionSite]]:
    institutions: list[Institution] = []
    sites: list[InstitutionSite] = []
    for record in sorted(records, key=lambda item: item.institution_id):
        has_coordinates = record.latitude is not None and record.longitude is not None
        is_seoul_address = _is_seoul_address(record.road_address)
        is_seoul_coordinate = (
            _is_seoul_coordinate(record, coverage) if has_coordinates else False
        )
        status = (
            InstitutionStatus.ACTIVE
            if has_coordinates and is_seoul_address and is_seoul_coordinate
            else InstitutionStatus.REVIEW_REQUIRED
        )
        institutions.append(
            Institution(
                institution_id=record.institution_id,
                official_name=record.official_name,
                institution_type=record.institution_type,
                foundation_type=record.foundation_type,
                education_office=record.education_office,
                status=status,
                status_source=record.source,
                effective_from=record.source_as_of,
                effective_to=None,
                last_seen_snapshot=snapshot_id,
                aliases=(),
                supersedes=(),
                merged_into=None,
                source=record.source,
                source_region_code=record.source_region_code,
                source_as_of=record.source_as_of,
            )
        )
        if has_coordinates:
            assert record.latitude is not None
            assert record.longitude is not None
            site_name = "main"
            sites.append(
                InstitutionSite(
                    site_id=f"{record.institution_id}:main",
                    institution_id=record.institution_id,
                    site_name=site_name,
                    road_address=record.road_address,
                    district=record.district,
                    latitude=record.latitude,
                    longitude=record.longitude,
                    coordinate_quality=record.coordinate_quality,
                    routing_anchor_latitude=record.latitude,
                    routing_anchor_longitude=record.longitude,
                    is_default=True,
                    status=status,
                    effective_from=record.source_as_of,
                    effective_to=None,
                )
            )
    return institutions, sites


def _is_seoul_address(address: str) -> bool:
    normalized = " ".join(address.split())
    return normalized.startswith(("\uc11c\uc6b8\ud2b9\ubcc4\uc2dc ", "\uc11c\uc6b8\uc2dc "))


def _validate_source_record(record: SourceInstitutionRecord) -> None:
    expected_region = _EXPECTED_REGION_CODES.get(record.source)
    if expected_region is None or record.source_region_code != expected_region:
        raise SnapshotQualityError("source region code mismatch")
    expected_prefix = _EXPECTED_ID_PREFIXES[record.source]
    if not record.institution_id.startswith(expected_prefix):
        raise SnapshotQualityError("source identifier namespace mismatch")
    if record.institution_type not in _ALLOWED_TYPES_BY_SOURCE[record.source]:
        raise SnapshotQualityError("unsupported institution type")
    if record.foundation_type not in _ALLOWED_FOUNDATION_TYPES:
        raise SnapshotQualityError("unsupported foundation type")
    if record.coordinate_quality not in _ALLOWED_COORDINATE_QUALITIES:
        raise SnapshotQualityError("unsupported coordinate quality")
    has_latitude = record.latitude is not None
    has_longitude = record.longitude is not None
    if has_latitude != has_longitude:
        raise SnapshotQualityError("source coordinate pair is incomplete")
    if has_latitude == (record.coordinate_quality == "MISSING"):
        raise SnapshotQualityError("source coordinate quality does not match coordinates")


def _is_seoul_coordinate(
    record: SourceInstitutionRecord,
    coverage: CoverageService,
) -> bool:
    assert record.latitude is not None
    assert record.longitude is not None
    return (
        coverage.classify(
            Coordinate(
                latitude=record.latitude,
                longitude=record.longitude,
            )
        )
        is CoverageState.SEOUL
    )


def _preserve_missing_records(
    institutions: list[Institution],
    sites: list[InstitutionSite],
    previous: VerifiedSnapshot,
    snapshot_id: str,
) -> tuple[list[Institution], list[InstitutionSite]]:
    current_ids = {item.institution_id for item in institutions}
    source_dates = {
        item.source: item.source_as_of for item in institutions
    }
    for old in previous.institutions:
        if old.institution_id in current_ids:
            continue
        source_as_of = source_dates.get(old.source, old.source_as_of)
        institutions.append(
            old.model_copy(
                update={
                    "status": InstitutionStatus.MISSING_FROM_SOURCE,
                    "status_source": "MISSING_FROM_SOURCE_GATE",
                    "last_seen_snapshot": snapshot_id,
                    "source_as_of": source_as_of,
                }
            )
        )
        for old_site in previous.sites:
            if old_site.institution_id == old.institution_id:
                sites.append(
                    old_site.model_copy(
                        update={"status": InstitutionStatus.MISSING_FROM_SOURCE}
                    )
                )
    institutions.sort(key=lambda item: item.institution_id)
    sites.sort(key=lambda item: item.site_id)
    return institutions, sites


def _candidate_manifest(
    *,
    snapshot_id: str,
    created_at: str,
    snapshot_as_of: str,
    institutions: list[Institution],
    sites: list[InstitutionSite],
    institution_bytes: bytes,
    site_bytes: bytes,
    possible_match_count: int,
    previous: VerifiedSnapshot | None,
    source_provenance: Mapping[str, SourceProvenance] | None,
    source_records: tuple[SourceInstitutionRecord, ...],
    enrichment_provenance: tuple[EnrichmentProvenance, ...],
) -> dict[str, object]:
    by_source: dict[str, list[Institution]] = defaultdict(list)
    for institution in institutions:
        by_source[institution.source].append(institution)
    current_by_source: dict[str, list[SourceInstitutionRecord]] = defaultdict(list)
    for record in source_records:
        current_by_source[record.source].append(record)
    sources = []
    for source_name, source_rows in sorted(by_source.items()):
        source_as_of = max(row.source_as_of for row in source_rows)
        raw = json.dumps(
            [row.model_dump(by_alias=True, mode="json") for row in source_rows],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        provenance = (
            source_provenance.get(source_name)
            if source_provenance is not None
            else None
        )
        sources.append(
            {
                "source": source_name,
                "endpoint": (
                    provenance.endpoint
                    if provenance is not None
                    else _SOURCE_ENDPOINTS[source_name]
                ),
                "licenseName": (
                    provenance.license_name
                    if provenance is not None
                    else _SOURCE_LICENSES[source_name]
                ),
                "attribution": (
                    provenance.attribution
                    if provenance is not None
                    else _SOURCE_ATTRIBUTIONS[source_name]
                ),
                "fetchedAt": (
                    provenance.fetched_at
                    if provenance is not None
                    else created_at
                ),
                "sourceAsOf": source_as_of,
                "rawSha256": (
                    provenance.raw_sha256
                    if provenance is not None
                    else hashlib.sha256(raw).hexdigest()
                ),
                "normalizedSha256": (
                    provenance.normalized_sha256
                    if provenance is not None
                    and provenance.normalized_sha256 is not None
                    else normalized_records_sha256(current_by_source[source_name])
                ),
                "requestRegionCode": (
                    provenance.request_region_code
                    if provenance is not None
                    and provenance.request_region_code is not None
                    else _EXPECTED_REGION_CODES[source_name]
                ),
                "requestTiming": (
                    provenance.request_timing
                    if provenance is not None
                    else None
                ),
                "pageCount": (
                    provenance.page_count if provenance is not None else 1
                ),
                "fetchedRowCount": (
                    provenance.fetched_row_count
                    if provenance is not None
                    and provenance.fetched_row_count is not None
                    else len(current_by_source[source_name])
                ),
                "normalizedRowCount": len(current_by_source[source_name]),
                "preservedRowCount": (
                    len(source_rows) - len(current_by_source[source_name])
                ),
                "rowCount": len(source_rows),
            }
        )
    previous_ids = (
        {item.institution_id for item in previous.institutions}
        if previous is not None
        else set()
    )
    current_ids = {item.institution_id for item in institutions}
    previous_by_id = (
        {item.institution_id: item for item in previous.institutions}
        if previous is not None
        else {}
    )
    current_by_id = {item.institution_id: item for item in institutions}
    current_source_ids = {record.institution_id for record in source_records}
    changed_count = sum(
        _institution_change_key(current_by_id[institution_id])
        != _institution_change_key(previous_by_id[institution_id])
        for institution_id in current_source_ids & previous_ids
    )
    return {
        "schemaVersion": 1,
        "snapshotId": snapshot_id,
        "createdAt": created_at,
        "snapshotAsOf": snapshot_as_of,
        "approved": False,
        "approvedAt": None,
        "approvedByRole": None,
        "sources": sources,
        "enrichments": [
            {
                "source": item.source,
                "endpoint": item.endpoint,
                "licenseName": item.license_name,
                "attribution": item.attribution,
                "fetchedAt": item.fetched_at,
                "sourceAsOf": item.source_as_of,
                "rawSha256": item.raw_sha256,
                "normalizedSha256": item.normalized_sha256,
                "requestRegionCode": item.request_region_code,
                "requestTiming": item.request_timing,
                "pageCount": item.page_count,
                "fetchedRowCount": item.fetched_row_count,
                "matchedRowCount": item.matched_row_count,
            }
            for item in enrichment_provenance
        ],
        "institutionsSha256": hashlib.sha256(institution_bytes).hexdigest(),
        "sitesSha256": hashlib.sha256(site_bytes).hexdigest(),
        "institutionCount": len(institutions),
        "siteCount": len(sites),
        "quarantinedCount": sum(
            item.status is InstitutionStatus.REVIEW_REQUIRED
            for item in institutions
        ),
        "possibleMatchCount": possible_match_count,
        "countsByType": dict(Counter(item.institution_type for item in institutions)),
        "countsByFoundation": dict(
            Counter(item.foundation_type for item in institutions)
        ),
        "countsByStatus": dict(Counter(item.status.value for item in institutions)),
        "coordinateQualityCounts": dict(
            Counter(item.coordinate_quality for item in sites)
        ),
        "diff": {
            "previousSnapshotId": (
                previous.manifest.snapshot_id if previous is not None else None
            ),
            "addedCount": len(current_ids - previous_ids),
            "changedCount": changed_count,
            "missingCount": sum(
                item.status is InstitutionStatus.MISSING_FROM_SOURCE
                for item in institutions
            ),
            "closedCandidateCount": 0,
        },
    }


def _jsonl_bytes(models: list[Institution] | list[InstitutionSite]) -> bytes:
    lines = [
        item.model_dump_json(by_alias=True)
        for item in models
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _possible_match_count(records: tuple[SourceInstitutionRecord, ...]) -> int:
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in records:
        key = ("".join(record.official_name.split()), "".join(record.road_address.split()))
        grouped[key].add(record.source)
    return sum(len(sources) - 1 for sources in grouped.values() if len(sources) > 1)


def _institution_change_key(institution: Institution) -> tuple[object, ...]:
    return (
        institution.official_name,
        institution.institution_type,
        institution.foundation_type,
        institution.education_office,
        institution.status,
        institution.effective_to,
        institution.aliases,
        institution.supersedes,
        institution.merged_into,
        institution.source,
        institution.source_region_code,
    )


def _duplicate_ids(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise SnapshotQualityError("candidate manifest is not an object")
    return value


def _recheck_candidate(
    path: Path,
    manifest: dict[str, object],
    snapshot_id: str,
) -> tuple[list[Institution], list[InstitutionSite]]:
    if manifest.get("snapshotId") != snapshot_id:
        raise SnapshotQualityError("candidate snapshot ID mismatch")
    institution_bytes = (path / "institutions.jsonl").read_bytes()
    site_bytes = (path / "sites.jsonl").read_bytes()
    if hashlib.sha256(institution_bytes).hexdigest() != manifest.get(
        "institutionsSha256"
    ):
        raise SnapshotQualityError("candidate institution hash mismatch")
    if hashlib.sha256(site_bytes).hexdigest() != manifest.get("sitesSha256"):
        raise SnapshotQualityError("candidate site hash mismatch")
    institutions = _parse_candidate_jsonl(
        institution_bytes,
        Institution,
        "institutions.jsonl",
    )
    sites = _parse_candidate_jsonl(site_bytes, InstitutionSite, "sites.jsonl")
    _recheck_manifest_counts(manifest, institutions, sites)
    _validate_candidate_manifest_schema(manifest)
    return institutions, sites


def _parse_candidate_jsonl(
    data: bytes,
    model: type[_SnapshotModel],
    label: str,
) -> list[_SnapshotModel]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotQualityError(f"candidate {label} is not UTF-8") from exc
    lines = text.splitlines()
    if not lines:
        raise SnapshotQualityError(f"candidate {label} is empty")
    records: list[_SnapshotModel] = []
    for line in lines:
        try:
            records.append(model.model_validate_json(line))
        except ValidationError as exc:
            raise SnapshotQualityError(f"candidate {label} is invalid") from exc
    return records


def _recheck_manifest_counts(
    manifest: dict[str, object],
    institutions: list[Institution],
    sites: list[InstitutionSite],
) -> None:
    expected: dict[str, object] = {
        "institutionCount": len(institutions),
        "siteCount": len(sites),
        "quarantinedCount": sum(
            institution.status is InstitutionStatus.REVIEW_REQUIRED
            for institution in institutions
        ),
        "countsByType": dict(
            Counter(institution.institution_type for institution in institutions)
        ),
        "countsByFoundation": dict(
            Counter(institution.foundation_type for institution in institutions)
        ),
        "countsByStatus": dict(
            Counter(institution.status.value for institution in institutions)
        ),
        "coordinateQualityCounts": dict(
            Counter(site.coordinate_quality for site in sites)
        ),
    }
    for name, value in expected.items():
        if manifest.get(name) != value:
            raise SnapshotQualityError(f"candidate {name} does not match records")
    if len({record.institution_id for record in institutions}) != len(institutions):
        raise SnapshotQualityError("candidate has duplicate institutionId")
    if len({record.site_id for record in sites}) != len(sites):
        raise SnapshotQualityError("candidate has duplicate siteId")
    institution_ids = {record.institution_id for record in institutions}
    if any(site.institution_id not in institution_ids for site in sites):
        raise SnapshotQualityError("candidate site has unknown institutionId")
    sources = manifest.get("sources")
    if type(sources) is not list:
        raise SnapshotQualityError("candidate sources must be a list")
    declared: dict[str, int] = {}
    for source in sources:
        if type(source) is not dict:
            raise SnapshotQualityError("candidate source metadata is invalid")
        source_name = source.get("source")
        row_count = source.get("rowCount")
        if (
            type(source_name) is not str
            or type(row_count) is not int
            or source_name in declared
        ):
            raise SnapshotQualityError("candidate source metadata is invalid")
        declared[source_name] = row_count
    actual = Counter(institution.source for institution in institutions)
    if declared != dict(actual):
        raise SnapshotQualityError("candidate source rowCount does not match records")


def _validate_candidate_manifest_schema(manifest: dict[str, object]) -> None:
    approved = dict(manifest)
    approved["approved"] = True
    approved["approvedAt"] = approved.get("createdAt")
    approved["approvedByRole"] = "data-steward"
    try:
        SnapshotManifest.model_validate_json(
            json.dumps(approved, ensure_ascii=False, separators=(",", ":"))
        )
    except ValidationError as exc:
        raise SnapshotQualityError("candidate manifest schema is invalid") from exc


def _recheck_promotion_quality(
    root: Path,
    manifest: dict[str, object],
    institutions: list[Institution],
    sites: list[InstitutionSite],
) -> None:
    for institution in institutions:
        _validate_persisted_institution(institution)
    if any(
        site.coordinate_quality not in _ALLOWED_COORDINATE_QUALITIES
        for site in sites
    ):
        raise SnapshotQualityError("unsupported coordinate quality")
    current = [
        institution
        for institution in institutions
        if institution.status is not InstitutionStatus.MISSING_FROM_SOURCE
    ]
    active = [
        institution
        for institution in current
        if institution.status is InstitutionStatus.ACTIVE
    ]
    active_site_parents = {
        site.institution_id
        for site in sites
        if site.status is InstitutionStatus.ACTIVE
    }
    if any(
        institution.institution_id not in active_site_parents
        for institution in active
    ):
        raise SnapshotQualityError("active candidate institution has no active site")
    coordinate_rate = len(active) / len(current) if current else 0.0
    if coordinate_rate < 0.98:
        raise SnapshotQualityError(
            "coordinate validation success rate is below 98 percent"
        )

    diff = manifest.get("diff")
    if type(diff) is not dict:
        raise SnapshotQualityError("candidate diff metadata is invalid")
    previous_snapshot_id = diff.get("previousSnapshotId")
    if previous_snapshot_id is None:
        if (root / "current.json").exists():
            raise SnapshotQualityError(
                "candidate previous snapshot ID is missing"
            )
        return
    if type(previous_snapshot_id) is not str:
        raise SnapshotQualityError("candidate previous snapshot ID is invalid")
    try:
        previous = verify_snapshot(root)
    except (OSError, ValueError) as exc:
        raise SnapshotQualityError(
            "candidate previous snapshot cannot be verified"
        ) from exc
    if previous.manifest.snapshot_id != previous_snapshot_id:
        raise SnapshotQualityError("candidate previous snapshot ID mismatch")
    previous_active = sum(
        institution.status is InstitutionStatus.ACTIVE
        for institution in previous.institutions
    )
    if previous_active and len(active) < previous_active * 0.9:
        raise SnapshotQualityError("record count drop exceeds 10 percent")


def _validate_persisted_institution(institution: Institution) -> None:
    expected_region = _EXPECTED_REGION_CODES.get(institution.source)
    expected_prefix = _EXPECTED_ID_PREFIXES.get(institution.source)
    allowed_types = _ALLOWED_TYPES_BY_SOURCE.get(institution.source)
    if expected_region is None or institution.source_region_code != expected_region:
        raise SnapshotQualityError("source region code mismatch")
    if expected_prefix is None or not institution.institution_id.startswith(
        expected_prefix
    ):
        raise SnapshotQualityError("source identifier namespace mismatch")
    if allowed_types is None or institution.institution_type not in allowed_types:
        raise SnapshotQualityError("unsupported institution type")
    if institution.foundation_type not in _ALLOWED_FOUNDATION_TYPES:
        raise SnapshotQualityError("unsupported foundation type")


def _write_json(path: Path, value: object, *, durable: bool = False) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    with path.open("w", encoding="utf-8") as stream:
        stream.write(payload)
        if durable:
            stream.flush()
            os.fsync(stream.fileno())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
