import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
from app.environment import EnvironmentFileError, load_environment_file
from app.institutions.snapshot import verify_snapshot
from app.institutions.sources.common import EnrichmentProvenance, SourceDataError
from app.institutions.sources.kindergarten import KindergartenSource
from app.institutions.sources.neis import NeisSource
from app.institutions.sources.sen import SenCsvSource
from app.institutions.sources.sen_counts import load_reviewed_school_counts
from app.institutions.sources.standard_school import (
    StandardSchoolLocationSource,
    enrich_neis_coordinates,
)
from app.institutions.sync import (
    SnapshotQualityError,
    build_candidate_snapshot,
    build_sync_preflight_audit,
    emit_sync_preflight_audit,
    enrichment_records_sha256,
    geocode_missing_records,
    reconcile_selectable_school_counts,
)
from app.policy.coverage import CoverageService
from app.providers.kakao_local import KakaoLocalClient

_REQUIRED_KEYS = (
    "NEIS_API_KEY",
    "KINDERGARTEN_API_KEY",
    "KAKAO_REST_API_KEY",
)
_SEN_COUNTS = {
    "HEADQUARTERS": 1,
    "DISTRICT_OFFICE": 11,
    "DIRECT_AGENCY": 8,
    "LIFELONG_LEARNING_CENTER": 4,
    "LIBRARY": 17,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and atomically promote a Seoul education institution snapshot."
    )
    parser.add_argument(
        "--sen-csv",
        type=Path,
        default=Path("apps/travel-map/resources/institution-sources/sen-institutions.csv"),
    )
    parser.add_argument(
        "--region-codes",
        type=Path,
        default=Path(
            "apps/travel-map/resources/institution-sources/"
            "kindergarten-region-codes.csv"
        ),
    )
    parser.add_argument(
        "--school-counts",
        type=Path,
        default=Path(
            "apps/travel-map/resources/institution-sources/"
            "sen-annual-school-counts.csv"
        ),
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=Path("apps/travel-map/resources/institution-snapshots"),
    )
    parser.add_argument(
        "--geodata-root",
        type=Path,
        default=Path("apps/travel-map/resources/geodata"),
    )
    parser.add_argument("--timing", default="20261")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--env-file", type=Path)
    return parser.parse_args()


async def run(args: argparse.Namespace, keys: dict[str, str]) -> None:
    credential_holders: list[
        NeisSource | KindergartenSource | KakaoLocalClient
    ] = []
    try:
        await _run_with_keys(args, keys, credential_holders)
    finally:
        for holder in credential_holders:
            holder.clear_credentials()
        for name in keys:
            keys[name] = ""


async def _run_with_keys(
    args: argparse.Namespace,
    keys: dict[str, str],
    credential_holders: list[
        NeisSource | KindergartenSource | KakaoLocalClient
    ],
) -> None:
    timeout = httpx.Timeout(5.0, connect=2.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        neis_source = NeisSource(api_key=keys["NEIS_API_KEY"], client=http)
        credential_holders.append(neis_source)
        kindergarten_source = KindergartenSource(
            api_key=keys["KINDERGARTEN_API_KEY"],
            client=http,
            region_codes_path=args.region_codes,
            timing=args.timing,
        )
        credential_holders.append(kindergarten_source)
        standard_source = StandardSchoolLocationSource(client=http)
        neis_result, kindergarten_result, standard_result = await asyncio.gather(
            neis_source.fetch(),
            kindergarten_source.fetch(),
            standard_source.fetch(),
        )
        sen_result = SenCsvSource(
            args.sen_csv,
            expected_type_counts=_SEN_COUNTS,
        ).load()
        neis_records = enrich_neis_coordinates(
            neis_result.records,
            standard_result.locations,
        )
        benchmark = load_reviewed_school_counts(args.school_counts)
        reconciliation = reconcile_selectable_school_counts(
            neis_result.records + kindergarten_result.records,
            benchmark=benchmark,
        )
        all_records = (
            neis_records + kindergarten_result.records + sen_result.records
        )
        source_provenance = {
            item.source: item
            for item in (
                neis_result.provenance,
                kindergarten_result.provenance,
                sen_result.provenance,
            )
        }
        emit_sync_preflight_audit(
            build_sync_preflight_audit(
                all_records,
                source_provenance=source_provenance,
                reconciliation=reconciliation,
            )
        )
        geocoder = KakaoLocalClient(
            api_key=keys["KAKAO_REST_API_KEY"],
            client=http,
        )
        credential_holders.append(geocoder)
        all_records = await geocode_missing_records(all_records, geocoder)
        geocoder.clear_credentials()

    previous = (
        verify_snapshot(args.snapshot_root)
        if (args.snapshot_root / "current.json").exists()
        else None
    )
    coverage = CoverageService.from_geojson(
        seoul_path=args.geodata_root / "seoul.geojson",
        buffer_distance_m=12_000,
    )
    snapshot_id = args.snapshot_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    standard_provenance = replace(
        standard_result.provenance,
        matched_row_count=sum(
            record.coordinate_quality == "OFFICIAL_STANDARD_COORDINATE"
            for record in neis_records
        ),
        matched_normalized_sha256=enrichment_records_sha256(
            neis_records,
            "OFFICIAL_STANDARD_COORDINATE",
        ),
    )
    kakao_provenance = geocoder.provenance()
    enrichments: tuple[EnrichmentProvenance, ...] = (standard_provenance,)
    if kakao_provenance.page_count:
        enrichments += (
            replace(
                kakao_provenance,
                matched_normalized_sha256=enrichment_records_sha256(
                    all_records,
                    "GEOCODED",
                ),
            ),
        )
    candidate = build_candidate_snapshot(
        records=all_records,
        previous=previous,
        output_root=args.snapshot_root,
        snapshot_id=snapshot_id,
        coverage=coverage,
        source_provenance=source_provenance,
        enrichment_provenance=enrichments,
    )
    if candidate.issues:
        raise SnapshotQualityError("; ".join(candidate.issues))
    print(
        json.dumps(
            {
                "snapshotId": snapshot_id,
                "status": "CANDIDATE_REVIEW_REQUIRED",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def main() -> int:
    args = parse_args()
    try:
        load_environment_file(args.env_file)
    except EnvironmentFileError:
        print("invalid environment file", file=sys.stderr)
        return 2
    missing = [
        name for name in _REQUIRED_KEYS if not os.environ.get(name, "").strip()
    ]
    if missing:
        print(
            "missing required environment keys: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    keys = {name: os.environ[name] for name in _REQUIRED_KEYS}
    try:
        asyncio.run(run(args, keys))
    except (SourceDataError, SnapshotQualityError, OSError, ValueError) as exc:
        for name in keys:
            keys[name] = ""
        print(f"institution sync failed: {exc}", file=sys.stderr)
        return 1
    for name in keys:
        keys[name] = ""
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
