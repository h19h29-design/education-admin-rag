import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar, cast

from pydantic import BaseModel, ValidationError

from app.institutions.models import (
    Institution,
    InstitutionSite,
    InstitutionStatus,
    SnapshotManifest,
)
from app.institutions.snapshot import (
    SnapshotIntegrityError,
    VerifiedSnapshot,
    _verify_manifest_fields,
    verify_snapshot,
    verify_snapshot_directory,
)
from app.institutions.sources.common import (
    EnrichmentProvenance,
    SourceDataError,
    SourceInstitutionRecord,
    SourceInstitutionSiteRecord,
    SourceProvenance,
    normalized_records_sha256,
    observation_counts_as_dict,
    observation_date_counts,
    source_as_of_for,
    validate_observation_date_counts,
)
from app.institutions.sources.neis_classification import (
    PINNED_POLICY_SHA256,
    NeisUnclassifiedPolicy,
)
from app.institutions.sources.sen_counts import ReviewedSchoolCounts
from app.institutions.sources.standard_school import (
    DOWNLOAD_URL as STANDARD_LOCATION_ENDPOINT,
)
from app.institutions.sources.standard_school import (
    PINNED_NATIONWIDE_COUNT,
)
from app.institutions.sources.standard_school import (
    PINNED_SHA256 as STANDARD_LOCATION_RAW_SHA256,
)
from app.institutions.sources.standard_school import (
    PINNED_SOURCE_AS_OF as STANDARD_LOCATION_SOURCE_AS_OF,
)
from app.policy.coverage import CoverageService
from app.policy.models import CoverageState
from app.providers.kakao_local import KakaoLocalClient
from app.routing.models import Coordinate

_SAFE_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_SITE_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
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
        "UNCLASSIFIED_SCHOOL",
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
_SEOUL_DISTRICTS = (
    "강남구",
    "강동구",
    "강북구",
    "강서구",
    "관악구",
    "광진구",
    "구로구",
    "금천구",
    "노원구",
    "도봉구",
    "동대문구",
    "동작구",
    "마포구",
    "서대문구",
    "서초구",
    "성동구",
    "성북구",
    "송파구",
    "양천구",
    "영등포구",
    "용산구",
    "은평구",
    "종로구",
    "중구",
    "중랑구",
)
_SOURCE_ENDPOINTS = {
    "NEIS": "https://open.neis.go.kr/hub/schoolInfo",
    "KINDERGARTEN_INFO": (
        "https://e-childschoolinfo.moe.go.kr/api/notice/basicInfo2.do"
    ),
    "SEN_REVIEWED_CSV": "https://www.sen.go.kr/www/website.jsp",
}
_SOURCE_LICENSES = {
    "NEIS": "PUBLIC_DATA_NO_USE_RESTRICTION",
    "KINDERGARTEN_INFO": "PUBLIC_DATA_PORTAL_TERMS",
    "SEN_REVIEWED_CSV": "KOGL_TYPE_1_ATTRIBUTION",
}
_SOURCE_ATTRIBUTIONS = {
    "NEIS": "Ministry of Education NEIS education data",
    "KINDERGARTEN_INFO": "Ministry of Education Kindergarten Info",
    "SEN_REVIEWED_CSV": (
        "Source: Seoul Metropolitan Office of Education "
        "(organization directory and 2026 civil-service handbook)"
    ),
}
_PINNED_SOURCE_RAW_SHA256 = {
    "SEN_REVIEWED_CSV": (
        "69863ac78689fb4b6e9941aabea03c3c1d618ccb26568e844079afd9092eb2c2"
    ),
}
_PINNED_SOURCE_NORMALIZED_SHA256 = {
    "SEN_REVIEWED_CSV": (
        "8cd2aa66f3df95a25a2127eaa2791e876f2d21cd7bc47aa700d34be75293b3b3"
    ),
}
_STANDARD_LOCATION_NORMALIZED_SHA256 = (
    "ebb2643be10bda983ca9cb81a7ce2820474a53c2f65fc3ac6a7bcc179527cb4a"
)
_KAKAO_ENDPOINT = "https://dapi.kakao.com/v2/local/search/address.json"
_TRANSACTION_KEY_NAME = ".sync-attestation.key"
_TRANSACTION_DIRECTORY_NAME = ".sync-transactions"
_TRANSACTION_VERSION = 1
_TRANSACTION_PHASES = {
    "BUILT",
    "MOVED",
    "APPROVAL_PREPARED",
    "VERIFIED",
    "POINTER_PREPARED",
    "PUBLISHED",
}
_TRANSACTION_FIELDS = {
    "version",
    "snapshotId",
    "candidateName",
    "nonce",
    "phase",
    "issues",
    "manifestSha256",
    "sourcesSha256",
    "enrichmentsSha256",
    "institutionsSha256",
    "sitesSha256",
    "previousSnapshotId",
    "approvedManifestSha256",
    "approvedAt",
    "approvedByRole",
    "signature",
}
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
_SnapshotModel = TypeVar("_SnapshotModel", bound=BaseModel)


class SnapshotQualityError(ValueError):
    """Raised when a candidate snapshot fails a promotion gate."""


@dataclass(frozen=True)
class SnapshotBuildResult:
    snapshot_id: str
    candidate_path: Path
    approved: bool
    issues: tuple[str, ...]


def reconcile_selectable_school_counts(
    records: tuple[SourceInstitutionRecord, ...],
    *,
    benchmark: ReviewedSchoolCounts,
    unclassified_policy: NeisUnclassifiedPolicy | None = None,
    tolerance: float = 0.01,
) -> dict[str, object]:
    if not 0.0 <= tolerance <= 0.1:
        raise SnapshotQualityError("school reconciliation tolerance is invalid")
    if not benchmark.counts or any(
        type(expected) is not int or expected <= 0
        for expected in benchmark.counts.values()
    ):
        raise SnapshotQualityError("school reconciliation expected count is invalid")
    if (
        set(benchmark.counts) != set(benchmark.category_evidence)
        or set(benchmark.counts) != set(benchmark.category_composition)
    ):
        raise SnapshotQualityError("school reconciliation evidence is incomplete")
    actual_counts = Counter(record.institution_type for record in records)
    categories: dict[str, dict[str, object]] = {}
    for institution_type, expected_count in sorted(benchmark.counts.items()):
        expected_source = (
            "KINDERGARTEN_INFO"
            if institution_type == "KINDERGARTEN"
            else "NEIS"
        )
        matching_records = tuple(
            record
            for record in records
            if record.institution_type == institution_type
        )
        actual_count = actual_counts[institution_type]
        delta_count = abs(actual_count - expected_count)
        delta_ratio = delta_count / expected_count
        actual_sources = sorted({record.source for record in matching_records})
        actual_source_as_of = sorted(
            {record.source_as_of for record in matching_records}
        )
        actual_source_observation_date_counts = dict(
            sorted(
                Counter(
                    record.source_as_of for record in matching_records
                ).items()
            )
        )
        source_validation_passed = (
            actual_sources == [expected_source]
            and bool(actual_source_as_of)
        )
        evidence = benchmark.category_evidence[institution_type]
        categories[institution_type] = {
            "expectedCount": expected_count,
            "actualCount": actual_count,
            "deltaCount": delta_count,
            "deltaRatio": delta_ratio,
            "threshold": tolerance,
            "expectedSource": expected_source,
            "actualSources": actual_sources,
            "actualSourceAsOf": actual_source_as_of,
            "actualSourceObservationDateCounts": (
                actual_source_observation_date_counts
            ),
            "sourceValidationPassed": source_validation_passed,
            "sourceUrl": evidence.source_url,
            "sourceAsOf": evidence.source_as_of,
            "sourceSha256": evidence.source_sha256,
            "evidenceStatus": evidence.status,
            "composition": benchmark.category_composition[institution_type],
            "passed": delta_ratio <= tolerance and source_validation_passed,
        }
    reported_totals: list[dict[str, object]] = []
    for total in benchmark.reported_totals:
        population_types = total.population.split("+")
        if (
            total.used_for_gate
            or not population_types
            or any(name not in benchmark.counts for name in population_types)
        ):
            raise SnapshotQualityError(
                "school reconciliation reported total is invalid"
            )
        reported_totals.append(
            {
                "expectedCount": total.expected_count,
                "actualCount": sum(actual_counts[name] for name in population_types),
                "population": total.population,
                "usedForGate": False,
                "passed": None,
                "sourceUrl": total.evidence.source_url,
                "sourceAsOf": total.evidence.source_as_of,
                "sourceSha256": total.evidence.source_sha256,
                "evidenceStatus": total.evidence.status,
            }
        )
    unclassified_rows = [
        record
        for record in records
        if record.institution_type == "UNCLASSIFIED_SCHOOL"
    ]
    unclassified_school_kind_counts = dict(
        sorted(
            Counter(
                record.source_kind_label
                for record in unclassified_rows
                if record.source_kind_label is not None
            ).items()
        )
    )
    unclassified_policy_passed = (
        not unclassified_rows
        if unclassified_policy is None
        else (
            all(
                record.source == "NEIS" and record.source_kind_label is not None
                for record in unclassified_rows
            )
            and unclassified_school_kind_counts == dict(unclassified_policy.counts)
        )
    )
    result: dict[str, object] = {
        "normalizedSha256": benchmark.normalized_sha256,
        "threshold": tolerance,
        "categories": categories,
        "reportedTotals": reported_totals,
        "unclassifiedSchoolKindCounts": unclassified_school_kind_counts,
        "unclassifiedSchoolPolicySha256": (
            unclassified_policy.sha256
            if unclassified_policy is not None
            else None
        ),
        "unclassifiedPolicyPassed": unclassified_policy_passed,
        "passed": all(
            category["passed"] is True for category in categories.values()
        )
        and unclassified_policy_passed,
    }
    return result


def build_sync_preflight_audit(
    records: tuple[SourceInstitutionRecord, ...],
    *,
    source_provenance: Mapping[str, SourceProvenance],
    reconciliation: Mapping[str, object],
) -> dict[str, object]:
    source_record_counts = Counter(record.source for record in records)
    district_counts = {district: 0 for district in _SEOUL_DISTRICTS}
    quarantined_institution_ids: list[str] = []
    quarantined_site_ids: list[str] = []
    ready_institutions = 0
    for record in records:
        if record.district in district_counts:
            district_counts[record.district] += 1
        is_unclassified = record.institution_type == "UNCLASSIFIED_SCHOOL"
        if is_unclassified or record.latitude is None:
            quarantined_institution_ids.append(record.institution_id)
            quarantined_site_ids.append(f"{record.institution_id}:main")
        else:
            ready_institutions += 1
        for site in record.additional_sites:
            if is_unclassified or site.latitude is None:
                quarantined_site_ids.append(
                    f"{record.institution_id}:{site.site_code}"
                )
    source_counts = {
        source: {
            "fetched": provenance.fetched_row_count,
            "normalized": source_record_counts[source],
            "preserved": 0,
            "output": source_record_counts[source],
        }
        for source, provenance in sorted(source_provenance.items())
    }
    passed = reconciliation.get("passed") is True
    return {
        "auditStage": "PRE_PROMOTION_RECONCILIATION",
        "passed": passed,
        "sourceCounts": source_counts,
        "typeCounts": dict(
            sorted(Counter(record.institution_type for record in records).items())
        ),
        "foundationCounts": dict(
            sorted(Counter(record.foundation_type for record in records).items())
        ),
        "districtCounts": district_counts,
        "statusCounts": {
            "PRECHECK_READY_INSTITUTION": ready_institutions,
            "PRECHECK_REVIEW_REQUIRED_INSTITUTION": (
                len(records) - ready_institutions
            ),
        },
        "quarantinedInstitutionIds": sorted(quarantined_institution_ids),
        "quarantinedSiteIds": sorted(quarantined_site_ids),
        "reconciliation": dict(reconciliation),
    }


