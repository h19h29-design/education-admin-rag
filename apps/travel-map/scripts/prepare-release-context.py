"""Create the exact, allowlisted Docker build context for a release."""

import argparse
import json
import shutil
from pathlib import Path

from app.institutions.snapshot import verify_snapshot
from app.policy.coverage import verify_geodata_resources
from app.policy.rules import RuleRepository

_STATIC_SUFFIXES = frozenset(
    {
        ".css",
        ".html",
        ".jpeg",
        ".jpg",
        ".js",
        ".png",
        ".svg",
        ".webp",
        ".woff2",
    }
)
_EXCLUDED_APP_PATH_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "artifacts",
        "e2e",
        "node_modules",
        "playwright-report",
        "raw",
        "source",
        "test-results",
        "tests",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage a verified, minimal Docker context for one release."
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser.parse_args()


def stage_release_context(
    source_root: Path,
    destination: Path,
    *,
    allow_test_fixture: bool = False,
) -> str:
    """Verify release artifacts then copy only files Docker is allowed to receive."""

    source = _resolve_directory(source_root, "source")
    if destination.exists():
        raise ValueError("release context destination must not exist")

    resources = source / "resources"
    verified_snapshot = verify_snapshot(resources / "institution-snapshots")
    is_test_fixture = (
        verified_snapshot.manifest.approved_by_role == "TEST_FIXTURE_REVIEWER"
        and {source.source for source in verified_snapshot.manifest.sources}
        == {"TEST_NEIS"}
        and verified_snapshot.manifest.school_count_reconciliation is None
    )
    if is_test_fixture and not allow_test_fixture:
        raise ValueError("test institution snapshot cannot stage for production")
    if not is_test_fixture and (
        verified_snapshot.manifest.approved_by_role != "data-steward"
        or verified_snapshot.manifest.school_count_reconciliation is None
        or any(
            source.source == "TEST_NEIS"
            for source in verified_snapshot.manifest.sources
        )
    ):
        raise ValueError("institution snapshot is not approved for production")
    verify_geodata_resources(resources / "geodata", verify_source=True)
    RuleRepository.from_directory(resources / "rules", require_hashes=True)

    destination.mkdir(mode=0o700, parents=True)
    try:
        for relative_path in (".dockerignore", "Dockerfile", "pyproject.toml", "uv.lock"):
            _copy_file(source, destination, relative_path)
        _copy_application(source, destination)
        _copy_rules(source, destination)
        for relative_path in (
            "resources/geodata/manifest.json",
            "resources/geodata/seoul.geojson",
            "resources/geodata/seoul-plus-12km.geojson",
            "resources/institution-snapshots/current.json",
        ):
            _copy_file(source, destination, relative_path)
        snapshot_root = "resources/institution-snapshots"
        for filename in ("manifest.json", "institutions.jsonl", "sites.jsonl"):
            _copy_file(
                source,
                destination,
                f"{snapshot_root}/{verified_snapshot.manifest.snapshot_id}/{filename}",
            )
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
    if (
        source.is_symlink()
        or not source.is_file()
        or not _is_within(source, source_root)
    ):
        raise ValueError(f"release context file is invalid: {relative_path}")
    target = destination / relative_path
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.copy2(source, target, follow_symlinks=False)


def _copy_tree(source_root: Path, destination: Path, relative_path: str) -> None:
    source = source_root / relative_path
    if (
        source.is_symlink()
        or not source.is_dir()
        or not _is_within(source, source_root)
    ):
        raise ValueError(f"release context directory is invalid: {relative_path}")
    target_root = destination / relative_path
    target_root.mkdir(mode=0o700, parents=True)
    for candidate in sorted(source.rglob("*")):
        if candidate.is_symlink() or not _is_within(candidate, source_root):
            raise ValueError(f"release context symlink is invalid: {relative_path}")
        relative = candidate.relative_to(source)
        target = target_root / relative
        if candidate.is_dir():
            target.mkdir(mode=0o700, exist_ok=True)
        elif candidate.is_file():
            shutil.copy2(candidate, target, follow_symlinks=False)
        else:
            raise ValueError(f"release context file is invalid: {relative_path}")


def _copy_rules(source_root: Path, destination: Path) -> None:
    """Copy the verified rule index and only the files it authenticates."""

    index_path = source_root / "resources/rules/index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        entries = index["rules"]
        filenames = tuple(entry["file"] for entry in entries)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("release context rule index is invalid") from exc
    if any(type(filename) is not str for filename in filenames):
        raise ValueError("release context rule index is invalid")
    _copy_file(source_root, destination, "resources/rules/index.json")
    for filename in filenames:
        _copy_file(source_root, destination, f"resources/rules/{filename}")


def _copy_application(source_root: Path, destination: Path) -> None:
    """Copy only runtime Python modules and explicitly supported static assets."""

    app_root = source_root / "app"
    if (
        app_root.is_symlink()
        or not app_root.is_dir()
        or not _is_within(app_root, source_root)
    ):
        raise ValueError("release context directory is invalid: app")
    target_root = destination / "app"
    target_root.mkdir(mode=0o700, parents=True)
    for candidate in sorted(app_root.rglob("*")):
        if candidate.is_symlink() or not _is_within(candidate, source_root):
            raise ValueError("release context symlink is invalid: app")
        if not candidate.is_file() or not _is_allowed_application_file(candidate, app_root):
            continue
        relative_path = candidate.relative_to(app_root)
        target = target_root / relative_path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(candidate, target, follow_symlinks=False)


def _is_allowed_application_file(candidate: Path, app_root: Path) -> bool:
    relative_path = candidate.relative_to(app_root)
    if any(part in _EXCLUDED_APP_PATH_PARTS for part in relative_path.parts):
        return False
    if any(part.startswith(".") for part in relative_path.parts):
        return False
    if candidate.suffix == ".py":
        return True
    return relative_path.parts[0] == "static" and candidate.suffix in _STATIC_SUFFIXES


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        return candidate.resolve(strict=True).is_relative_to(root)
    except OSError:
        return False


def main() -> int:
    args = parse_args()
    snapshot_id = stage_release_context(args.source, args.destination)
    print(snapshot_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
