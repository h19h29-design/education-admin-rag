"""Behavior contracts for native-PDF provenance extraction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import src.cli as cli_module
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
    NativeExtractionError,
    discover_repeated_signatures,
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
    tmp_path.mkdir(parents=True, exist_ok=True)
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


def test_manifest_body_page_count_keeps_2020_threshold_after_three_quarantines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches lowering a 40% threshold to the surviving-page count after failures."""
    source = tmp_path / "fixture.pdf"
    _write_pdf(source, ("one", "two", "three", "four", "five"))
    document = _document(source, pages=5)
    import src.ingestion.extract_native as native

    def extract_with_three_failures(
        page: object, *, document: SourceDocument, pdf_page_index: int
    ) -> RawPage:
        del page
        if pdf_page_index in (2, 3, 4):
            raise pymupdf.mupdf.FzErrorGeneric("synthetic page failure")
        block = _block("navigation", (92.0, 40.0, 98.0, 70.0)) if pdf_page_index == 1 else _block(
            "body", (10.0, 40.0, 80.0, 70.0)
        )
        return RawPage(
            doc_id=document.doc_id,
            edition_year=document.edition_year,
            pdf_page_index=pdf_page_index,
            page_label=str(pdf_page_index),
            page_width=100.0,
            page_height=200.0,
            render_sha256="a" * 64,
            raw_blocks=(block,),
        )

    monkeypatch.setattr(native, "_extract_raw_page", extract_with_three_failures)

    records = extract_document(source, document)

    assert records[0].status == "extracted"
    assert records[0].normalized_text == "navigation"
    assert [record.status for record in records] == [
        "extracted",
        "quarantined",
        "quarantined",
        "quarantined",
        "extracted",
    ]


def test_repetition_discovery_keeps_inclusive_40_percent_manifest_boundary() -> None:
    """Catches making two occurrences among five body pages fail the approved boundary."""
    navigation = _block("navigation", (92.0, 40.0, 98.0, 70.0))
    pages = (
        _page(page_number=7, blocks=(navigation,)),
        _page(page_number=9, blocks=(navigation,)),
    )

    repeated, counts = discover_repeated_signatures(pages, body_page_count=5)

    assert repeated == frozenset({"right-margin:navigation"})
    assert counts == {"right-margin:navigation": 2}


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
    cleaned = remove_repeated_margin_blocks(page, repeated_signatures=frozenset({"header-footer:top:공통 머리말"}), body_page_count=4, signature_counts={"header-footer:top:공통 머리말": 2})
    assert "고유 머리말" in cleaned.normalized_text
    assert cleaned.normalized_text.count("공통 머리말") == 1


def test_header_crossing_top_eight_percent_boundary_is_body_text() -> None:
    """Catches removing a block that only overlaps, rather than fits inside, the top zone."""
    crossing = _block("crossing", (10.0, 0.0, 70.0, 17.0))
    page = _page(year=2021, blocks=(crossing,))

    cleaned = remove_repeated_margin_blocks(
        page,
        repeated_signatures=frozenset({"header-footer:crossing"}),
        body_page_count=2,
        signature_counts={"header-footer:crossing": 2},
    )

    assert cleaned.normalized_text == "crossing"


def test_same_text_in_top_and_bottom_zones_does_not_share_repetition_count() -> None:
    """Catches combining distinct header and footer placements into one template signature."""
    pages = (
        _page(year=2021, page_number=7, blocks=(_block("shared", (10.0, 0.0, 70.0, 10.0)),)),
        _page(year=2021, page_number=8, blocks=(_block("shared", (10.0, 190.0, 70.0, 200.0)),)),
    )

    repeated, counts = discover_repeated_signatures(pages, body_page_count=4)

    assert repeated == frozenset()
    assert counts == {
        "header-footer:bottom:shared": 1,
        "header-footer:top:shared": 1,
    }