def emit_sync_preflight_audit(audit: Mapping[str, object]) -> None:
    print(json.dumps(dict(audit), ensure_ascii=False, sort_keys=True), flush=True)
    if audit.get("passed") is not True:
        raise SnapshotQualityError(
            "official school count reconciliation failed"
        )


async def geocode_missing_records(
    records: tuple[SourceInstitutionRecord, ...],
    client: KakaoLocalClient,
) -> tuple[SourceInstitutionRecord, ...]:
    geocoded: list[SourceInstitutionRecord] = []
    for record in records:
        if (record.latitude is None) != (record.longitude is None):
            raise SnapshotQualityError("source coordinate pair is incomplete")
        updated = record
        if record.latitude is None:
            result = await client.geocode(record.road_address)
            if result is not None:
                updated = replace(
                    record,
                    road_address=result.road_address,
                    latitude=result.latitude,
                    longitude=result.longitude,
                    coordinate_quality="GEOCODED",
                )
        additional_sites: list[SourceInstitutionSiteRecord] = []
        for site in updated.additional_sites:
            if (site.latitude is None) != (site.longitude is None):
                raise SnapshotQualityError("source site coordinate pair is incomplete")
            if site.latitude is not None:
                additional_sites.append(site)
                continue
            branch_result = await client.geocode(site.road_address)
            if branch_result is None:
                additional_sites.append(site)
                continue
            additional_sites.append(
                replace(
                    site,
                    road_address=branch_result.road_address,
                    latitude=branch_result.latitude,
                    longitude=branch_result.longitude,
                    coordinate_quality="GEOCODED",
                )
            )
        geocoded.append(
            replace(updated, additional_sites=tuple(additional_sites))
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
    root = _validated_snapshot_root(root)
    candidate_path = root / f".{snapshot_id}.candidate"
    final_path = root / snapshot_id
    if (
        candidate_path.is_symlink()
        or final_path.is_symlink()
        or candidate_path.exists()
        or final_path.exists()
    ):
        raise SnapshotQualityError("snapshot ID already exists")

    duplicate_ids = _duplicate_ids(record.institution_id for record in records)
    if duplicate_ids:
        raise SnapshotQualityError("duplicate source ID")
    issues: list[str] = []
    for record in records:
        _validate_source_record(record)
    if source_provenance is None:
        raise SnapshotQualityError("source provenance is required")
    expected_sources = {record.source for record in records}
    if set(source_provenance) != expected_sources:
        raise SnapshotQualityError("source provenance does not match record sources")
    if any(
        key != provenance.source
        for key, provenance in source_provenance.items()
    ):
        raise SnapshotQualityError("source provenance name mismatch")
    current_by_source: dict[str, list[SourceInstitutionRecord]] = defaultdict(list)
    for record in records:
        current_by_source[record.source].append(record)
    for source_name, provenance in source_provenance.items():
        _validate_source_provenance(
            source_name,
            provenance,
            current_by_source[source_name],
        )
    _validate_enrichment_provenance(records, enrichment_provenance)

    institutions, sites = _build_current_records(records, snapshot_id, coverage)
    selectable_institutions = [
        institution
        for institution in institutions
        if institution.institution_type != "UNCLASSIFIED_SCHOOL"
    ]
    current_coordinate_rate = (
        sum(
            institution.status is InstitutionStatus.ACTIVE
            for institution in selectable_institutions
        )
        / len(selectable_institutions)
        if selectable_institutions
        else 1.0
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

    effective_source_provenance = dict(source_provenance)
    output_sources = {institution.source for institution in institutions}
    if previous is not None:
        previous_sources = {
            item.source: item for item in previous.manifest.sources
        }
        for source_name in output_sources - set(effective_source_provenance):
            prior = previous_sources.get(source_name)
            if prior is None:
                raise SnapshotQualityError(
                    "preserved source provenance is unavailable"
                )
            effective_source_provenance[source_name] = SourceProvenance(
                source=prior.source,
                endpoint=prior.endpoint,
                license_name=prior.license_name,
                attribution=prior.attribution,
                fetched_at=prior.fetched_at,
                source_as_of=prior.source_as_of,
                source_observation_date_counts=tuple(
                    prior.source_observation_date_counts.items()
                ),
                normalized_observation_date_counts=(),
                raw_sha256=prior.raw_sha256,
                page_count=prior.page_count,
                row_count=0,
                fetched_row_count=prior.fetched_row_count,
                request_region_code=prior.request_region_code,
                request_timing=prior.request_timing,
                normalized_sha256=prior.source_normalized_sha256,
                unclassified_school_kind_counts=tuple(
                    prior.unclassified_school_kind_counts.items()
                ),
                unclassified_school_policy_sha256=(
                    prior.unclassified_school_policy_sha256
                ),
            )

    effective_enrichment_provenance = {
        item.source: item for item in enrichment_provenance
    }
    qualities_to_sources = {
        "OFFICIAL_STANDARD_COORDINATE": "OFFICIAL_STANDARD_SCHOOL_LOCATION",
        "GEOCODED": "KAKAO_LOCAL_GEOCODING",
    }
    required_enrichments = {
        qualities_to_sources[site.coordinate_quality]
        for site in sites
        if site.coordinate_quality in qualities_to_sources
    }
    if previous is not None:
        previous_enrichments = {
            item.source: item for item in previous.manifest.enrichments
        }
        for enrichment_source in (
            required_enrichments - set(effective_enrichment_provenance)
        ):
            prior_enrichment = previous_enrichments.get(enrichment_source)
            if prior_enrichment is None:
                raise SnapshotQualityError(
                    "preserved enrichment provenance is unavailable"
                )
            effective_enrichment_provenance[enrichment_source] = (
                EnrichmentProvenance(
                    source=prior_enrichment.source,
                    endpoint=prior_enrichment.endpoint,
                    license_name=prior_enrichment.license_name,
                    attribution=prior_enrichment.attribution,
                    fetched_at=prior_enrichment.fetched_at,
                    source_as_of=prior_enrichment.source_as_of,
                    raw_sha256=prior_enrichment.raw_sha256,
                    normalized_sha256=prior_enrichment.source_normalized_sha256,
                    request_region_code=prior_enrichment.request_region_code,
                    request_timing=prior_enrichment.request_timing,
                    page_count=prior_enrichment.page_count,
                    fetched_row_count=prior_enrichment.fetched_row_count,
                    matched_row_count=0,
                    matched_normalized_sha256=None,
                )
            )

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
            + [
                source_date
                for item in effective_source_provenance.values()
                for source_date, _ in item.source_observation_date_counts
            ]
            + [
                item.source_as_of
                for item in effective_enrichment_provenance.values()
            ]
        ),
        default=now[:10],
    )
    possible_matches = _persisted_possible_matches(institutions, sites)
    manifest = _candidate_manifest(
        snapshot_id=snapshot_id,
        created_at=now,
        snapshot_as_of=snapshot_as_of,
        institutions=institutions,
        sites=sites,
        institution_bytes=institution_bytes,
        site_bytes=site_bytes,
        possible_matches=possible_matches,
        previous=previous,
        source_provenance=effective_source_provenance,
        source_records=records,
        enrichment_provenance=tuple(
            effective_enrichment_provenance[source]
            for source in sorted(effective_enrichment_provenance)
        ),
    )
    _write_json(candidate_path / "manifest.json", manifest)
    for file_name in ("manifest.json", "institutions.jsonl", "sites.jsonl"):
        _fsync_file(candidate_path / file_name)
    _fsync_directory(candidate_path)
    _fsync_directory(root)
    _create_build_transaction(
        root,
        snapshot_id=snapshot_id,
        manifest=manifest,
        issues=tuple(issues),
    )
    return SnapshotBuildResult(
        snapshot_id=snapshot_id,
        candidate_path=candidate_path,
        approved=False,
        issues=tuple(issues),
    )


def build_candidate_review_packet(
    *,
    snapshot_id: str,
    snapshot_root: Path,
    coverage: CoverageService,
) -> dict[str, object]:
    candidate, manifest, institutions, sites, _ = _load_reviewable_candidate(
        snapshot_id=snapshot_id,
        root=Path(snapshot_root),
        coverage=coverage,
        allow_final_recovery=True,
    )
    return _review_packet_from_loaded_candidate(
        snapshot_id=snapshot_id,
        candidate=candidate,
        manifest=manifest,
        institutions=institutions,
        sites=sites,
    )


