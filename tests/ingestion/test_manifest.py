"""Regression tests for the source-document manifest boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pymupdf as fitz
import pytest
from typer.testing import CliRunner, Result

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
EXPECTED_EDITION_YEARS = (2020, 2021, 2022, 2023, 2024, 2025)
SAFE_MANIFEST_ERROR = "manifest must contain exactly editions 2020 through 2025 in order"
SENSITIVE_SENTINEL = "PRIVATE CASE CONTENT"


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


def _manifest_payload() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _mutated_year_manifest(case: str) -> dict[str, object]:
    payload = _manifest_payload()
    documents = payload["documents"]
    assert isinstance(documents, list)
    if case == "truncated":
        payload["documents"] = documents[:-1]
    elif case == "replaced":
        replaced = dict(documents[-1])
        replaced["edition_year"] = 2026
        documents[-1] = replaced
    elif case == "duplicate":
        duplicate = dict(documents[-1])
        duplicate["edition_year"] = 2024
        documents[-1] = duplicate
    elif case == "reordered":
        documents[0], documents[1] = documents[1], documents[0]
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"unknown test case: {case}")
    return payload


def _assert_safe_cli_failure(result: Result, expected_summary: str) -> None:
    output = result.output
    assert result.exit_code != 0
    assert expected_summary in output
    assert "Traceback" not in output
    assert SENSITIVE_SENTINEL not in output


def test_manifest_contains_exactly_2020_through_2025() -> None:
    """Catches a missing, duplicate, or wrongly ordered annual source."""
    docs = load_manifest(MANIFEST_PATH)
    assert [doc.edition_year for doc in docs] == [2020, 2021, 2022, 2023, 2024, 2025]
    assert [doc.pdf_page_count for doc in docs] == [302, 383, 386, 168, 324, 314]


@pytest.mark.parametrize("case", ["truncated", "replaced", "duplicate", "reordered"])
def test_manifest_rejects_any_edition_sequence_except_the_approved_six(
    tmp_path: Path, case: str
) -> None:
    """Catches a partial, substituted, duplicate, or reordered annual corpus."""
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, _mutated_year_manifest(case))

    with pytest.raises(ManifestError, match=SAFE_MANIFEST_ERROR):
        load_manifest(manifest)


@pytest.mark.parametrize("case", ["truncated", "replaced", "duplicate", "reordered"])
def test_verify_sources_fails_closed_on_wrong_edition_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Catches the CLI processing a noncanonical annual corpus as an intake."""
    manifest = tmp_path / "manifest.json"
    source_root = tmp_path / "sources"
    source_root.mkdir()
    _write_manifest(manifest, _mutated_year_manifest(case))
    monkeypatch.setenv("SEN_QA_SOURCE_ROOT", str(source_root))

    result = CliRunner().invoke(app, ["verify-sources", "--manifest", str(manifest)])

    _assert_safe_cli_failure(result, "verified=0 changed=0 failed=1")
    assert SAFE_MANIFEST_ERROR in result.output


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