def test_2022_continuation_body_text_survives_cleanup() -> None:
    """Catches a continuation page being mistaken for its repeated header/footer template."""
    page = _fixture_page("2022-continuation.json", year=2022, page_number=7)
    cleaned = remove_repeated_margin_blocks(page, repeated_signatures=frozenset(), body_page_count=4, signature_counts={})
    assert cleaned.normalized_text == "이어서 적용한다"


def test_multiline_multispan_block_preserves_word_boundaries_and_raw_spans() -> None:
    """Catches native lines being concatenated into a different token."""
    line_one_spans = (
        _span("교육", (10.0, 40.0, 30.0, 50.0)),
        _span("행정", (30.0, 40.0, 50.0, 50.0)),
    )
    line_two_spans = (
        _span("지원", (10.0, 55.0, 30.0, 65.0)),
        _span("시스템", (30.0, 55.0, 60.0, 65.0)),
    )
    block = RawBlock(
        bbox=BoundingBox.from_tuple((10.0, 40.0, 60.0, 65.0)),
        lines=(
            RawLine(bbox=BoundingBox.from_tuple((10.0, 40.0, 50.0, 50.0)), spans=line_one_spans),
            RawLine(bbox=BoundingBox.from_tuple((10.0, 55.0, 60.0, 65.0)), spans=line_two_spans),
        ),
    )
    page = _page(year=2022, blocks=(block,))

    cleaned = remove_repeated_margin_blocks(page, repeated_signatures=frozenset())

    assert cleaned.normalized_text == "교육행정 지원시스템"
    assert cleaned.raw_blocks == (block,)
    assert cleaned.raw_blocks[0].lines[1].spans[0].text == "지원"


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


@pytest.mark.parametrize(
    "page_error",
    [pymupdf.mupdf.FzErrorGeneric("synthetic page failure"), IndexError("synthetic page index")],
)
def test_supported_page_failure_is_quarantined_and_later_page_is_extracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page_error: Exception
) -> None:
    """Catches supported PyMuPDF/page-index failures aborting later pages or becoming blank text."""
    source = tmp_path / "fixture.pdf"
    _write_pdf(source, ("first", "second"))
    document = _document(source, pages=2)
    import src.ingestion.extract_native as native

    real_extract = native._extract_raw_page

    def break_first(*args: object, **kwargs: object) -> RawPage:
        if kwargs.get("pdf_page_index") == 1:
            raise page_error
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(native, "_extract_raw_page", break_first)
    records = extract_document(source, document)
    assert records[0].status == "quarantined"
    assert records[0].reason_code == "page-extraction-failed"
    assert records[1].status == "extracted"
    assert records[1].normalized_text is not None


def test_programmer_runtime_error_is_not_silently_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches arbitrary implementation errors being mislabeled as corrupt PDF pages."""
    source = tmp_path / "fixture.pdf"
    _write_pdf(source, ("first",))
    document = _document(source)
    import src.ingestion.extract_native as native

    def programming_bug(*args: object, **kwargs: object) -> RawPage:
        del args, kwargs
        raise RuntimeError("synthetic programming bug")

    monkeypatch.setattr(native, "_extract_raw_page", programming_bug)

    with pytest.raises(RuntimeError, match="synthetic programming bug"):
        extract_document(source, document)


def test_pymupdf_open_failure_is_translated_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a MuPDF open failure escaping the native extraction boundary."""
    source = tmp_path / "fixture.pdf"
    _write_pdf(source, ("first",))
    document = _document(source)
    import src.ingestion.extract_native as native

    def fail_open(path: Path) -> object:
        del path
        raise pymupdf.mupdf.FzErrorGeneric("PRIVATE PDF CONTENT")

    monkeypatch.setattr(native.pymupdf, "open", fail_open)

    with pytest.raises(NativeExtractionError, match="cannot open approved source PDF") as captured:
        extract_document(source, document)
    assert "PRIVATE PDF CONTENT" not in str(captured.value)


