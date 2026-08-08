"""Regression tests for the source-document manifest boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf as fitz
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from src.cli import app
from src.ingestion.manifest import (
    ManifestError,
    PageNumberingPolicy,
    SourceDocument,
    load_manifest,
    page_label,
    resolve_source,
    verify_source,
)

MANIFEST_PATH = Path("data/manifests/sen_qa_sources.json")


def _write_pdf(path: Path, *, width: float = 612, height: float = 792) -> None:
    document = fitz.open()  # type: ignore[no-untyped-call]
    document.new_page(width=width, height=height)
    document.save(path)  # type: ignore[no-untyped-call]
    document.close()  # type: ignore[no-untyped-call]


def _expected_document(path: Path, **updates: object) -> SourceDocument:
    digest = hashlib.sha256(path.read_bytes() if path.exists() else b"").hexdigest()
    base: dict[str, object] = {
        "doc_id": "fixture-2020",
        "edition_year": 2020,
        "official_title": "Fixture source",
        "publisher": "Fixture publisher",
        "registration_no": None,
        "source_period_start": None,
        "source_period_end": None,
        "source_filename": path.name,
        "source_relpath": path.name,
        "sha256": digest,
        "pdf_page_count": 1,
        "page_size_profiles": (
            {"start_pdf_page": 1, "end_pdf_page": 1, "width_pt": 612.0, "height_pt": 792.0},
        ),
        "extraction_method": "native",
        "source_dpi": None,
        "render_dpi": None,
        "page_numbering": {"mode": "offset", "body_start_pdf_page": 1, "body_end_pdf_page": 1, "offset": 0},
        "official_public_url": None,
        "official_url_status": "unverified",
        "redistribution_status": "unverified",
        "access_level": "staff",
    }
    base.update(updates)
    return SourceDocument.model_validate(base)


def test_manifest_contains_exactly_2020_through_2025() -> None:
    """Catches a missing, duplicate, or wrongly ordered annual source."""
    docs = load_manifest(MANIFEST_PATH)
    assert [doc.edition_year for doc in docs] == [2020, 2021, 2022, 2023, 2024, 2025]
    assert [doc.pdf_page_count for doc in docs] == [302, 383, 386, 168, 324, 314]


def test_source_hash_mismatch_is_fatal(tmp_path: Path) -> None:
    """Catches accepting a source whose bytes changed after approval."""
    source = tmp_path / "book.pdf"
    _write_pdf(source)
    expected_doc = _expected_document(source, sha256="0" * 64)

    with pytest.raises(ManifestError, match="SHA-256 mismatch"):
        verify_source(source, expected_doc)


def test_source_filename_mismatch_is_fatal(tmp_path: Path) -> None:
    """Catches accepting an approved hash under an unexpected filename."""
    source = tmp_path / "renamed.pdf"
    _write_pdf(source)
    expected_doc = _expected_document(source).model_copy(update={"source_filename": "approved.pdf"})

    with pytest.raises(ManifestError, match="filename mismatch"):
        verify_source(source, expected_doc)


def test_source_page_count_mismatch_is_fatal(tmp_path: Path) -> None:
    """Catches accepting a truncated or expanded PDF."""
    source = tmp_path / "book.pdf"
    _write_pdf(source)
    expected_doc = _expected_document(
        source,
        pdf_page_count=2,
        page_size_profiles=(
            {"start_pdf_page": 1, "end_pdf_page": 2, "width_pt": 612.0, "height_pt": 792.0},
        ),
    )

    with pytest.raises(ManifestError, match="page-count mismatch"):
        verify_source(source, expected_doc)


def test_source_page_size_mismatch_is_fatal(tmp_path: Path) -> None:
    """Catches a same-page-count PDF with a changed page geometry."""
    source = tmp_path / "book.pdf"
    _write_pdf(source, width=600, height=792)
    expected_doc = _expected_document(source)

    with pytest.raises(ManifestError, match="page-size profile mismatch"):
        verify_source(source, expected_doc)


@pytest.mark.parametrize("source_relpath", ["../outside.pdf", "/tmp/outside.pdf"])
def test_source_path_cannot_escape_root(tmp_path: Path, source_relpath: str) -> None:
    """Catches traversal or absolute paths that escape the approved source root."""
    expected_doc = _expected_document(tmp_path / "placeholder.pdf").model_copy(
        update={"source_relpath": source_relpath}
    )

    with pytest.raises(ManifestError, match="source root"):
        resolve_source(tmp_path / "source", expected_doc)


def test_source_symlink_cannot_escape_root(tmp_path: Path) -> None:
    """Catches a source-root file symlinked to an external file."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    outside = tmp_path / "outside.pdf"
    _write_pdf(outside)
    (source_root / "book.pdf").symlink_to(outside)
    expected_doc = _expected_document(outside, source_filename="book.pdf", source_relpath="book.pdf")

    with pytest.raises(ManifestError, match="source root"):
        resolve_source(source_root, expected_doc)


def test_manifest_rejects_unknown_fields(tmp_path: Path) -> None:
    """Catches silently accepting misspelled or unreviewed manifest fields."""
    data = _expected_document(tmp_path / "book.pdf").model_dump(mode="json")
    data["unreviewed"] = True
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"documents": [data]}), encoding="utf-8")

    with pytest.raises(ValidationError, match="unreviewed"):
        load_manifest(manifest)


def test_manifest_rejects_noncontiguous_page_profiles(tmp_path: Path) -> None:
    """Catches an unverified PDF-page gap in a geometry profile."""
    data = _expected_document(tmp_path / "book.pdf").model_dump(mode="json")
    data["pdf_page_count"] = 3
    data["page_size_profiles"] = [
        {"start_pdf_page": 1, "end_pdf_page": 1, "width_pt": 612, "height_pt": 792},
        {"start_pdf_page": 3, "end_pdf_page": 3, "width_pt": 612, "height_pt": 792},
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"documents": [data]}), encoding="utf-8")

    with pytest.raises(ValidationError, match="contiguous"):
        load_manifest(manifest)


def test_page_label_returns_none_outside_body_and_never_negative() -> None:
    """Catches cover/TOC labels and negative labels leaking into citations."""
    policy = PageNumberingPolicy(mode="offset", body_start_pdf_page=7, body_end_pdf_page=12, offset=-6)

    assert page_label(policy, 1) is None
    assert page_label(policy, 7) == 1
    assert page_label(policy, 12) == 6
    assert page_label(policy, 13) is None
    assert page_label(policy, 0) is None


def test_verify_sources_requires_source_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches an intake run that defaults to an arbitrary local directory."""
    monkeypatch.delenv("SEN_QA_SOURCE_ROOT", raising=False)

    result = CliRunner().invoke(app, ["verify-sources", "--manifest", str(MANIFEST_PATH)])

    assert result.exit_code != 0
    assert "SEN_QA_SOURCE_ROOT" in result.stdout


def test_verify_sources_reports_mismatch_without_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a failed intake being reported as success or exposing PDF text."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    monkeypatch.setenv("SEN_QA_SOURCE_ROOT", str(source_root))

    result = CliRunner().invoke(app, ["verify-sources", "--manifest", str(MANIFEST_PATH)])

    assert result.exit_code != 0
    assert "verified=0" in result.stdout
    assert "failed=6" in result.stdout