def test_source_read_error_is_translated_without_leaking_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a hash-read permission failure escaping as an OS exception."""
    source = tmp_path / "book.pdf"
    _write_pdf(source)
    expected_doc = _expected_document(source)
    original_open = Path.open

    def deny_source_read(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == source:
            raise PermissionError(SENSITIVE_SENTINEL)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny_source_read)

    with pytest.raises(ManifestError, match="cannot read source file") as captured:
        verify_source(source, expected_doc)
    assert SENSITIVE_SENTINEL not in str(captured.value)


def test_disappeared_source_is_translated_without_leaking_path_content(tmp_path: Path) -> None:
    """Catches a source removed after resolution escaping as FileNotFoundError."""
    source = tmp_path / "book.pdf"
    _write_pdf(source)
    expected_doc = _expected_document(source)
    source.unlink()

    with pytest.raises(ManifestError, match="cannot read source file") as captured:
        verify_source(source, expected_doc)
    assert source.name not in str(captured.value)


def test_corrupt_pdf_is_translated_without_leaking_content(tmp_path: Path) -> None:
    """Catches malformed PDF bytes escaping the PDF-library boundary."""
    source = tmp_path / "book.pdf"
    source.write_bytes(f"not a PDF {SENSITIVE_SENTINEL}".encode())
    expected_doc = _expected_document(source)

    with pytest.raises(ManifestError, match="cannot open source PDF") as captured:
        verify_source(source, expected_doc)
    assert SENSITIVE_SENTINEL not in str(captured.value)


def test_pdf_iteration_error_is_translated_without_leaking_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a page-load failure aborting intake with a library traceback."""
    source = tmp_path / "book.pdf"
    _write_pdf(source)
    expected_doc = _expected_document(source)

    class BrokenDocument:
        page_count = 1

        def __getitem__(self, page_number: int) -> object:
            raise RuntimeError(SENSITIVE_SENTINEL)

        def close(self) -> None:
            return None

    monkeypatch.setattr(fitz, "open", lambda path: BrokenDocument())

    with pytest.raises(ManifestError, match="cannot inspect source PDF") as captured:
        verify_source(source, expected_doc)
    assert SENSITIVE_SENTINEL not in str(captured.value)


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

    with pytest.raises(ManifestError, match="manifest validation failed"):
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

    with pytest.raises(ManifestError, match="manifest validation failed"):
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


@pytest.mark.parametrize(
    "manifest_text",
    [
        "",
        f"{{not valid JSON {SENSITIVE_SENTINEL}",
        json.dumps({"documents": [{"official_title": SENSITIVE_SENTINEL}]}),
    ],
    ids=["empty", "malformed-json", "schema-invalid"],
)
def test_verify_sources_sanitizes_manifest_load_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest_text: str
) -> None:
    """Catches invalid manifest data escaping through a Rich traceback."""
    manifest = tmp_path / "manifest.json"
    source_root = tmp_path / "sources"
    source_root.mkdir()
    manifest.write_text(manifest_text, encoding="utf-8")
    monkeypatch.setenv("SEN_QA_SOURCE_ROOT", str(source_root))

    result = CliRunner().invoke(app, ["verify-sources", "--manifest", str(manifest)])

    _assert_safe_cli_failure(result, "verified=0 changed=0 failed=1")
    assert "manifest validation failed" in result.output


def test_verify_sources_continues_after_corrupt_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches one corrupt source aborting verification before complete counts."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    documents: list[dict[str, object]] = []
    for year in EXPECTED_EDITION_YEARS:
        source = source_root / f"{year}.pdf"
        if year == 2022:
            source.write_bytes(f"not a PDF {SENSITIVE_SENTINEL}".encode())
        else:
            _write_pdf(source)
        documents.append(
            _expected_document(source, doc_id=f"fixture-{year}", edition_year=year).model_dump(
                mode="json"
            )
        )
    manifest = tmp_path / "manifest.json"
    _write_manifest(manifest, {"documents": documents})
    monkeypatch.setenv("SEN_QA_SOURCE_ROOT", str(source_root))

    result = CliRunner().invoke(app, ["verify-sources", "--manifest", str(manifest)])

    _assert_safe_cli_failure(result, "verified=5 changed=0 failed=1")
    assert "failed document=fixture-2022 reason=cannot open source PDF" in result.output


def test_verify_sources_reports_mismatch_without_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a failed intake being reported as success or exposing PDF text."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    monkeypatch.setenv("SEN_QA_SOURCE_ROOT", str(source_root))

    result = CliRunner().invoke(app, ["verify-sources", "--manifest", str(MANIFEST_PATH)])

    assert result.exit_code != 0
    assert "verified=0" in result.stdout
    assert "failed=6" in result.stdout
