from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.corpus.legacy import (
    CanonicalTitleEntry,
    LegacyError,
    build_legacy_map,
    load_legacy_index,
    write_legacy_report,
)


def _legacy_html(cases: list[dict[str, object]]) -> str:
    payload = json.dumps({"cases": cases}, ensure_ascii=False, separators=(",", ":"))
    return f"<script>window.APP = {payload};</script>"


def test_legacy_loader_retains_only_id_title_and_year(tmp_path: Path) -> None:
    """Catches legacy body text being promoted into the mapping boundary."""
    sentinel = "PRIVATE_LEGACY_BODY_SENTINEL"
    source = tmp_path / "legacy.html"
    source.write_text(
        _legacy_html(
            [
                {
                    "id": "AC-2020-001",
                    "title": "학교 회계 제목",
                    "year": "2020",
                    "body": sentinel,
                    "laws": [sentinel],
                }
            ]
        ),
        encoding="utf-8",
    )

    entries = load_legacy_index(source)

    assert len(entries) == 1
    assert entries[0].legacy_id == "AC-2020-001"
    assert entries[0].title == "학교 회계 제목"
    assert entries[0].edition_year == 2020
    assert sentinel not in repr(entries)


def test_legacy_loader_accepts_reviewed_yearless_domain_ids(tmp_path: Path) -> None:
    """Catches 2021+ launcher IDs being dropped because only 2020 embeds the year."""
    source = tmp_path / "legacy.html"
    source.write_text(
        _legacy_html([{"id": "HR-019", "title": "인사 제목", "year": "2021"}]),
        encoding="utf-8",
    )

    entries = load_legacy_index(source)

    assert [(item.legacy_id, item.edition_year) for item in entries] == [
        ("HR-019", 2021)
    ]


def test_legacy_loader_rejects_duplicate_ids_without_retaining_values(
    tmp_path: Path,
) -> None:
    """Catches duplicate legacy IDs making the reverse map non-functional."""
    sentinel = "PRIVATE_DUPLICATE_LEGACY_SENTINEL"
    source = tmp_path / "legacy.html"
    source.write_text(
        _legacy_html(
            [
                {"id": "AC-2020-001", "title": sentinel, "year": "2020"},
                {"id": "AC-2020-001", "title": "다른 제목", "year": "2020"},
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(LegacyError, match="legacy_index_invalid") as captured:
        load_legacy_index(source)

    rendered = "".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.__cause__),
            repr(captured.value.__context__),
        )
    )
    assert sentinel not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_legacy_loader_rejects_symlink_and_fifo_without_blocking(
    tmp_path: Path,
) -> None:
    """Catches untrusted legacy paths redirecting or blocking the release process."""
    real = tmp_path / "real.html"
    real.write_text(
        _legacy_html([{"id": "HR-019", "title": "인사 제목", "year": "2021"}]),
        encoding="utf-8",
    )
    link = tmp_path / "link.html"
    link.symlink_to(real)
    with pytest.raises(LegacyError, match="legacy_index_invalid"):
        load_legacy_index(link)

    fifo = tmp_path / "legacy.fifo"
    os.mkfifo(fifo)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from src.corpus.legacy import LegacyError,load_legacy_index; "
                f"p=Path({str(fifo)!r}); "
                "\ntry: load_legacy_index(p)"
                "\nexcept LegacyError: raise SystemExit(0)"
                "\nraise SystemExit(1)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == ""


def test_legacy_mapping_is_unique_and_ambiguous_titles_require_review(
    tmp_path: Path,
) -> None:
    """Catches title collisions being silently mapped to an arbitrary case."""
    source = tmp_path / "legacy.html"
    source.write_text(
        _legacy_html(
            [
                {"id": "AC-2020-001", "title": "고유 제목", "year": "2020"},
                {"id": "AC-2020-002", "title": "중복 제목", "year": "2020"},
            ]
        ),
        encoding="utf-8",
    )
    legacy = load_legacy_index(source)
    targets = (
        CanonicalTitleEntry(
            case_id="case-2020-contract-general-001",
            title="고유 제목",
            edition_year=2020,
        ),
        CanonicalTitleEntry(
            case_id="case-2020-contract-general-002",
            title="중복 제목",
            edition_year=2020,
        ),
        CanonicalTitleEntry(
            case_id="case-2020-contract-general-003",
            title="중복 제목",
            edition_year=2020,
        ),
    )

    report = build_legacy_map(legacy, targets)

    assert [
        (item.legacy_id, item.case_id, item.review_status) for item in report.items
    ] == [
        ("AC-2020-001", "case-2020-contract-general-001", "pending"),
        ("AC-2020-002", None, "ambiguous"),
    ]
    assert report.mapped_count == 1
    assert report.ambiguous_count == 1
    assert report.unmapped_count == 0

    output = write_legacy_report(
        tmp_path / "reports",
        "corpus-20250808123456-deadbeef",
        report,
    )
    rows = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert set(rows[0]) == {
        "case_id",
        "edition_year",
        "legacy_id",
        "mapping_confidence",
        "review_status",
        "title",
    }
    assert rows[0]["title"] == "고유 제목"


def test_legacy_boundaries_revalidate_forged_models(tmp_path: Path) -> None:
    """Catches caller-constructed model instances bypassing strict field validators."""
    forged_target = CanonicalTitleEntry.model_construct(
        case_id="not-canonical",
        title="PRIVATE_FORGED_LEGACY_SENTINEL",
        edition_year=2020,
    )
    with pytest.raises(LegacyError, match="legacy_mapping_invalid") as captured:
        build_legacy_map((), (forged_target,))
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
