"""Behavior contracts for native-PDF provenance extraction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from src.cli import app
from src.ingestion.extract_common import (
    APPROVED_PAGE_POLICIES,
    BoundingBox,
    RawBlock,
    RawLine,
    RawPage,
    RawSpan,
    printed_page_label,
)
from src.ingestion.extract_native import (
    HEADER_FOOTER_REPETITION_FRACTION,
    extract_document,
    remove_repeated_margin_blocks,
    write_document_jsonl,
)
from src.ingestion.manifest import SourceDocument, load_manifest


def _span(text: str, bbox: tuple[float, float, float, float]) -> RawSpan:
    return RawSpan(text=text, bbox=BoundingBox.from_tuple(bbox), font="Fixture", size=10.0, confidence=1.0)


def _block(text: str, bbox: tuple[float, float, float, float]) -> RawBlock:
    span = _span(text, bbox)
    return RawBlock(bbox=BoundingBox.from_tuple(bbox), lines=(RawLine(bbox=span.bbox, spans=(span,)),))


def _page(
    *,
    year: int = 2020,
    page_number: int = 7,
    blocks: tuple[RawBlock, ...],
    width: float = 100.0,
    height: float = 200.0,
) -> RawPage:
    return RawPage(
        doc_id=f"fixture-{year}",
        edition_year=year,
        pdf_page_index=page_number,
        page_label=printed_page_label(year, page_number),
        page_width=width,
        page_height=height,
        render_sha256="a" * 64,
        raw_blocks=blocks,
    )


def _fixture_page(name: str, *, year: int, page_number: int) -> RawPage:
    fixture = json.loads((Path("tests/fixtures/native-pages") / name).read_text(encoding="utf-8"))
    blocks = tuple(
        RawBlock(
            bbox=BoundingBox.from_tuple(tuple(block["bbox"])),
            lines=tuple(
                RawLine(
                    bbox=BoundingBox.from_tuple(tuple(line["bbox"])),
                    spans=tuple(
                        RawSpan(
                            text=span["text"],
                            bbox=BoundingBox.from_tuple(tuple(span["bbox"])),
                            font=span["font"],
                            size=span["size"],
                            confidence=1.0,
                        )
                        for span in line["spans"]
                    ),
                )
                for line in block["lines"]
            ),
        )
        for block in fixture["blocks"]
    )
    return _page(
        year=year,
        page_number=page_number,
        blocks=blocks,
        width=fixture["page_width"],
        height=fixture["page_height"],
    )


def _document(path: Path, *, year: int = 2020, pages: int = 1) -> SourceDocument:
    return SourceDocument.model_validate(
        {
            "doc_id": f"fixture-{year}",
            "edition_year": year,
            "official_title": "Synthetic fixture",
            "publisher": "Fixture",
            "registration_no": None,
            "source_period_start": None,
            "source_period_end": None,
            "source_filename": path.name,
            "source_relpath": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "pdf_page_count": pages,
            "page_size_profiles": ({"start_pdf_page": 1, "end_pdf_page": pages, "width_pt": 100.0, "height_pt": 200.0},),
            "extraction_method": "native",
            "source_dpi": None,
            "render_dpi": None,
            "page_numbering": {"mode": "offset", "body_start_pdf_page": 1, "body_end_pdf_page": pages, "offset": 0},
            "official_public_url": None,
            "official_url_status": "unverified",
            "redistribution_status": "unverified",
            "access_level": "staff",
        }
    )


def _write_pdf(path: Path, page_texts: tuple[str, ...]) -> None:
    document = pymupdf.open()
    for text in page_texts:
        page = document.new_page(width=100.0, height=200.0)
        page.insert_text((10.0, 90.0), text, fontsize=10.0)
    document.save(path)
    document.close()


def _write_full_manifest(tmp_path: Path, *, native_2020: bool = True) -> Path:
    documents: list[dict[str, object]] = []
    for year in range(2020, 2026):
        source = tmp_path / f"{year}.pdf"
        _write_pdf(source, ("fixture",))
        payload = _document(source, year=year).model_dump(mode="json")
        if year == 2020 and not native_2020:
            payload["extraction_method"] = "ocr"
            payload["source_dpi"] = 96
            payload["render_dpi"] = 300
        documents.append(payload)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"documents": documents}), encoding="utf-8")
    return manifest


def test_2020_printed_page_is_pdf_page_minus_six() -> None:
    """Catches a duplicated or incorrect 2020 citation-page offset."""
    assert printed_page_label(2020, pdf_page_index=13) == "7"


def test_front_matter_has_no_printed_page_label() -> None:
    """Catches a citation label leaking from a 2020 front-matter page."""
    assert printed_page_label(2020, pdf_page_index=3) is None


def test_checked_in_label_policies_drift_if_native_manifest_offsets_change() -> None:
    """Catches citation labels silently diverging from the reviewed source manifest."""
    documents = load_manifest(Path("data/manifests/sen_qa_sources.json"))
    assert {document.edition_year: document.page_numbering for document in documents if document.extraction_method == "native"} == APPROVED_PAGE_POLICIES


def test_raw_models_reject_mutation_invalid_geometry_and_malformed_hash() -> None:
    """Catches mutable provenance data or untrustworthy page geometry/hash values."""
    page = _page(blocks=(_block("body", (1.0, 2.0, 3.0, 4.0)),))
    with pytest.raises(ValidationError):
        page.raw_blocks[0].lines[0].spans[0].text = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        BoundingBox.from_tuple((4.0, 2.0, 3.0, 4.0))
    with pytest.raises(ValidationError):
        BoundingBox.from_tuple((1.0, float("nan"), 3.0, 4.0))
    with pytest.raises(ValidationError):
        _page(blocks=(), width=float("inf"))
    with pytest.raises(ValidationError):
        RawPage.model_validate({**page.model_dump(), "render_sha256": "not-a-digest"})


def test_2020_margin_is_removed_only_at_repetition_boundary() -> None:
    """Catches removing a unique right-margin body block below the 40% threshold."""
    page = _fixture_page("2020-odd-page.json", year=2020, page_number=7)

    retained = remove_repeated_margin_blocks(page, repeated_signatures=frozenset({"right-margin:19편"}), body_page_count=5, signature_counts={"right-margin:19편": 1})
    removed = remove_repeated_margin_blocks(page, repeated_signatures=frozenset({"right-margin:19편"}), body_page_count=5, signature_counts={"right-margin:19편": 2})

    assert "19편" in retained.normalized_text
    assert "19편" not in removed.normalized_text
    assert "계약방법" in removed.normalized_text
    assert removed.raw_blocks == page.raw_blocks


def test_2020_right_margin_coordinate_boundary_preserves_unique_body_text() -> None:
    """Catches treating a block at 89% width as removable navigation."""
    near_margin = _block("보존", (89.0, 40.0, 95.0, 70.0))
    page = _page(blocks=(near_margin,))
    cleaned = remove_repeated_margin_blocks(page, repeated_signatures=frozenset({"right-margin:보존"}), body_page_count=1, signature_counts={"right-margin:보존": 1})
    assert cleaned.normalized_text == "보존"


def test_2021_header_footer_need_both_coordinate_zone_and_repeated_text() -> None:
    """Catches deleting ordinary body/unique header text using only one cleanup signal."""
    repeated_header = _block("공통 머리말", (10.0, 0.0, 70.0, 10.0))
    unique_header = _block("고유 머리말", (10.0, 0.0, 70.0, 10.0))
    body_repeat = _block("공통 머리말", (10.0, 50.0, 70.0, 60.0))
    page = _page(year=2021, blocks=(repeated_header, unique_header, body_repeat))
    cleaned = remove_repeated_margin_blocks(page, repeated_signatures=frozenset({"header-footer:공통 머리말"}), body_page_count=4, signature_counts={"header-footer:공통 머리말": 2})
    assert "고유 머리말" in cleaned.normalized_text
    assert cleaned.normalized_text.count("공통 머리말") == 1


def test_2022_continuation_body_text_survives_cleanup() -> None:
    """Catches a continuation page being mistaken for its repeated header/footer template."""
    page = _fixture_page("2022-continuation.json", year=2022, page_number=7)
    cleaned = remove_repeated_margin_blocks(page, repeated_signatures=frozenset(), body_page_count=4, signature_counts={})
    assert cleaned.normalized_text == "이어서 적용한다"


def test_header_footer_repetition_threshold_is_conservative() -> None:
    """Catches accepting a two-page coincidence as a 2021-22 page template."""
    assert HEADER_FOOTER_REPETITION_FRACTION > 0.40


def test_jsonl_is_deterministic_and_never_substitutes_empty_page_text(tmp_path: Path) -> None:
    """Catches output order/path-dependent bytes or an extraction failure becoming empty text."""
    source = tmp_path / "fixture.pdf"
    _write_pdf(source, ("first", "second"))
    document = _document(source, pages=2)

    records = extract_document(source, document)
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    write_document_jsonl(first, records)
    write_document_jsonl(second, records)
    assert first.read_bytes() == second.read_bytes()
    lines = [json.loads(line) for line in first.read_text(encoding="utf-8").splitlines()]
    assert [line["pdf_page_index"] for line in lines] == [1, 2]
    assert all(line["status"] == "extracted" or "normalized_text" not in line for line in lines)


def test_page_failure_is_quarantined_and_later_page_is_extracted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches one broken PDF page aborting later pages or being emitted as blank text."""
    source = tmp_path / "fixture.pdf"
    _write_pdf(source, ("first", "second"))
    document = _document(source, pages=2)
    import src.ingestion.extract_native as native

    real_extract = native._extract_raw_page

    def break_first(*args: object, **kwargs: object) -> RawPage:
        if kwargs.get("pdf_page_index") == 1:
            raise RuntimeError("synthetic failure")
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(native, "_extract_raw_page", break_first)
    records = extract_document(source, document)
    assert records[0].status == "quarantined"
    assert records[0].reason_code == "page-extraction-failed"
    assert records[1].status == "extracted"
    assert records[1].normalized_text is not None


