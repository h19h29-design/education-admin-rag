"""Create the exact, allowlisted Docker build context for a release."""

import argparse
import shutil
from pathlib import Path

from app.institutions.snapshot import verify_snapshot
from app.policy.coverage import verify_geodata_resources
from app.policy.rules import RuleRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage a verified, minimal Docker context for one release."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser.parse_args()


def stage_release_context(source_root: Path, destination: Path) -> str:
    """Verify release artifacts then copy only files Docker is allowed to receive."""

    source = _resolve_directory(source_root, "source")
    if destination.exists():
        raise ValueError("release context destination must not exist")

    resources = source / "resources"
    verified_snapshot = verify_snapshot(resources / "institution-snapshots")
    verify_geodata_resources(resources / "geodata", verify_source=True)
    RuleRepository.from_directory(resources / "rules", require_hashes=True)

    destination.mkdir(mode=0o700, parents=True)
    try:
        for relative_path in ("Dockerfile", "pyproject.toml", "uv.lock"):
            _copy_file(source, destination, relative_path)
        _copy_tree(source, destination, "app")
        _copy_tree(source, destination, "resources/rules")
        for relative_path in (
            "resources/geodata/manifest.json",
            "resources/geodata/seoul.geojson",
            "resources/geodata/seoul-plus-12km.geojson",
            "resources/institution-snapshots/current.json",
            (
                "resources/institution-snapshots/"
                f"{verified_snapshot.manifest.snapshot_id}"
            ),
        ):
            candidate = source / relative_path
            if candidate.is_dir():
                _copy_tree(source, destination, relative_path)
            else:
                _copy_file(source, destination, relative_path)
    except Exception:
        # The caller chose a fresh path, so cleanup cannot affect unrelated data.
        shutil.rmtree(destination)
        raise
    return verified_snapshot.manifest.snapshot_id


def _resolve_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"release context {label} does not exist") from exc
    if not resolved.is_dir():
        raise ValueError(f"release context {label} must be a directory")
    return resolved


def _copy_file(source_root: Path, destination: Path, relative_path: str) -> None:
    source = source_root / relative_path
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"release context file is invalid: {relative_path}")
    target = destination / relative_path
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)


def _copy_tree(source_root: Path, destination: Path, relative_path: str) -> None:
    source = source_root / relative_path
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"release context directory is invalid: {relative_path}")
    target_root = destination / relative_path
    target_root.mkdir(mode=0o700, parents=True)
    for candidate in sorted(source.rglob("*")):
        if candidate.is_symlink():
            raise ValueError(f"release context symlink is invalid: {relative_path}")
        relative = candidate.relative_to(source)
        target = target_root / relative
        if candidate.is_dir():
            target.mkdir(mode=0o700, exist_ok=True)
        elif candidate.is_file():
            shutil.copy2(candidate, target, follow_symlinks=False)
        else:
            raise ValueError(f"release context file is invalid: {relative_path}")


def main() -> int:
    args = parse_args()
    snapshot_id = stage_release_context(args.source, args.destination)
    print(snapshot_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