def test_pymupdf_close_failure_is_translated_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a MuPDF close failure escaping the native extraction boundary."""
    source = tmp_path / "fixture.pdf"
    _write_pdf(source, ("first",))
    document = _document(source)
    import src.ingestion.extract_native as native

    class CloseFailureDocument:
        def __getitem__(self, page_number: int) -> object:
            del page_number
            return object()

        def close(self) -> None:
            raise pymupdf.mupdf.FzErrorGeneric("PRIVATE PDF CONTENT")

    monkeypatch.setattr(native.pymupdf, "open", lambda path: CloseFailureDocument())
    monkeypatch.setattr(
        native,
        "_extract_raw_page",
        lambda page, *, document, pdf_page_index: _page(
            year=document.edition_year,
            page_number=pdf_page_index,
            blocks=(_block("body", (10.0, 40.0, 80.0, 70.0)),),
        ),
    )

    with pytest.raises(NativeExtractionError, match="cannot close approved source PDF") as captured:
        extract_document(source, document)
    assert "PRIVATE PDF CONTENT" not in str(captured.value)


def test_extract_native_cli_rejects_invalid_years_and_non_native_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches silently accepting malformed years or routing OCR sources through native extraction."""
    source_root = tmp_path / "sources"
    manifest = _write_full_manifest(source_root)
    monkeypatch.setenv("SEN_QA_SOURCE_ROOT", str(source_root))

    bad_years = CliRunner().invoke(app, ["extract-native", "--manifest", str(manifest), "--years", "2020,,2021", "--output", str(tmp_path / "out")])
    assert bad_years.exit_code != 0
    assert "years must" in bad_years.output

    manifest = _write_full_manifest(source_root, native_2020=False)
    non_native = CliRunner().invoke(app, ["extract-native", "--manifest", str(manifest), "--years", "2020", "--output", str(tmp_path / "out")])
    assert non_native.exit_code != 0
    assert "native" in non_native.output


def test_extract_native_cli_writes_only_selected_verified_native_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches extracting unselected editions or leaving a partial output directory."""
    source_root = tmp_path / "sources"
    manifest = _write_full_manifest(source_root)
    monkeypatch.setenv("SEN_QA_SOURCE_ROOT", str(source_root))
    output = tmp_path / "out"
    result = CliRunner().invoke(app, ["extract-native", "--manifest", str(manifest), "--years", "2020", "--output", str(output)])
    assert result.exit_code == 0
    assert sorted(path.name for path in output.glob("*.jsonl")) == ["fixture-2020.jsonl"]
    assert "pages=1 extracted=1 quarantined=0 failed=0" in result.output


@pytest.mark.parametrize("relationship", ["equal", "inside", "ancestor"])
def test_output_overlapping_source_root_is_rejected_before_mutation(
    tmp_path: Path, relationship: str
) -> None:
    """Catches output promotion overwriting all or part of the read-only source tree."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    sentinel = source_root / "source-sentinel.pdf"
    sentinel.write_bytes(b"source stays intact")
    output = {
        "equal": source_root,
        "inside": source_root / "artifacts" / "raw-pages",
        "ancestor": tmp_path,
    }[relationship]

    with pytest.raises(NativeExtractionError, match="must not overlap"):
        cli_module._resolve_extraction_paths(source_root, output)

    assert sentinel.read_bytes() == b"source stays intact"
    assert not (source_root / "artifacts").exists()


def test_symlinked_source_or_output_overlap_is_rejected(tmp_path: Path) -> None:
    """Catches lexical path checks missing overlap through a symlink."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_link = tmp_path / "source-link"
    source_link.symlink_to(source_root, target_is_directory=True)
    inside_target = source_root / "existing-output"
    inside_target.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(inside_target, target_is_directory=True)

    with pytest.raises(NativeExtractionError, match="must not overlap"):
        cli_module._resolve_extraction_paths(source_link, tmp_path / "safe" / ".." / "source-link" / "new")
    with pytest.raises(NativeExtractionError, match="must not overlap"):
        cli_module._resolve_extraction_paths(source_root, output_link)


def test_path_validation_rejects_filesystem_root_and_existing_file(tmp_path: Path) -> None:
    """Catches an unsafe broad root or regular file becoming a promotion target."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    existing_file = tmp_path / "output.jsonl"
    existing_file.write_text("sentinel", encoding="utf-8")

    with pytest.raises(NativeExtractionError, match="filesystem root"):
        cli_module._resolve_extraction_paths(Path(source_root.anchor), tmp_path / "output")
    with pytest.raises(NativeExtractionError, match="filesystem root"):
        cli_module._resolve_extraction_paths(source_root, Path(source_root.anchor))
    with pytest.raises(NativeExtractionError, match="existing directory"):
        cli_module._resolve_extraction_paths(source_root, existing_file)
    assert existing_file.read_text(encoding="utf-8") == "sentinel"


