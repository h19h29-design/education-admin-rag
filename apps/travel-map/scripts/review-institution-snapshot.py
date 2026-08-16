import argparse
import json
import sys
from pathlib import Path

from app.institutions.sync import (
    SnapshotQualityError,
    build_candidate_review_packet,
)
from app.policy.coverage import CoverageService


def _preflight_arguments(parser: argparse.ArgumentParser) -> None:
    argument_index = 1
    while argument_index < len(sys.argv):
        if sys.argv[argument_index] in ("-h", "--help"):
            argument_index += 1
        elif sys.argv[argument_index] == "--snapshot-id":
            argument_index += 2
        elif sys.argv[argument_index].startswith("--snapshot-id="):
            argument_index += 1
        elif sys.argv[argument_index] == "--snapshot-root":
            argument_index += 2
        elif sys.argv[argument_index].startswith("--snapshot-root="):
            argument_index += 1
        elif sys.argv[argument_index] == "--geodata-root":
            argument_index += 2
        elif sys.argv[argument_index].startswith("--geodata-root="):
            argument_index += 1
        else:
            parser.error("invalid command arguments")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a credential-free administrator review packet."
    )
    parser.add_argument("--snapshot-id", required=True)
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
    _preflight_arguments(parser)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        coverage = CoverageService.from_geojson(
            seoul_path=args.geodata_root / "seoul.geojson",
            buffer_distance_m=12_000,
        )
        packet = build_candidate_review_packet(
            snapshot_id=args.snapshot_id,
            snapshot_root=args.snapshot_root,
            coverage=coverage,
        )
    except (SnapshotQualityError, OSError, ValueError):
        print("institution snapshot review failed", file=sys.stderr)
        return 1
    print(
        json.dumps(
            packet,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
