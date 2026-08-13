import argparse
import json
import sys
from pathlib import Path

from app.institutions.sync import SnapshotQualityError, approve_candidate_snapshot
from app.policy.coverage import CoverageService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Approve an independently reviewed institution snapshot."
    )
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--review-digest", required=True)
    parser.add_argument("--reviewer-role", required=True)
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        coverage = CoverageService.from_geojson(
            seoul_path=args.geodata_root / "seoul.geojson",
            buffer_distance_m=12_000,
        )
        review_digest = approve_candidate_snapshot(
            snapshot_id=args.snapshot_id,
            review_digest=args.review_digest,
            reviewer_role=args.reviewer_role,
            snapshot_root=args.snapshot_root,
            coverage=coverage,
        )
    except (SnapshotQualityError, OSError, ValueError):
        print("institution snapshot approval failed", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "reviewDigest": review_digest,
                "snapshotId": args.snapshot_id,
                "status": "SNAPSHOT_APPROVED",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