def test_safe_nonexistent_output_is_resolved_without_creating_parents(tmp_path: Path) -> None:
    """Catches validation mutating the filesystem before extraction is approved."""
    source_root = tmp_path / "source"
    source_root.mkdir()
    output = tmp_path / "not-created" / "raw-pages"

    resolved_source, resolved_output = cli_module._resolve_extraction_paths(source_root, output)

    assert resolved_source == source_root.resolve()
    assert resolved_output == output.resolve()
    assert not output.parent.exists()


def test_successful_promotion_leaves_legacy_backup_and_unrelated_siblings_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches fixed-name cleanup deleting paths this run does not own."""
    source_root = tmp_path / "sources"
    manifest = _write_full_manifest(source_root)
    monkeypatch.setenv("SEN_QA_SOURCE_ROOT", str(source_root))
    output = tmp_path / "out"
    legacy_backup = tmp_path / ".out.previous"
    legacy_backup.mkdir()
    (legacy_backup / "sentinel").write_text("legacy", encoding="utf-8")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "sentinel").write_text("unrelated", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["extract-native", "--manifest", str(manifest), "--years", "2020", "--output", str(output)],
    )

    assert result.exit_code == 0
    assert (legacy_backup / "sentinel").read_text(encoding="utf-8") == "legacy"
    assert (unrelated / "sentinel").read_text(encoding="utf-8") == "unrelated"


def test_failed_new_promotion_restores_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a failed second rename leaving the previous output displaced."""
    output = tmp_path / "out"
    output.mkdir()
    (output / "old").write_text("old", encoding="utf-8")
    workspace = tmp_path / ".out.promotion-owned"
    staging = workspace / "new-output"
    staging.mkdir(parents=True)
    (staging / "new").write_text("new", encoding="utf-8")
    real_replace = cli_module.os.replace

    def fail_new_promotion(source: Path, destination: Path) -> None:
        if Path(source) == staging:
            raise OSError("synthetic promotion failure")
        real_replace(source, destination)

    monkeypatch.setattr(cli_module.os, "replace", fail_new_promotion)

    with pytest.raises(NativeExtractionError, match="cannot promote extraction output"):
        cli_module._replace_output_directory(staging, output)

    assert (output / "old").read_text(encoding="utf-8") == "old"
    assert not workspace.exists()


def test_failed_restoration_preserves_owned_backup_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches cleanup erasing the only previous output after restore also fails."""
    output = tmp_path / "out"
    output.mkdir()
    (output / "old").write_text("old", encoding="utf-8")
    workspace = tmp_path / ".out.promotion-owned"
    staging = workspace / "new-output"
    staging.mkdir(parents=True)
    (staging / "new").write_text("new", encoding="utf-8")
    backup = workspace / "previous-output"
    real_replace = cli_module.os.replace

    def fail_promotion_and_restore(source: Path, destination: Path) -> None:
        if Path(source) in (staging, backup):
            raise OSError("synthetic rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(cli_module.os, "replace", fail_promotion_and_restore)

    with pytest.raises(NativeExtractionError, match="backup preserved for recovery"):
        cli_module._replace_output_directory(staging, output)

    assert not output.exists()
    assert (backup / "old").read_text(encoding="utf-8") == "old"
    assert (staging / "new").read_text(encoding="utf-8") == "new"


def test_cli_version_is_preserved() -> None:
    """Catches extraction CLI registration breaking the established version command."""
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "education-admin-rag 0.1.0"