def test_extract_native_cli_rejects_invalid_years_and_non_native_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches silently accepting malformed years or routing OCR sources through native extraction."""
    manifest = _write_full_manifest(tmp_path)
    monkeypatch.setenv("SEN_QA_SOURCE_ROOT", str(tmp_path))

    bad_years = CliRunner().invoke(app, ["extract-native", "--manifest", str(manifest), "--years", "2020,,2021", "--output", str(tmp_path / "out")])
    assert bad_years.exit_code != 0
    assert "years must" in bad_years.output

    manifest = _write_full_manifest(tmp_path, native_2020=False)
    non_native = CliRunner().invoke(app, ["extract-native", "--manifest", str(manifest), "--years", "2020", "--output", str(tmp_path / "out")])
    assert non_native.exit_code != 0
    assert "native" in non_native.output


def test_extract_native_cli_writes_only_selected_verified_native_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches extracting unselected editions or leaving a partial output directory."""
    manifest = _write_full_manifest(tmp_path)
    monkeypatch.setenv("SEN_QA_SOURCE_ROOT", str(tmp_path))
    output = tmp_path / "out"
    result = CliRunner().invoke(app, ["extract-native", "--manifest", str(manifest), "--years", "2020", "--output", str(output)])
    assert result.exit_code == 0
    assert sorted(path.name for path in output.glob("*.jsonl")) == ["fixture-2020.jsonl"]
    assert "pages=1 extracted=1 quarantined=0 failed=0" in result.output


def test_cli_version_is_preserved() -> None:
    """Catches extraction CLI registration breaking the established version command."""
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "education-admin-rag 0.1.0"
