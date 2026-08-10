import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
from app.institutions.snapshot import verify_snapshot
from app.institutions.sources.common import SourceDataError
from app.institutions.sources.kindergarten import KindergartenSource
from app.institutions.sources.neis import NeisSource
from app.institutions.sources.sen import SenCsvSource
from app.institutions.sources.standard_school import (
    StandardSchoolLocationSource,
    enrich_neis_coordinates,
)
from app.institutions.sync import (
    SnapshotQualityError,
    build_candidate_snapshot,
    geocode_missing_records,
    promote_snapshot,
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
    parser.add_argument("--sen-csv", type=Path, required=True)
    parser.add_argument("--region-codes", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, required=True)
    parser.add_argument("--geodata-root", type=Path, required=True)
    parser.add_argument("--timing", required=True)
    parser.add_argument("--snapshot-id")
    return parser.parse_args()


async def run(args: argparse.Namespace, keys: dict[str, str]) -> None:
    timeout = httpx.Timeout(5.0, connect=2.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        neis_source = NeisSource(api_key=keys["NEIS_API_KEY"], client=http)
        kindergarten_source = KindergartenSource(
            api_key=keys["KINDERGARTEN_API_KEY"],
            client=http,
            region_codes_path=args.region_codes,
            timing=args.timing,
        )
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
        all_records = (
            neis_records + kindergarten_result.records + sen_result.records
        )
        geocoder = KakaoLocalClient(
            api_key=keys["KAKAO_REST_API_KEY"],
            client=http,
        )
        all_records = await geocode_missing_records(all_records, geocoder)

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
    provenance = {
        item.source: item
        for item in (
            neis_result.provenance,
            kindergarten_result.provenance,
            sen_result.provenance,
        )
    }
    standard_provenance = replace(
        standard_result.provenance,
        matched_row_count=sum(
            record.coordinate_quality == "OFFICIAL_STANDARD_COORDINATE"
            for record in neis_records
        ),
    )
    candidate = build_candidate_snapshot(
        records=all_records,
        previous=previous,
        output_root=args.snapshot_root,
        snapshot_id=snapshot_id,
        coverage=coverage,
        source_provenance=provenance,
        enrichment_provenance=(standard_provenance, geocoder.provenance()),
    )
    if candidate.issues:
        raise SnapshotQualityError("; ".join(candidate.issues))
    promote_snapshot(candidate, args.snapshot_root)
    manifest = json.loads(
        (args.snapshot_root / snapshot_id / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    summary = {
        "snapshotId": snapshot_id,
        "institutionCount": manifest["institutionCount"],
        "siteCount": manifest["siteCount"],
        "quarantinedCount": manifest["quarantinedCount"],
        "sources": {
            source["source"]: source["rowCount"]
            for source in manifest["sources"]
        },
        "standardSchoolCoordinateRows": len(standard_result.locations),
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


def main() -> int:
    args = parse_args()
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
        print(f"institution sync failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