def _review_packet_from_loaded_candidate(
    *,
    snapshot_id: str,
    candidate: SnapshotBuildResult,
    manifest: dict[str, object],
    institutions: list[Institution],
    sites: list[InstitutionSite],
) -> dict[str, object]:
    source_entries = cast(list[dict[str, object]], manifest["sources"])
    source_counts: dict[str, object] = {}
    source_observation_counts: dict[str, object] = {}
    normalized_observation_counts: dict[str, object] = {}
    preserved_observation_counts: dict[str, object] = {}
    for entry in sorted(source_entries, key=lambda item: cast(str, item["source"])):
        source = cast(str, entry["source"])
        source_counts[source] = {
            "fetched": entry["fetchedRowCount"],
            "normalized": entry["normalizedRowCount"],
            "preserved": entry["preservedRowCount"],
            "output": entry["rowCount"],
        }
        source_observation_counts[source] = dict(
            cast(dict[str, int], entry["sourceObservationDateCounts"])
        )
        normalized_observation_counts[source] = dict(
            cast(dict[str, int], entry["normalizedObservationDateCounts"])
        )
        preserved_observation_counts[source] = dict(
            cast(dict[str, int], entry["preservedObservationDateCounts"])
        )
    district_counts = {district: 0 for district in _SEOUL_DISTRICTS}
    for site in sites:
        if site.is_default and site.district in district_counts:
            district_counts[site.district] += 1
    candidate_manifest = dict(manifest)
    candidate_manifest["approved"] = False
    candidate_manifest["approvedAt"] = None
    candidate_manifest["approvedByRole"] = None
    if manifest.get("approved") is False:
        manifest_bytes = _validated_snapshot_file(
            candidate.candidate_path / "manifest.json",
            candidate.candidate_path,
            "manifest.json",
        ).read_bytes()
    else:
        manifest_bytes = (
            json.dumps(
                candidate_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    packet: dict[str, object] = {
        "status": "CANDIDATE_REVIEW_REQUIRED",
        "snapshotId": snapshot_id,
        "createdAt": manifest["createdAt"],
        "snapshotAsOf": manifest["snapshotAsOf"],
        "previousSnapshotId": cast(dict[str, object], manifest["diff"])[
            "previousSnapshotId"
        ],
        "sourceCounts": source_counts,
        "sourceObservationDateCounts": source_observation_counts,
        "normalizedObservationDateCounts": normalized_observation_counts,
        "preservedObservationDateCounts": preserved_observation_counts,
        "unclassifiedSchoolKindCounts": {},
        "unclassifiedSchoolPolicySha256": None,
        "institutionTypeCounts": dict(
            cast(dict[str, int], manifest["countsByType"])
        ),
        "foundationCounts": dict(
            cast(dict[str, int], manifest["countsByFoundation"])
        ),
        "districtCounts": district_counts,
        "statusCounts": dict(cast(dict[str, int], manifest["countsByStatus"])),
        "coordinateQualityCounts": dict(
            cast(dict[str, int], manifest["coordinateQualityCounts"])
        ),
        "quarantinedInstitutionIds": sorted(
            institution.institution_id
            for institution in institutions
            if institution.status is InstitutionStatus.REVIEW_REQUIRED
        ),
        "quarantinedSiteIds": sorted(
            site.site_id
            for site in sites
            if site.status is InstitutionStatus.REVIEW_REQUIRED
        ),
        "diff": dict(cast(dict[str, object], manifest["diff"])),
        "institutionsSha256": manifest["institutionsSha256"],
        "sitesSha256": manifest["sitesSha256"],
        "candidateManifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "sourceProvenanceSha256": _manifest_section_sha256(manifest["sources"]),
        "enrichmentProvenanceSha256": _manifest_section_sha256(
            manifest["enrichments"]
        ),
    }
    neis_entry = next(
        (
            entry
            for entry in source_entries
            if entry["source"] == "NEIS"
        ),
        None,
    )
    if neis_entry is not None:
        packet["unclassifiedSchoolKindCounts"] = dict(
            cast(dict[str, int], neis_entry["unclassifiedSchoolKindCounts"])
        )
        packet["unclassifiedSchoolPolicySha256"] = neis_entry[
            "unclassifiedSchoolPolicySha256"
        ]
    packet["reviewDigest"] = _manifest_section_sha256(packet)
    return packet


def approve_candidate_snapshot(
    *,
    snapshot_id: str,
    review_digest: str,
    reviewer_role: str,
    snapshot_root: Path,
    coverage: CoverageService,
) -> str:
    if _SAFE_SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise SnapshotQualityError("snapshot ID is unsafe")
    if type(review_digest) is not str or _SHA256.fullmatch(review_digest) is None:
        raise SnapshotQualityError("review digest must be exact lowercase SHA-256")
    if reviewer_role != "data-steward":
        raise SnapshotQualityError("reviewer role must be data-steward")
    root = _validated_snapshot_root(Path(snapshot_root))
    lock_path = root / ".promotion.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise SnapshotQualityError("promotion lock is invalid") from exc
    try:
        if (
            not stat.S_ISREG(os.fstat(descriptor).st_mode)
            or lock_path.resolve(strict=True).parent != root
        ):
            raise SnapshotQualityError("promotion lock must be a regular root file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        packet = build_candidate_review_packet(
            snapshot_id=snapshot_id,
            snapshot_root=root,
            coverage=coverage,
        )
        recomputed_digest = cast(str, packet["reviewDigest"])
        if not hmac.compare_digest(review_digest, recomputed_digest):
            raise SnapshotQualityError("review digest does not match candidate")
        _promote_snapshot_locked(
            snapshot_id,
            root,
            coverage=coverage,
            expected_review_digest=review_digest,
        )
        try:
            verified = verify_snapshot(root)
        except (OSError, ValueError) as exc:
            raise SnapshotQualityError("approved snapshot verification failed") from exc
        if verified.manifest.snapshot_id != snapshot_id:
            raise SnapshotQualityError("approved snapshot verification failed")
        return recomputed_digest
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _load_reviewable_candidate(
    *,
    snapshot_id: str,
    root: Path,
    coverage: CoverageService,
    allow_final_recovery: bool = False,
) -> tuple[
    SnapshotBuildResult,
    dict[str, object],
    list[Institution],
    list[InstitutionSite],
    dict[str, object],
]:
    if _SAFE_SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise SnapshotQualityError("snapshot ID is unsafe")
    root = _validated_snapshot_root(Path(root))
    expected_candidate_name = f".{snapshot_id}.candidate"
    candidate_path = root / expected_candidate_name
    if candidate_path.is_symlink():
        raise SnapshotQualityError("candidate path must not be a symlink")
    transaction = _load_build_transaction(root, snapshot_id)
    transaction_issues = transaction.get("issues")
    if type(transaction_issues) is not list:
        raise SnapshotQualityError("build transaction issues are invalid")
    if transaction_issues:
        raise SnapshotQualityError("; ".join(cast(list[str], transaction_issues)))
    phase = cast(str, transaction["phase"])
    final_path = root / snapshot_id
    if final_path.is_symlink():
        raise SnapshotQualityError("final snapshot path must not be a symlink")
    current_path = root / "current.json"
    if current_path.is_symlink():
        raise SnapshotQualityError("current pointer must not be a symlink")
    if candidate_path.exists() and final_path.exists():
        raise SnapshotQualityError("candidate and final snapshot both exist")
    selected_path = candidate_path if candidate_path.exists() else final_path
    if selected_path == final_path and not allow_final_recovery:
        raise SnapshotQualityError("candidate snapshot is missing")
    if not selected_path.is_dir():
        raise SnapshotQualityError("candidate snapshot is missing")
    manifest_path = _validated_snapshot_file(
        selected_path / "manifest.json",
        selected_path,
        "manifest.json",
    )
    manifest = _read_json_object(manifest_path)
    try:
        _verify_manifest_fields(manifest)
    except SnapshotIntegrityError as exc:
        raise SnapshotQualityError("candidate manifest fields are invalid") from exc
    if selected_path == candidate_path and manifest.get("approved") is not False:
        raise SnapshotQualityError("candidate manifest must remain approved=false")
    if selected_path == candidate_path and phase != "BUILT":
        raise SnapshotQualityError("build transaction phase is invalid")
    if transaction.get("sourcesSha256") != _manifest_section_sha256(
        manifest.get("sources")
    ):
        raise SnapshotQualityError(
            "candidate source provenance transaction attestation mismatch"
        )
    if transaction.get("enrichmentsSha256") != _manifest_section_sha256(
        manifest.get("enrichments")
    ):
        raise SnapshotQualityError(
            "candidate enrichment provenance transaction attestation mismatch"
        )
    if manifest.get("approved") is False:
        if selected_path == final_path and phase not in {
            "BUILT",
            "MOVED",
            "APPROVAL_PREPARED",
        }:
            raise SnapshotQualityError("build transaction approval phase is invalid")
        _validate_unapproved_manifest_schema(manifest)
    elif selected_path == final_path and manifest.get("approved") is True:
        if phase not in {
            "APPROVAL_PREPARED",
            "VERIFIED",
            "POINTER_PREPARED",
            "PUBLISHED",
        }:
            raise SnapshotQualityError("build transaction approval phase is invalid")
        _validate_approved_manifest_schema(manifest)
    else:
        raise SnapshotQualityError("recoverable final manifest approval is invalid")
    institutions, sites = _recheck_candidate(selected_path, manifest, snapshot_id)
    _recheck_promotion_quality(root, manifest, institutions, sites, coverage)
    _recheck_source_provenance(manifest, institutions, sites)
    _recheck_enrichment_provenance(manifest, institutions, sites)
    _transaction_attests_manifest(transaction, manifest)
    candidate = SnapshotBuildResult(
        snapshot_id=snapshot_id,
        candidate_path=selected_path,
        approved=False,
        issues=(),
    )
    return candidate, manifest, institutions, sites, transaction


def _promote_snapshot_locked(
    snapshot_id: str,
    output_root: Path,
    *,
    coverage: CoverageService,
    expected_review_digest: str,
) -> None:
    root = _validated_snapshot_root(Path(output_root))
    candidate, manifest, institutions, sites, transaction = _load_reviewable_candidate(
        snapshot_id=snapshot_id,
        root=root,
        coverage=coverage,
        allow_final_recovery=True,
    )
    final_packet = _review_packet_from_loaded_candidate(
        snapshot_id=snapshot_id,
        candidate=candidate,
        manifest=manifest,
        institutions=institutions,
        sites=sites,
    )
    final_digest = cast(str, final_packet["reviewDigest"])
    if not hmac.compare_digest(expected_review_digest, final_digest):
        raise SnapshotQualityError("review digest does not match candidate")
    selected_path = candidate.candidate_path
    expected_candidate_name = f".{snapshot_id}.candidate"
    candidate_path = root / expected_candidate_name
    final_path = root / snapshot_id
    current_path = root / "current.json"
    phase = cast(str, transaction["phase"])
    manifest_path = selected_path / "manifest.json"
    for file_name in ("manifest.json", "institutions.jsonl", "sites.jsonl"):
        _fsync_file(selected_path / file_name)
    _fsync_directory(selected_path)
    if selected_path == candidate_path:
        os.replace(candidate_path, final_path)
        _fsync_directory(root)
        selected_path = final_path
        manifest_path = final_path / "manifest.json"
        transaction = _advance_build_transaction(
            root,
            transaction,
            phase="MOVED",
        )
        phase = "MOVED"
    elif manifest.get("approved") is False and phase == "BUILT":
        transaction = _advance_build_transaction(
            root,
            transaction,
            phase="MOVED",
        )
        phase = "MOVED"
    if manifest.get("approved") is False:
        approved_manifest = dict(manifest)
        if phase == "MOVED":
            approved_manifest["approved"] = True
            approved_manifest["approvedAt"] = _utc_now()
            approved_manifest["approvedByRole"] = "data-steward"
            transaction = _advance_build_transaction(
                root,
                transaction,
                phase="APPROVAL_PREPARED",
                approved_manifest=approved_manifest,
            )
            phase = "APPROVAL_PREPARED"
        elif phase == "APPROVAL_PREPARED":
            approved_manifest["approved"] = True
            approved_manifest["approvedAt"] = transaction.get("approvedAt")
            approved_manifest["approvedByRole"] = transaction.get(
                "approvedByRole"
            )
        else:
            raise SnapshotQualityError("build transaction approval phase is invalid")
        if transaction.get("approvedManifestSha256") != _manifest_section_sha256(
            approved_manifest
        ):
            raise SnapshotQualityError("build transaction approval phase mismatch")
        temporary_manifest = selected_path / ".manifest.json.tmp"
        _validate_atomic_temporary_path(
            temporary_manifest,
            selected_path,
            ".manifest.json.tmp",
        )
        _write_json(temporary_manifest, approved_manifest, durable=True)
        os.replace(temporary_manifest, manifest_path)
        _fsync_directory(selected_path)
        manifest = approved_manifest

    _transaction_attests_manifest(transaction, manifest)

    try:
        verify_snapshot_directory(root, snapshot_id)
    except SnapshotIntegrityError as exc:
        raise SnapshotQualityError(
            "strict snapshot verification failed before pointer publication"
        ) from exc
    if phase == "APPROVAL_PREPARED":
        transaction = _advance_build_transaction(
            root,
            transaction,
            phase="VERIFIED",
        )
        phase = "VERIFIED"
    if phase == "PUBLISHED":
        try:
            verified = verify_snapshot(root)
        except (OSError, ValueError) as exc:
            raise SnapshotQualityError(
                "published build transaction pointer is invalid"
            ) from exc
        if verified.manifest.snapshot_id != snapshot_id:
            raise SnapshotQualityError(
                "published build transaction pointer is invalid"
            )
        return
    if phase == "VERIFIED":
        transaction = _advance_build_transaction(
            root,
            transaction,
            phase="POINTER_PREPARED",
        )
        phase = "POINTER_PREPARED"
    if phase != "POINTER_PREPARED":
        raise SnapshotQualityError("build transaction pointer phase is invalid")

    temporary_pointer = root / ".current.json.tmp"
    _validate_atomic_temporary_path(
        temporary_pointer,
        root,
        ".current.json.tmp",
    )
    _write_json(
        temporary_pointer,
        {"snapshotId": snapshot_id},
        durable=True,
    )
    os.replace(temporary_pointer, current_path)
    _fsync_directory(root)
    _advance_build_transaction(
        root,
        transaction,
        phase="PUBLISHED",
    )


def _validated_snapshot_root(root: Path) -> Path:
    if root.is_symlink():
        raise SnapshotQualityError("snapshot root must not be a symlink")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotQualityError("snapshot root is invalid") from exc
    if not resolved.is_dir():
        raise SnapshotQualityError("snapshot root must be a directory")
    return resolved


def _validated_snapshot_file(path: Path, parent: Path, label: str) -> Path:
    if path.is_symlink():
        raise SnapshotQualityError(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotQualityError(f"candidate {label} is missing") from exc
    if resolved.parent != parent or not resolved.is_file():
        raise SnapshotQualityError(
            f"candidate {label} must be a regular snapshot file"
        )
    return resolved


def _validate_atomic_temporary_path(
    path: Path,
    parent: Path,
    label: str,
) -> None:
    if path.is_symlink():
        raise SnapshotQualityError(f"{label} must not be a symlink")
    if path.exists():
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SnapshotQualityError(f"{label} is invalid") from exc
        if resolved.parent != parent or not resolved.is_file():
            raise SnapshotQualityError(f"{label} must be a regular temporary file")


def _build_current_records(
    records: tuple[SourceInstitutionRecord, ...],
    snapshot_id: str,
    coverage: CoverageService,
) -> tuple[list[Institution], list[InstitutionSite]]:
    institutions: list[Institution] = []
    sites: list[InstitutionSite] = []
    for record in sorted(records, key=lambda item: item.institution_id):
        is_unclassified = record.institution_type == "UNCLASSIFIED_SCHOOL"
        source_sites = (
            SourceInstitutionSiteRecord(
                site_code="main",
                site_name=record.site_name,
                road_address=record.road_address,
                district=record.district,
                latitude=record.latitude,
                longitude=record.longitude,
                coordinate_quality=record.coordinate_quality,
            ),
            *record.additional_sites,
        )
        built_sites: list[InstitutionSite] = []
        for source_site in source_sites:
            site_status = (
                InstitutionStatus.REVIEW_REQUIRED
                if is_unclassified
                else _status_from_coordinate_and_coverage(source_site, coverage)
            )
            built_sites.append(
                InstitutionSite(
                    site_id=f"{record.institution_id}:{source_site.site_code}",
                    institution_id=record.institution_id,
                    site_name=source_site.site_name,
                    road_address=source_site.road_address,
                    district=source_site.district,
                    latitude=source_site.latitude,
                    longitude=source_site.longitude,
                    coordinate_quality=source_site.coordinate_quality,
                    routing_anchor_latitude=source_site.latitude,
                    routing_anchor_longitude=source_site.longitude,
                    is_default=source_site.site_code == "main",
                    status=site_status,
                    effective_from=record.source_as_of,
                    effective_to=None,
                )
            )
        status = (
            InstitutionStatus.REVIEW_REQUIRED
            if is_unclassified
            else (
                InstitutionStatus.ACTIVE
                if any(
                    site.is_default and site.status is InstitutionStatus.ACTIVE
                    for site in built_sites
                )
                else InstitutionStatus.REVIEW_REQUIRED
            )
        )
        institutions.append(
            Institution(
                institution_id=record.institution_id,
                official_name=record.official_name,
                institution_type=record.institution_type,
                foundation_type=record.foundation_type,
                education_office=record.education_office,
                status=status,
                status_source=(
                    "OFFICIAL_CLASSIFICATION_PENDING"
                    if is_unclassified
                    else record.source
                ),
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
        sites.extend(built_sites)
    return institutions, sites


def _status_from_coordinate_and_coverage(
    source_site: SourceInstitutionSiteRecord,
    coverage: CoverageService,
) -> InstitutionStatus:
    if (
        source_site.latitude is not None
        and source_site.longitude is not None
        and _is_seoul_address(source_site.road_address)
        and coverage.classify(
            Coordinate(
                latitude=source_site.latitude,
                longitude=source_site.longitude,
            )
        )
        is CoverageState.SEOUL
    ):
        return InstitutionStatus.ACTIVE
    return InstitutionStatus.REVIEW_REQUIRED


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
    if record.institution_type == "UNCLASSIFIED_SCHOOL":
        if record.source != "NEIS" or not record.source_kind_label:
            raise SnapshotQualityError("unclassified school source label is invalid")
    elif record.source_kind_label is not None:
        raise SnapshotQualityError("source kind label is reserved for unclassified schools")
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
    if not record.site_name.strip():
        raise SnapshotQualityError("source main site name must be nonblank")
    site_codes: set[str] = set()
    for site in record.additional_sites:
        if (
            _SAFE_SITE_CODE.fullmatch(site.site_code) is None
            or site.site_code == "main"
            or site.site_code in site_codes
        ):
            raise SnapshotQualityError("source branch site code is invalid")
        site_codes.add(site.site_code)
        if (
            not site.site_name.strip()
            or not site.road_address.strip()
            or not site.district.strip()
        ):
            raise SnapshotQualityError("source branch site fields must be nonblank")
        has_site_latitude = site.latitude is not None
        has_site_longitude = site.longitude is not None
        if has_site_latitude != has_site_longitude:
            raise SnapshotQualityError("source site coordinate pair is incomplete")
        if has_site_latitude == (site.coordinate_quality == "MISSING"):
            raise SnapshotQualityError(
                "source site coordinate quality does not match coordinates"
            )
        if site.coordinate_quality not in _ALLOWED_COORDINATE_QUALITIES:
            raise SnapshotQualityError("unsupported coordinate quality")


def _validate_source_provenance(
    source_name: str,
    provenance: SourceProvenance,
    records: list[SourceInstitutionRecord],
) -> None:
    fetched_row_count = provenance.fetched_row_count
    checked_fetched_row_count = (
        fetched_row_count if type(fetched_row_count) is int else -1
    )
    expected_timing = provenance.request_timing
    expected_normalized_hash = normalized_records_sha256(
        [_record_before_enrichment(record) for record in records]
    )
    try:
        validate_observation_date_counts(
            provenance.source_observation_date_counts,
            expected_total=checked_fetched_row_count,
            label="raw observation",
        )
        validate_observation_date_counts(
            provenance.normalized_observation_date_counts,
            expected_total=len(records),
            label="normalized observation",
        )
        normalized_counts = observation_date_counts(
            record.source_as_of for record in records
        )
    except SourceDataError as exc:
        raise SnapshotQualityError(
            "source provenance observation dates are invalid"
        ) from exc
    if normalized_counts != provenance.normalized_observation_date_counts:
        raise SnapshotQualityError(
            "source provenance observation dates do not match normalized rows"
        )
    if provenance.source_as_of != source_as_of_for(
        provenance.source_observation_date_counts
    ):
        raise SnapshotQualityError(
            "source provenance source_as_of is not canonical"
        )
    if (
        provenance.endpoint != _SOURCE_ENDPOINTS[source_name]
        or provenance.license_name != _SOURCE_LICENSES[source_name]
        or provenance.attribution != _SOURCE_ATTRIBUTIONS[source_name]
        or provenance.request_region_code != _EXPECTED_REGION_CODES[source_name]
        or not _source_pagination_is_valid(
            source_name,
            provenance.page_count,
            checked_fetched_row_count,
        )
        or type(provenance.row_count) is not int
        or provenance.row_count != len(records)
        or checked_fetched_row_count < provenance.row_count
        or _SHA256.fullmatch(provenance.raw_sha256) is None
        or source_name in _PINNED_SOURCE_RAW_SHA256
        and provenance.raw_sha256 != _PINNED_SOURCE_RAW_SHA256[source_name]
        or provenance.normalized_sha256 != expected_normalized_hash
        or source_name in _PINNED_SOURCE_NORMALIZED_SHA256
        and provenance.normalized_sha256
        != _PINNED_SOURCE_NORMALIZED_SHA256[source_name]
    ):
        raise SnapshotQualityError("source provenance does not match normalized rows")
    if not _source_unclassified_provenance_matches(
        source_name,
        provenance.unclassified_school_kind_counts,
        provenance.unclassified_school_policy_sha256,
        records,
    ):
        raise SnapshotQualityError(
            "source provenance unclassified school policy does not match rows"
        )
    if source_name == "KINDERGARTEN_INFO":
        if (
            expected_timing is None
            or re.fullmatch(r"\d{4}[12]", expected_timing) is None
            or _kindergarten_timing_date(expected_timing) != provenance.source_as_of
            or fetched_row_count != provenance.row_count
        ):
            raise SnapshotQualityError("source provenance timing/count is invalid")
    elif expected_timing is not None:
        raise SnapshotQualityError("source provenance timing is invalid")
    if (
        source_name == "SEN_REVIEWED_CSV"
        and fetched_row_count != provenance.row_count + 1
    ):
        raise SnapshotQualityError("source provenance count is invalid")
    if (
        source_name == "NEIS"
        and checked_fetched_row_count - provenance.row_count > 50
    ):
        raise SnapshotQualityError("source provenance count is invalid")
    try:
        fetched_at = datetime.fromisoformat(
            provenance.fetched_at[:-1] + "+00:00"
            if provenance.fetched_at.endswith("Z")
            else provenance.fetched_at
        )
        source_dates = tuple(
            datetime.fromisoformat(source_date)
            for source_date, _ in provenance.source_observation_date_counts
        )
    except ValueError as exc:
        raise SnapshotQualityError("source provenance dates are invalid") from exc
    if fetched_at.tzinfo is None or any(
        source_date.date() > fetched_at.date() for source_date in source_dates
    ):
        raise SnapshotQualityError("source provenance chronology is invalid")


def _source_unclassified_provenance_matches(
    source_name: str,
    declared_counts: tuple[tuple[str, int], ...],
    declared_sha256: str | None,
    records: list[SourceInstitutionRecord],
) -> bool:
    if source_name != "NEIS":
        return declared_counts == () and declared_sha256 is None
    actual_counts = tuple(
        sorted(
            Counter(
                record.source_kind_label
                for record in records
                if record.institution_type == "UNCLASSIFIED_SCHOOL"
                and record.source_kind_label is not None
            ).items()
        )
    )
    unclassified_count = sum(
        record.institution_type == "UNCLASSIFIED_SCHOOL" for record in records
    )
    if not unclassified_count:
        return declared_counts == () and declared_sha256 is None
    if (
        declared_counts != actual_counts
        or sum(count for _, count in declared_counts) != unclassified_count
        or declared_sha256 != PINNED_POLICY_SHA256
    ):
        return False
    try:
        NeisUnclassifiedPolicy(
            counts=declared_counts,
            sha256=declared_sha256,
            reviewed_as_of="2026-08-13",
            reviewer_role="data-steward",
        )
    except ValueError:
        return False
    return True


def _record_before_enrichment(
    record: SourceInstitutionRecord,
) -> SourceInstitutionRecord:
    if record.coordinate_quality in {
        "OFFICIAL_STANDARD_COORDINATE",
        "GEOCODED",
    }:
        record = replace(
            record,
            latitude=None,
            longitude=None,
            coordinate_quality="MISSING",
        )
    additional_sites = tuple(
        replace(
            site,
            latitude=None,
            longitude=None,
            coordinate_quality="MISSING",
        )
        if site.coordinate_quality in {
            "OFFICIAL_STANDARD_COORDINATE",
            "GEOCODED",
        }
        else site
        for site in record.additional_sites
    )
    return replace(
        record,
        additional_sites=additional_sites,
    )


def _kindergarten_timing_date(timing: str) -> str:
    month_day = "04-01" if timing[-1] == "1" else "10-01"
    return f"{timing[:4]}-{month_day}"


def _source_pagination_is_valid(
    source: str,
    page_count: object,
    fetched_row_count: object,
) -> bool:
    if (
        type(page_count) is not int
        or type(fetched_row_count) is not int
        or fetched_row_count <= 0
    ):
        return False
    if source == "NEIS":
        minimum_pages = (fetched_row_count + 999) // 1_000
        return (
            minimum_pages <= page_count <= min(200, fetched_row_count)
            and fetched_row_count <= 5_000
        )
    if source == "KINDERGARTEN_INFO":
        minimum_pages = max(25, (fetched_row_count + 99) // 100)
        return (
            minimum_pages <= page_count <= min(2_500, fetched_row_count + 25)
            and fetched_row_count <= 250_000
        )
    if source == "SEN_REVIEWED_CSV":
        return page_count == 1
    return False


def _validate_enrichment_provenance(
    records: tuple[SourceInstitutionRecord, ...],
    enrichments: tuple[EnrichmentProvenance, ...],
) -> None:
    by_source = {item.source: item for item in enrichments}
    if len(by_source) != len(enrichments):
        raise SnapshotQualityError("duplicate enrichment provenance")
    allowed = {
        "OFFICIAL_STANDARD_SCHOOL_LOCATION",
        "KAKAO_LOCAL_GEOCODING",
    }
    if not set(by_source) <= allowed:
        raise SnapshotQualityError("unsupported enrichment provenance")
    qualities = [record.coordinate_quality for record in records]
    qualities.extend(
        site.coordinate_quality
        for record in records
        for site in record.additional_sites
    )
    official_count = qualities.count("OFFICIAL_STANDARD_COORDINATE")
    geocoded_count = qualities.count("GEOCODED")
    if official_count and "OFFICIAL_STANDARD_SCHOOL_LOCATION" not in by_source:
        raise SnapshotQualityError(
            "official school-location enrichment provenance is required"
        )
    if geocoded_count and "KAKAO_LOCAL_GEOCODING" not in by_source:
        raise SnapshotQualityError("Kakao enrichment provenance is required")
    standard = by_source.get("OFFICIAL_STANDARD_SCHOOL_LOCATION")
    if standard is not None and (
        standard.endpoint != STANDARD_LOCATION_ENDPOINT
        or standard.license_name != "PUBLIC_DATA_NO_USE_RESTRICTION"
        or standard.attribution
        != "Korea Education Facilities Safety Authority"
        or standard.source_as_of != STANDARD_LOCATION_SOURCE_AS_OF
        or standard.raw_sha256 != STANDARD_LOCATION_RAW_SHA256
        or standard.normalized_sha256
        != _STANDARD_LOCATION_NORMALIZED_SHA256
        or standard.request_region_code != "7010000"
        or standard.request_timing is not None
        or standard.page_count != 1
        or standard.fetched_row_count != PINNED_NATIONWIDE_COUNT
        or standard.matched_row_count != official_count
        or standard.matched_normalized_sha256
        != enrichment_records_sha256(
            records,
            "OFFICIAL_STANDARD_COORDINATE",
        )
    ):
        raise SnapshotQualityError(
            "official school-location enrichment provenance is invalid"
        )
    kakao = by_source.get("KAKAO_LOCAL_GEOCODING")
    if kakao is not None and (
        kakao.endpoint != _KAKAO_ENDPOINT
        or kakao.license_name != "KAKAO_LOCAL_API_TERMS"
        or kakao.attribution != "Kakao Local API"
        or kakao.request_region_code != "SEOUL_ADDRESS_BATCH"
        or kakao.request_timing is not None
        or type(kakao.page_count) is not int
        or kakao.page_count <= 0
        or kakao.fetched_row_count != kakao.page_count
        or kakao.matched_row_count != geocoded_count
        or kakao.normalized_sha256 != _geocoded_records_sha256(records)
        or kakao.matched_normalized_sha256
        != enrichment_records_sha256(records, "GEOCODED")
        or _SHA256.fullmatch(kakao.raw_sha256) is None
        or kakao.source_as_of != kakao.fetched_at[:10]
    ):
        raise SnapshotQualityError("Kakao enrichment provenance is invalid")
    for item in enrichments:
        try:
            fetched_at = datetime.fromisoformat(
                item.fetched_at[:-1] + "+00:00"
                if item.fetched_at.endswith("Z")
                else item.fetched_at
            )
            source_date = datetime.fromisoformat(item.source_as_of)
        except ValueError as exc:
            raise SnapshotQualityError("enrichment provenance date is invalid") from exc
        if fetched_at.tzinfo is None or source_date.date() > fetched_at.date():
            raise SnapshotQualityError(
                "enrichment provenance chronology is invalid"
            )


def _geocoded_records_sha256(
    records: tuple[SourceInstitutionRecord, ...],
) -> str:
    values: list[dict[str, object]] = [
        {
            "road_address": record.road_address,
            "latitude": record.latitude,
            "longitude": record.longitude,
            "confidence": "EXACT_ROAD_ADDRESS",
        }
        for record in records
        if record.coordinate_quality == "GEOCODED"
    ]
    values.extend(
        {
            "road_address": site.road_address,
            "latitude": site.latitude,
            "longitude": site.longitude,
            "confidence": "EXACT_ROAD_ADDRESS",
        }
        for record in records
        for site in record.additional_sites
        if site.coordinate_quality == "GEOCODED"
    )
    values.sort(
        key=lambda item: (
            str(item["road_address"]),
            cast(float, item["latitude"]),
            cast(float, item["longitude"]),
        )
    )
    normalized = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def enrichment_records_sha256(
    records: tuple[SourceInstitutionRecord, ...],
    coordinate_quality: str,
) -> str:
    values: list[dict[str, object]] = []
    for record in records:
        source_sites = (
            SourceInstitutionSiteRecord(
                site_code="main",
                site_name=record.site_name,
                road_address=record.road_address,
                district=record.district,
                latitude=record.latitude,
                longitude=record.longitude,
                coordinate_quality=record.coordinate_quality,
            ),
            *record.additional_sites,
        )
        values.extend(
            {
                "site_id": f"{record.institution_id}:{site.site_code}",
                "road_address": site.road_address,
                "latitude": site.latitude,
                "longitude": site.longitude,
            }
            for site in source_sites
            if site.coordinate_quality == coordinate_quality
        )
    return _canonical_mapping_sha256(values)


def _enrichment_sites_sha256(
    sites: list[InstitutionSite],
    coordinate_quality: str,
) -> str:
    return _canonical_mapping_sha256(
        [
            {
                "site_id": site.site_id,
                "road_address": site.road_address,
                "latitude": site.latitude,
                "longitude": site.longitude,
            }
            for site in sites
            if site.coordinate_quality == coordinate_quality
        ]
    )


def _canonical_mapping_sha256(values: list[dict[str, object]]) -> str:
    values.sort(key=lambda item: str(item["site_id"]))
    return hashlib.sha256(
        json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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
    current_site_ids = {item.site_id for item in sites}
    for old in previous.institutions:
        if old.institution_id in current_ids:
            sites.extend(
                old_site.model_copy(
                    update={"status": InstitutionStatus.MISSING_FROM_SOURCE}
                )
                for old_site in previous.sites
                if old_site.institution_id == old.institution_id
                and old_site.site_id not in current_site_ids
            )
            continue
        institutions.append(
            old.model_copy(
                update={
                    "status": InstitutionStatus.MISSING_FROM_SOURCE,
                    "status_source": "MISSING_FROM_SOURCE_GATE",
                    "last_seen_snapshot": snapshot_id,
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
    possible_matches: list[dict[str, object]],
    previous: VerifiedSnapshot | None,
    source_provenance: Mapping[str, SourceProvenance] | None,
    source_records: tuple[SourceInstitutionRecord, ...],
    enrichment_provenance: tuple[EnrichmentProvenance, ...],
) -> dict[str, object]:
    by_source: dict[str, list[Institution]] = defaultdict(list)
    for institution in institutions:
        by_source[institution.source].append(institution)
    sites_by_parent: dict[str, list[InstitutionSite]] = defaultdict(list)
    for site in sites:
        sites_by_parent[site.institution_id].append(site)
    sources = []
    for source_name, source_rows in sorted(by_source.items()):
        if source_provenance is None or source_name not in source_provenance:
            raise SnapshotQualityError(
                "source provenance is required for every output source"
            )
        provenance = source_provenance[source_name]
        if provenance.fetched_row_count is None:
            raise SnapshotQualityError("source fetched row count is required")
        current_rows = [
            row
            for row in source_rows
            if row.status is not InstitutionStatus.MISSING_FROM_SOURCE
        ]
        preserved_rows = [
            row
            for row in source_rows
            if row.status is InstitutionStatus.MISSING_FROM_SOURCE
        ]
        normalized_counts = observation_date_counts(
            row.source_as_of for row in current_rows
        )
        preserved_counts = observation_date_counts(
            row.source_as_of for row in preserved_rows
        )
        if normalized_counts != provenance.normalized_observation_date_counts:
            raise SnapshotQualityError(
                "source provenance observation dates do not match output rows"
            )
        sources.append(
            {
                "source": source_name,
                "endpoint": provenance.endpoint,
                "licenseName": provenance.license_name,
                "attribution": provenance.attribution,
                "fetchedAt": provenance.fetched_at,
                "sourceAsOf": source_as_of_for(
                    provenance.source_observation_date_counts
                ),
                "sourceObservationDateCounts": observation_counts_as_dict(
                    provenance.source_observation_date_counts
                ),
                "normalizedObservationDateCounts": observation_counts_as_dict(
                    normalized_counts
                ),
                "preservedObservationDateCounts": observation_counts_as_dict(
                    preserved_counts
                ),
                "rawSha256": provenance.raw_sha256,
                "sourceNormalizedSha256": provenance.normalized_sha256,
                "normalizedSha256": (
                    _normalized_persisted_source_sha256(
                        source_rows,
                        sites_by_parent,
                    )
                ),
                "requestRegionCode": provenance.request_region_code,
                "requestTiming": provenance.request_timing,
                "pageCount": provenance.page_count,
                "fetchedRowCount": provenance.fetched_row_count,
                "normalizedRowCount": len(current_rows),
                "preservedRowCount": len(preserved_rows),
                "rowCount": len(source_rows),
                "unclassifiedSchoolKindCounts": dict(
                    provenance.unclassified_school_kind_counts
                ),
                "unclassifiedSchoolPolicySha256": (
                    provenance.unclassified_school_policy_sha256
                ),
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
    current_sites_by_parent: dict[str, list[InstitutionSite]] = defaultdict(list)
    previous_sites_by_parent: dict[str, list[InstitutionSite]] = defaultdict(list)
    for site in sites:
        current_sites_by_parent[site.institution_id].append(site)
    if previous is not None:
        for site in previous.sites:
            previous_sites_by_parent[site.institution_id].append(site)
    current_source_ids = {record.institution_id for record in source_records}
    changed_count = sum(
        (
            _institution_change_key(current_by_id[institution_id])
            != _institution_change_key(previous_by_id[institution_id])
            or _site_change_key(current_sites_by_parent[institution_id])
            != _site_change_key(previous_sites_by_parent[institution_id])
        )
        for institution_id in current_source_ids & previous_ids
    )
    institutions_by_id = {
        institution.institution_id: institution for institution in institutions
    }
    enrichment_qualities = {
        "OFFICIAL_STANDARD_SCHOOL_LOCATION": "OFFICIAL_STANDARD_COORDINATE",
        "KAKAO_LOCAL_GEOCODING": "GEOCODED",
    }
    enrichment_entries: list[dict[str, object]] = []
    for item in enrichment_provenance:
        quality = enrichment_qualities[item.source]
        matched_sites = [
            site for site in sites if site.coordinate_quality == quality
        ]
        current_matched_count = sum(
            site.status is not InstitutionStatus.MISSING_FROM_SOURCE
            and institutions_by_id[site.institution_id].status
            is not InstitutionStatus.MISSING_FROM_SOURCE
            for site in matched_sites
        )
        preserved_matched_count = len(matched_sites) - current_matched_count
        enrichment_entries.append(
            {
                "source": item.source,
                "endpoint": item.endpoint,
                "licenseName": item.license_name,
                "attribution": item.attribution,
                "fetchedAt": item.fetched_at,
                "sourceAsOf": item.source_as_of,
                "rawSha256": item.raw_sha256,
                "sourceNormalizedSha256": item.normalized_sha256,
                "normalizedSha256": _enrichment_sites_sha256(sites, quality),
                "requestRegionCode": item.request_region_code,
                "requestTiming": item.request_timing,
                "pageCount": item.page_count,
                "fetchedRowCount": item.fetched_row_count,
                "matchedRowCount": current_matched_count,
                "preservedMatchedRowCount": preserved_matched_count,
                "rowCount": len(matched_sites),
            }
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
        "enrichments": enrichment_entries,
        "institutionsSha256": hashlib.sha256(institution_bytes).hexdigest(),
        "sitesSha256": hashlib.sha256(site_bytes).hexdigest(),
        "institutionCount": len(institutions),
        "siteCount": len(sites),
        "quarantinedCount": sum(
            item.status is InstitutionStatus.REVIEW_REQUIRED
            for item in institutions
        ),
        "possibleMatchCount": len(possible_matches),
        "possibleMatches": possible_matches,
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


def _site_change_key(sites: list[InstitutionSite]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                site.site_id,
                site.site_name,
                site.road_address,
                site.district,
                site.latitude,
                site.longitude,
                site.coordinate_quality,
                site.routing_anchor_latitude,
                site.routing_anchor_longitude,
                site.is_default,
                site.status,
                site.effective_to,
            )
            for site in sites
        )
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
    try:
        value = _strict_json_loads(path.read_text(encoding="utf-8"))
    except SnapshotQualityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotQualityError("candidate manifest is invalid JSON") from exc
    if type(value) is not dict:
        raise SnapshotQualityError("candidate manifest is not an object")
    return value


def _strict_json_loads(data: str | bytes) -> object:
    return json.loads(
        data,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_nonstandard_json_constant,
    )


def _manifest_section_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _create_build_transaction(
    root: Path,
    *,
    snapshot_id: str,
    manifest: dict[str, object],
    issues: tuple[str, ...],
) -> None:
    transaction_directory = _validated_transaction_directory(root, create=True)
    transaction_path = transaction_directory / f"{snapshot_id}.json"
    if transaction_path.exists() or transaction_path.is_symlink():
        raise SnapshotQualityError("build transaction already exists")
    diff = manifest.get("diff")
    if type(diff) is not dict:
        raise SnapshotQualityError("candidate diff metadata is invalid")
    transaction: dict[str, object] = {
        "version": _TRANSACTION_VERSION,
        "snapshotId": snapshot_id,
        "candidateName": f".{snapshot_id}.candidate",
        "nonce": secrets.token_hex(16),
        "phase": "BUILT",
        "issues": list(issues),
        "manifestSha256": _manifest_section_sha256(manifest),
        "sourcesSha256": _manifest_section_sha256(manifest.get("sources")),
        "enrichmentsSha256": _manifest_section_sha256(
            manifest.get("enrichments")
        ),
        "institutionsSha256": manifest.get("institutionsSha256"),
        "sitesSha256": manifest.get("sitesSha256"),
        "previousSnapshotId": diff.get("previousSnapshotId"),
        "approvedManifestSha256": None,
        "approvedAt": None,
        "approvedByRole": None,
    }
    _write_signed_transaction(root, transaction, replace_existing=False)


def _validated_transaction_directory(root: Path, *, create: bool) -> Path:
    path = root / _TRANSACTION_DIRECTORY_NAME
    if create:
        try:
            path.mkdir(mode=0o700)
            _fsync_directory(root)
        except FileExistsError:
            pass
    if path.is_symlink():
        raise SnapshotQualityError("build transaction directory is invalid")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SnapshotQualityError("build transaction directory is missing") from exc
    if resolved.parent != root or not resolved.is_dir():
        raise SnapshotQualityError("build transaction directory is invalid")
    return resolved


def _load_or_create_attestation_key(root: Path) -> bytes:
    key_path = root / _TRANSACTION_KEY_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(key_path, flags, 0o600)
    except FileExistsError:
        return _load_attestation_key(root)
    except OSError as exc:
        raise SnapshotQualityError("build transaction key is invalid") from exc
    try:
        key = secrets.token_bytes(32)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    _fsync_directory(root)
    return key


def _load_attestation_key(root: Path) -> bytes:
    key_path = root / _TRANSACTION_KEY_NAME
    if key_path.is_symlink():
        raise SnapshotQualityError("build transaction key is invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(key_path, flags)
    except OSError as exc:
        raise SnapshotQualityError("build transaction key is invalid") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or key_path.resolve(strict=True).parent != root
        ):
            raise SnapshotQualityError("build transaction key is invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            key = stream.read(33)
    finally:
        os.close(descriptor)
    if len(key) != 32:
        raise SnapshotQualityError("build transaction key is invalid")
    return key


def _signed_transaction(
    transaction: Mapping[str, object],
    key: bytes,
) -> dict[str, object]:
    body = {name: value for name, value in transaction.items() if name != "signature"}
    signature = hmac.new(
        key,
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {**body, "signature": signature}


def _write_signed_transaction(
    root: Path,
    transaction: Mapping[str, object],
    *,
    replace_existing: bool,
) -> dict[str, object]:
    snapshot_id = transaction.get("snapshotId")
    if type(snapshot_id) is not str or _SAFE_SNAPSHOT_ID.fullmatch(snapshot_id) is None:
        raise SnapshotQualityError("build transaction snapshot ID is invalid")
    directory = _validated_transaction_directory(root, create=True)
    path = directory / f"{snapshot_id}.json"
    if not replace_existing and (path.exists() or path.is_symlink()):
        raise SnapshotQualityError("build transaction already exists")
    temporary = directory / f".{snapshot_id}.json.tmp"
    _validate_atomic_temporary_path(
        temporary,
        directory,
        f".{snapshot_id}.json.tmp",
    )
    signed = _signed_transaction(transaction, _load_or_create_attestation_key(root))
    _write_json(temporary, signed, durable=True)
    os.replace(temporary, path)
    _fsync_directory(directory)
    return signed


def _load_build_transaction(root: Path, snapshot_id: str) -> dict[str, object]:
    directory = _validated_transaction_directory(root, create=False)
    path = _validated_snapshot_file(
        directory / f"{snapshot_id}.json",
        directory,
        "build transaction",
    )
    transaction = _read_json_object(path)
    if set(transaction) != _TRANSACTION_FIELDS:
        raise SnapshotQualityError("build transaction fields are invalid")
    signature = transaction.get("signature")
    if type(signature) is not str or _SHA256.fullmatch(signature) is None:
        raise SnapshotQualityError("build transaction signature is invalid")
    expected = _signed_transaction(transaction, _load_attestation_key(root))[
        "signature"
    ]
    if type(expected) is not str or not hmac.compare_digest(signature, expected):
        raise SnapshotQualityError("build transaction attestation is invalid")
    _validate_build_transaction(transaction, snapshot_id)
    return transaction


def _validate_build_transaction(
    transaction: Mapping[str, object],
    snapshot_id: str,
) -> None:
    phase = transaction.get("phase")
    issues = transaction.get("issues")
    nonce = transaction.get("nonce")
    digest_fields = (
        "manifestSha256",
        "sourcesSha256",
        "enrichmentsSha256",
        "institutionsSha256",
        "sitesSha256",
    )
    if (
        transaction.get("version") != _TRANSACTION_VERSION
        or transaction.get("snapshotId") != snapshot_id
        or transaction.get("candidateName") != f".{snapshot_id}.candidate"
        or type(nonce) is not str
        or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        or phase not in _TRANSACTION_PHASES
        or type(issues) is not list
        or any(type(issue) is not str for issue in issues)
        or any(
            type(transaction.get(field)) is not str
            or _SHA256.fullmatch(cast(str, transaction.get(field))) is None
            for field in digest_fields
        )
        or (
            transaction.get("previousSnapshotId") is not None
            and type(transaction.get("previousSnapshotId")) is not str
        )
    ):
        raise SnapshotQualityError("build transaction contents are invalid")
    approval_values = (
        transaction.get("approvedManifestSha256"),
        transaction.get("approvedAt"),
        transaction.get("approvedByRole"),
    )
    if phase in {"BUILT", "MOVED"}:
        if any(value is not None for value in approval_values):
            raise SnapshotQualityError("build transaction approval phase is invalid")
    elif (
        type(approval_values[0]) is not str
        or _SHA256.fullmatch(cast(str, approval_values[0])) is None
        or type(approval_values[1]) is not str
        or approval_values[2] != "data-steward"
    ):
        raise SnapshotQualityError("build transaction approval phase is invalid")


def _advance_build_transaction(
    root: Path,
    transaction: Mapping[str, object],
    *,
    phase: str,
    approved_manifest: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if phase not in _TRANSACTION_PHASES:
        raise SnapshotQualityError("build transaction phase is invalid")
    updated = dict(transaction)
    updated["phase"] = phase
    if approved_manifest is not None:
        updated["approvedManifestSha256"] = _manifest_section_sha256(
            approved_manifest
        )
        updated["approvedAt"] = approved_manifest.get("approvedAt")
        updated["approvedByRole"] = approved_manifest.get("approvedByRole")
    updated.pop("signature", None)
    return _write_signed_transaction(root, updated, replace_existing=True)


def _transaction_attests_manifest(
    transaction: Mapping[str, object],
    manifest: Mapping[str, object],
) -> None:
    unapproved = dict(manifest)
    unapproved["approved"] = False
    unapproved["approvedAt"] = None
    unapproved["approvedByRole"] = None
    diff = unapproved.get("diff")
    if type(diff) is not dict:
        raise SnapshotQualityError("candidate diff metadata is invalid")
    if (
        transaction.get("manifestSha256") != _manifest_section_sha256(unapproved)
        or transaction.get("sourcesSha256")
        != _manifest_section_sha256(unapproved.get("sources"))
        or transaction.get("enrichmentsSha256")
        != _manifest_section_sha256(unapproved.get("enrichments"))
        or transaction.get("institutionsSha256")
        != unapproved.get("institutionsSha256")
        or transaction.get("sitesSha256") != unapproved.get("sitesSha256")
        or transaction.get("previousSnapshotId")
        != diff.get("previousSnapshotId")
    ):
        raise SnapshotQualityError("build transaction attestation mismatch")
    if manifest.get("approved") is True and (
        manifest.get("approvedAt") != transaction.get("approvedAt")
        or manifest.get("approvedByRole") != transaction.get("approvedByRole")
        or transaction.get("approvedManifestSha256")
        != _manifest_section_sha256(manifest)
    ):
        raise SnapshotQualityError("build transaction approval phase mismatch")


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise SnapshotQualityError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonstandard_json_constant(value: str) -> object:
    raise SnapshotQualityError(f"nonstandard JSON constant: {value}")


def _recheck_candidate(
    path: Path,
    manifest: dict[str, object],
    snapshot_id: str,
) -> tuple[list[Institution], list[InstitutionSite]]:
    if manifest.get("snapshotId") != snapshot_id:
        raise SnapshotQualityError("candidate snapshot ID mismatch")
    institution_path = _validated_snapshot_file(
        path / "institutions.jsonl",
        path,
        "institutions.jsonl",
    )
    site_path = _validated_snapshot_file(
        path / "sites.jsonl",
        path,
        "sites.jsonl",
    )
    institution_bytes = institution_path.read_bytes()
    site_bytes = site_path.read_bytes()
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
            decoded = _strict_json_loads(line)
            if type(decoded) is not dict:
                raise SnapshotQualityError(
                    f"candidate {label} row is not an object"
                )
            expected_fields = (
                _INSTITUTION_FIELDS if model is Institution else _SITE_FIELDS
            )
            if set(decoded) != expected_fields:
                raise SnapshotQualityError(
                    f"candidate {label} fields are not canonical"
                )
            records.append(model.model_validate_json(line))
        except SnapshotQualityError:
            raise
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
    actual_matches = _persisted_possible_matches(institutions, sites)
    if (
        manifest.get("possibleMatchCount") != len(actual_matches)
        or manifest.get("possibleMatches") != actual_matches
    ):
        raise SnapshotQualityError(
            "candidate possibleMatches do not match persisted records"
        )


def _recheck_source_provenance(
    manifest: dict[str, object],
    institutions: list[Institution],
    sites: list[InstitutionSite],
) -> None:
    source_entries = manifest.get("sources")
    if type(source_entries) is not list:
        raise SnapshotQualityError("candidate source provenance is invalid")
    by_source: dict[str, list[Institution]] = defaultdict(list)
    for institution in institutions:
        by_source[institution.source].append(institution)
    sites_by_parent: dict[str, list[InstitutionSite]] = defaultdict(list)
    for site in sites:
        sites_by_parent[site.institution_id].append(site)
    for entry in source_entries:
        if type(entry) is not dict:
            raise SnapshotQualityError("candidate source provenance is invalid")
        source = entry.get("source")
        if type(source) is not str or source not in by_source:
            raise SnapshotQualityError("candidate source provenance is invalid")
        rows = by_source[source]
        current_count = sum(
            row.status is not InstitutionStatus.MISSING_FROM_SOURCE for row in rows
        )
        current_rows = [
            row
            for row in rows
            if row.status is not InstitutionStatus.MISSING_FROM_SOURCE
        ]
        preserved_count = len(rows) - current_count
        timing = entry.get("requestTiming")
        raw_counts_value = entry.get("sourceObservationDateCounts")
        normalized_counts_value = entry.get("normalizedObservationDateCounts")
        preserved_counts_value = entry.get("preservedObservationDateCounts")
        if (
            type(raw_counts_value) is not dict
            or type(normalized_counts_value) is not dict
            or type(preserved_counts_value) is not dict
        ):
            raise SnapshotQualityError(
                "candidate source observation date counts are invalid"
            )
        raw_counts = tuple(raw_counts_value.items())
        normalized_counts = tuple(normalized_counts_value.items())
        preserved_counts = tuple(preserved_counts_value.items())
        fetched_row_count_value = entry.get("fetchedRowCount")
        checked_fetched_row_count = (
            fetched_row_count_value
            if type(fetched_row_count_value) is int
            else -1
        )
        try:
            validate_observation_date_counts(
                raw_counts,
                expected_total=checked_fetched_row_count,
                label="candidate raw observation",
            )
            validate_observation_date_counts(
                normalized_counts,
                expected_total=current_count,
                label="candidate normalized observation",
            )
            validate_observation_date_counts(
                preserved_counts,
                expected_total=preserved_count,
                label="candidate preserved observation",
            )
            actual_normalized_counts = observation_date_counts(
                row.source_as_of for row in current_rows
            )
            actual_preserved_counts = observation_date_counts(
                row.source_as_of
                for row in rows
                if row.status is InstitutionStatus.MISSING_FROM_SOURCE
            )
        except SourceDataError as exc:
            raise SnapshotQualityError(
                "candidate source observation date counts are invalid"
            ) from exc
        if (
            normalized_counts != actual_normalized_counts
            or preserved_counts != actual_preserved_counts
            or entry.get("sourceAsOf") != source_as_of_for(raw_counts)
        ):
            raise SnapshotQualityError(
                "candidate source observation dates do not match persisted rows"
            )
        source_normalized_matches = current_count == 0 or (
            entry.get("sourceNormalizedSha256")
            == _normalized_persisted_source_sha256(
                current_rows,
                sites_by_parent,
                before_enrichment=True,
            )
        )
        if (
            entry.get("endpoint") != _SOURCE_ENDPOINTS[source]
            or entry.get("licenseName") != _SOURCE_LICENSES[source]
            or entry.get("attribution") != _SOURCE_ATTRIBUTIONS[source]
            or entry.get("requestRegionCode") != _EXPECTED_REGION_CODES[source]
            or not _source_pagination_is_valid(
                source,
                entry.get("pageCount"),
                entry.get("fetchedRowCount"),
            )
            or entry.get("normalizedRowCount") != current_count
            or entry.get("preservedRowCount") != preserved_count
            or not source_normalized_matches
            or entry.get("normalizedSha256")
            != _normalized_persisted_source_sha256(rows, sites_by_parent)
            or type(entry.get("rawSha256")) is not str
            or _SHA256.fullmatch(entry["rawSha256"]) is None
            or source in _PINNED_SOURCE_RAW_SHA256
            and entry["rawSha256"] != _PINNED_SOURCE_RAW_SHA256[source]
            or source in _PINNED_SOURCE_NORMALIZED_SHA256
            and entry.get("sourceNormalizedSha256")
            != _PINNED_SOURCE_NORMALIZED_SHA256[source]
        ):
            raise SnapshotQualityError(
                "candidate source provenance does not match persisted rows"
            )
        if not _persisted_unclassified_provenance_matches(source, entry, rows):
            raise SnapshotQualityError(
                "candidate unclassified source provenance does not match records"
            )
        if source == "KINDERGARTEN_INFO":
            if (
                type(timing) is not str
                or re.fullmatch(r"\d{4}[12]", timing) is None
                or _kindergarten_timing_date(timing) != entry.get("sourceAsOf")
                or current_count > 0
                and entry["fetchedRowCount"] != current_count
            ):
                raise SnapshotQualityError(
                    "candidate source provenance timing/count is invalid"
                )
        elif timing is not None:
            raise SnapshotQualityError(
                "candidate source provenance timing is invalid"
            )
        if source == "NEIS" and entry["fetchedRowCount"] == 5:
            raise SnapshotQualityError("candidate source provenance is a sample")
        if (
            source == "NEIS"
            and current_count > 0
            and not current_count
            <= entry["fetchedRowCount"]
            <= current_count + 50
        ):
            raise SnapshotQualityError(
                "candidate source provenance count is invalid"
            )
        if (
            source == "SEN_REVIEWED_CSV"
            and current_count > 0
            and entry["fetchedRowCount"] != current_count + 1
        ):
            raise SnapshotQualityError(
                "candidate source provenance count is invalid"
            )


def _persisted_unclassified_provenance_matches(
    source: str,
    entry: Mapping[str, object],
    rows: list[Institution],
) -> bool:
    declared_counts = entry.get("unclassifiedSchoolKindCounts")
    declared_sha256 = entry.get("unclassifiedSchoolPolicySha256")
    if type(declared_counts) is not dict:
        return False
    if (
        list(declared_counts) != sorted(declared_counts)
        or any(type(label) is not str or not label.strip() for label in declared_counts)
        or any(type(count) is not int or count <= 0 for count in declared_counts.values())
    ):
        return False
    if source != "NEIS":
        return declared_counts == {} and declared_sha256 is None
    unclassified_count = sum(
        row.institution_type == "UNCLASSIFIED_SCHOOL" for row in rows
    )
    if not unclassified_count:
        return declared_counts == {} and declared_sha256 is None
    if (
        sum(declared_counts.values()) != unclassified_count
        or declared_sha256 != PINNED_POLICY_SHA256
    ):
        return False
    try:
        NeisUnclassifiedPolicy(
            counts=tuple(declared_counts.items()),
            sha256=declared_sha256,
            reviewed_as_of="2026-08-13",
            reviewer_role="data-steward",
        )
    except ValueError:
        return False
    return True


def _recheck_enrichment_provenance(
    manifest: dict[str, object],
    institutions: list[Institution],
    sites: list[InstitutionSite],
) -> None:
    institutions_by_id = {
        institution.institution_id: institution for institution in institutions
    }
    current_sites = [
        site
        for site in sites
        if site.status is not InstitutionStatus.MISSING_FROM_SOURCE
        and institutions_by_id[site.institution_id].status
        is not InstitutionStatus.MISSING_FROM_SOURCE
    ]
    entries = manifest.get("enrichments")
    if type(entries) is not list:
        raise SnapshotQualityError("candidate enrichment provenance is invalid")
    by_source: dict[str, dict[str, object]] = {}
    for entry in entries:
        if type(entry) is not dict:
            raise SnapshotQualityError("candidate enrichment provenance is invalid")
        source = entry.get("source")
        if type(source) is not str or source in by_source:
            raise SnapshotQualityError("candidate enrichment provenance is invalid")
        by_source[source] = entry
    official_count = sum(
        site.coordinate_quality == "OFFICIAL_STANDARD_COORDINATE"
        for site in current_sites
    )
    official_total = sum(
        site.coordinate_quality == "OFFICIAL_STANDARD_COORDINATE" for site in sites
    )
    official_preserved = official_total - official_count
    geocoded_count = sum(
        site.coordinate_quality == "GEOCODED" for site in current_sites
    )
    geocoded_total = sum(
        site.coordinate_quality == "GEOCODED" for site in sites
    )
    geocoded_preserved = geocoded_total - geocoded_count
    if official_total and "OFFICIAL_STANDARD_SCHOOL_LOCATION" not in by_source:
        raise SnapshotQualityError(
            "candidate enrichment provenance is missing official locations"
        )
    if geocoded_total and "KAKAO_LOCAL_GEOCODING" not in by_source:
        raise SnapshotQualityError(
            "candidate enrichment provenance is missing Kakao"
        )
    standard = by_source.pop("OFFICIAL_STANDARD_SCHOOL_LOCATION", None)
    if standard is not None and (
        standard.get("endpoint") != STANDARD_LOCATION_ENDPOINT
        or standard.get("licenseName") != "PUBLIC_DATA_NO_USE_RESTRICTION"
        or standard.get("attribution")
        != "Korea Education Facilities Safety Authority"
        or standard.get("sourceAsOf") != STANDARD_LOCATION_SOURCE_AS_OF
        or standard.get("rawSha256") != STANDARD_LOCATION_RAW_SHA256
        or standard.get("sourceNormalizedSha256")
        != _STANDARD_LOCATION_NORMALIZED_SHA256
        or standard.get("normalizedSha256")
        != _enrichment_sites_sha256(
            sites,
            "OFFICIAL_STANDARD_COORDINATE",
        )
        or standard.get("requestRegionCode") != "7010000"
        or standard.get("requestTiming") is not None
        or standard.get("pageCount") != 1
        or standard.get("fetchedRowCount") != PINNED_NATIONWIDE_COUNT
        or standard.get("matchedRowCount") != official_count
        or standard.get("preservedMatchedRowCount") != official_preserved
        or standard.get("rowCount") != official_total
    ):
        raise SnapshotQualityError(
            "candidate enrichment provenance is invalid"
        )
    kakao = by_source.pop("KAKAO_LOCAL_GEOCODING", None)
    kakao_page_count = kakao.get("pageCount") if kakao is not None else None
    kakao_raw_sha256 = kakao.get("rawSha256") if kakao is not None else None
    kakao_source_normalized_matches = kakao is not None and (
        geocoded_count == 0
        or kakao.get("sourceNormalizedSha256")
        == _geocoded_sites_sha256(current_sites)
    )
    if kakao is not None and (
        kakao.get("endpoint") != _KAKAO_ENDPOINT
        or kakao.get("licenseName") != "KAKAO_LOCAL_API_TERMS"
        or kakao.get("attribution") != "Kakao Local API"
        or kakao.get("requestRegionCode") != "SEOUL_ADDRESS_BATCH"
        or kakao.get("requestTiming") is not None
        or type(kakao_page_count) is not int
        or kakao_page_count <= 0
        or kakao.get("fetchedRowCount") != kakao_page_count
        or kakao.get("matchedRowCount") != geocoded_count
        or kakao.get("preservedMatchedRowCount") != geocoded_preserved
        or kakao.get("rowCount") != geocoded_total
        or not kakao_source_normalized_matches
        or kakao.get("normalizedSha256")
        != _enrichment_sites_sha256(sites, "GEOCODED")
        or type(kakao_raw_sha256) is not str
        or _SHA256.fullmatch(kakao_raw_sha256) is None
    ):
        raise SnapshotQualityError("candidate enrichment provenance is invalid")
    if by_source:
        raise SnapshotQualityError("candidate enrichment provenance is unsupported")


def _normalized_persisted_source_sha256(
    rows: list[Institution],
    sites_by_parent: Mapping[str, list[InstitutionSite]],
    *,
    before_enrichment: bool = False,
) -> str:
    records: list[SourceInstitutionRecord] = []
    for row in rows:
        parent_sites = [
            site
            for site in sites_by_parent.get(row.institution_id, [])
            if not (
                before_enrichment
                and site.status is InstitutionStatus.MISSING_FROM_SOURCE
            )
        ]
        source_sites: list[SourceInstitutionSiteRecord] = []
        prefix = f"{row.institution_id}:"
        for site in parent_sites:
            if not site.site_id.startswith(prefix):
                raise SnapshotQualityError(
                    "candidate site ID does not match its institution"
                )
            site_code = site.site_id.removeprefix(prefix)
            if _SAFE_SITE_CODE.fullmatch(site_code) is None:
                raise SnapshotQualityError("candidate site code is invalid")
            source_sites.append(
                SourceInstitutionSiteRecord(
                    site_code=site_code,
                    site_name=site.site_name,
                    road_address=site.road_address,
                    district=site.district,
                    latitude=site.latitude,
                    longitude=site.longitude,
                    coordinate_quality=site.coordinate_quality,
                )
            )
        main_sites = [site for site in source_sites if site.site_code == "main"]
        if len(main_sites) != 1:
            raise SnapshotQualityError(
                "candidate source row must have one persisted main site"
            )
        main = main_sites[0]
        records.append(
            SourceInstitutionRecord(
                institution_id=row.institution_id,
                official_name=row.official_name,
                institution_type=row.institution_type,
                foundation_type=row.foundation_type,
                education_office=row.education_office,
                road_address=main.road_address,
                district=main.district,
                latitude=main.latitude,
                longitude=main.longitude,
                source=row.source,
                source_region_code=row.source_region_code,
                source_as_of=row.source_as_of,
                coordinate_quality=main.coordinate_quality,
                site_name=main.site_name,
                additional_sites=tuple(
                    sorted(
                        (
                            site
                            for site in source_sites
                            if site.site_code != "main"
                        ),
                        key=lambda item: item.site_code,
                    )
                ),
            )
        )
    if before_enrichment:
        records = [_record_before_enrichment(record) for record in records]
    return normalized_records_sha256(records)


def _geocoded_sites_sha256(sites: list[InstitutionSite]) -> str:
    values = [
        {
            "road_address": site.road_address,
            "latitude": site.latitude,
            "longitude": site.longitude,
            "confidence": "EXACT_ROAD_ADDRESS",
        }
        for site in sites
        if site.coordinate_quality == "GEOCODED"
    ]
    values.sort(
        key=lambda item: (
            str(item["road_address"]),
            cast(float, item["latitude"]),
            cast(float, item["longitude"]),
        )
    )
    return hashlib.sha256(
        json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _persisted_possible_matches(
    institutions: list[Institution],
    sites: list[InstitutionSite],
) -> list[dict[str, object]]:
    institutions_by_id = {
        institution.institution_id: institution for institution in institutions
    }
    grouped: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for site in sites:
        institution = institutions_by_id[site.institution_id]
        key = (
            "".join(institution.official_name.split()),
            "".join(site.road_address.split()),
        )
        grouped[key][institution.institution_id] = institution.source
    pairs: set[tuple[str, str]] = set()
    for identities in grouped.values():
        sorted_ids = sorted(identities)
        for index, left in enumerate(sorted_ids):
            for right in sorted_ids[index + 1 :]:
                if identities[left] != identities[right]:
                    pairs.add((left, right))
    return [
        {
            "institutionIds": [left, right],
            "reason": "EXACT_NORMALIZED_NAME_AND_ADDRESS",
        }
        for left, right in sorted(pairs)
    ]


def _validate_unapproved_manifest_schema(manifest: dict[str, object]) -> None:
    if (
        manifest.get("approved") is not False
        or manifest.get("approvedAt") is not None
        or manifest.get("approvedByRole") is not None
    ):
        raise SnapshotQualityError("unapproved manifest approval fields are invalid")
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


def _validate_approved_manifest_schema(manifest: dict[str, object]) -> None:
    try:
        parsed = SnapshotManifest.model_validate_json(
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        )
    except ValidationError as exc:
        raise SnapshotQualityError("approved manifest schema is invalid") from exc
    if parsed.approved_by_role != "data-steward":
        raise SnapshotQualityError("approved manifest role is invalid")


def _recheck_promotion_quality(
    root: Path,
    manifest: dict[str, object],
    institutions: list[Institution],
    sites: list[InstitutionSite],
    coverage: CoverageService,
) -> None:
    institutions_by_id = {
        institution.institution_id: institution for institution in institutions
    }
    for institution in institutions:
        _validate_persisted_institution(institution)
        if institution.institution_type == "UNCLASSIFIED_SCHOOL" and (
            institution.status is not InstitutionStatus.REVIEW_REQUIRED
            or institution.status_source != "OFFICIAL_CLASSIFICATION_PENDING"
        ):
            raise SnapshotQualityError(
                "unclassified institution must remain pending official classification"
            )
    if any(
        site.coordinate_quality not in _ALLOWED_COORDINATE_QUALITIES
        for site in sites
    ):
        raise SnapshotQualityError("unsupported coordinate quality")
    for site in sites:
        if (
            institutions_by_id[site.institution_id].institution_type
            == "UNCLASSIFIED_SCHOOL"
            and site.status is not InstitutionStatus.REVIEW_REQUIRED
        ):
            raise SnapshotQualityError(
                "unclassified institution sites must remain review required"
            )
        if site.status is not InstitutionStatus.ACTIVE:
            continue
        if (
            site.latitude is None
            or site.longitude is None
            or site.routing_anchor_latitude is None
            or site.routing_anchor_longitude is None
        ):
            raise SnapshotQualityError(
                "persisted ACTIVE site has no complete coordinate anchors"
            )
        coordinate_state = coverage.classify(
            Coordinate(latitude=site.latitude, longitude=site.longitude)
        )
        anchor_state = coverage.classify(
            Coordinate(
                latitude=site.routing_anchor_latitude,
                longitude=site.routing_anchor_longitude,
            )
        )
        if (
            not _is_seoul_address(site.road_address)
            or coordinate_state is not CoverageState.SEOUL
            or anchor_state is not CoverageState.SEOUL
        ):
            raise SnapshotQualityError(
                "persisted ACTIVE site failed Seoul coverage replay"
            )
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
    sites_by_parent: dict[str, list[InstitutionSite]] = defaultdict(list)
    for site in sites:
        sites_by_parent[site.institution_id].append(site)
    for institution in current:
        parent_sites = sites_by_parent[institution.institution_id]
        if parent_sites and (
            sum(site.is_default for site in parent_sites) != 1
            or not any(
                site.is_default
                and site.site_id == f"{institution.institution_id}:main"
                for site in parent_sites
            )
        ):
            raise SnapshotQualityError(
                "candidate institution sites need one exact default main site"
            )
        if institution.status is InstitutionStatus.ACTIVE and not any(
            site.is_default and site.status is InstitutionStatus.ACTIVE
            for site in parent_sites
        ):
            raise SnapshotQualityError(
                "active candidate institution needs an active default site"
            )
    selectable_current = [
        institution
        for institution in current
        if institution.institution_type != "UNCLASSIFIED_SCHOOL"
    ]
    selectable_active = [
        institution
        for institution in active
        if institution.institution_type != "UNCLASSIFIED_SCHOOL"
    ]
    coordinate_rate = (
        len(selectable_active) / len(selectable_current)
        if selectable_current
        else 1.0
    )
    if coordinate_rate < 0.98:
        raise SnapshotQualityError(
            "coordinate validation success rate is below 98 percent"
        )

    diff = manifest.get("diff")
    if type(diff) is not dict:
        raise SnapshotQualityError("candidate diff metadata is invalid")
    verified_current: VerifiedSnapshot | None = None
    if (root / "current.json").exists():
        try:
            verified_current = verify_snapshot(root)
        except (OSError, ValueError) as exc:
            raise SnapshotQualityError(
                "current snapshot cannot be verified"
            ) from exc
    if (
        verified_current is not None
        and verified_current.manifest.snapshot_id == manifest.get("snapshotId")
    ):
        return
    previous_snapshot_id = diff.get("previousSnapshotId")
    if previous_snapshot_id is None:
        if verified_current is not None:
            raise SnapshotQualityError(
                "candidate previous snapshot ID is missing"
            )
        return
    if type(previous_snapshot_id) is not str:
        raise SnapshotQualityError("candidate previous snapshot ID is invalid")
    if verified_current is None:
        raise SnapshotQualityError(
            "candidate previous snapshot cannot be verified"
        )
    previous = verified_current
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


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
