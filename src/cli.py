import json
import os
import re
import shutil
import tempfile
from pathlib import Path

import typer
from pydantic import BaseModel

from src.corpus.models import Case, CaseRelation, Chunk, Document, LawRef
from src.ingestion.extract_native import (
    NativeExtractionError,
    extract_document,
    write_document_jsonl,
)
from src.ingestion.extract_ocr import (
    ModelLockError,
    OcrAdapterError,
    OcrExtractionError,
    create_paddle_adapter,
    extract_ocr_document,
    load_model_lock,
    validate_installed_models,
    write_ocr_jsonl,
)
from src.ingestion.manifest import (
    ManifestError,
    SourceDocument,
    load_manifest,
    resolve_source,
    verify_source,
)

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Run education administration corpus commands."""


@app.command()
def version() -> None:
    typer.echo("education-admin-rag 0.1.0")


@app.command("verify-sources")
def verify_sources(
    manifest: Path = typer.Option(  # noqa: B008 - Typer declares CLI parameters this way.
        ..., "--manifest", exists=True, dir_okay=False, readable=True
    ),
) -> None:
    """Verify every approved original PDF under SEN_QA_SOURCE_ROOT."""
    source_root_value = os.environ.get("SEN_QA_SOURCE_ROOT")
    if not source_root_value:
        typer.echo("SEN_QA_SOURCE_ROOT is required")
        raise typer.Exit(code=2)

    try:
        documents = load_manifest(manifest)
    except ManifestError as error:
        typer.echo(f"verified=0 changed=0 failed=1 error={error}")
        raise typer.Exit(code=1) from error

    verified = 0
    changed = 0
    failed = 0
    for document in documents:
        try:
            verify_source(resolve_source(Path(source_root_value), document), document)
        except ManifestError as error:
            failed += 1
            if "mismatch" in str(error):
                changed += 1
            typer.echo(f"failed document={document.doc_id} reason={error}")
        else:
            verified += 1

    typer.echo(f"verified={verified} changed={changed} failed={failed}")
    if failed:
        raise typer.Exit(code=1)


def _parse_native_years(years: str) -> tuple[int, ...]:
    """Parse the deliberately narrow comma-separated annual selection syntax."""
    parts = years.split(",")
    if not parts or any(not part.isdecimal() or len(part) != 4 for part in parts):
        raise ValueError("years must be comma-separated four-digit edition years")
    parsed = tuple(int(part) for part in parts)
    if len(set(parsed)) != len(parsed):
        raise ValueError("years must not contain duplicates")
    return parsed


def _parse_ocr_pages(selection: str) -> tuple[int, ...]:
    """Parse exact positive pages and inclusive ranges without whitespace or duplicates."""
    if not selection or re.fullmatch(r"[0-9,-]+", selection) is None:
        raise ValueError("pages must be positive numbers or inclusive ranges")
    pages: list[int] = []
    for part in selection.split(","):
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2 or not all(bound.isdecimal() for bound in bounds):
                raise ValueError("pages must be positive numbers or inclusive ranges")
            start, end = (int(bound) for bound in bounds)
            if start < 1 or end < start:
                raise ValueError("pages range is invalid")
            pages.extend(range(start, end + 1))
        else:
            if not part.isdecimal() or int(part) < 1:
                raise ValueError("pages must be positive numbers or inclusive ranges")
            pages.append(int(part))
    if not pages or len(set(pages)) != len(pages) or pages != sorted(pages):
        raise ValueError("pages must be unique and ascending")
    return tuple(pages)


def _resolve_extraction_paths(source_root: Path, output: Path) -> tuple[Path, Path]:
    """Resolve and separate source/output trees before any extraction mutation."""
    try:
        resolved_source = source_root.resolve(strict=True)
        resolved_output = output.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise NativeExtractionError("cannot resolve extraction paths") from error

    filesystem_root = Path(resolved_source.anchor)
    if resolved_source == filesystem_root or resolved_output == filesystem_root:
        raise NativeExtractionError("source and output must not be a filesystem root")
    if not resolved_source.is_dir():
        raise NativeExtractionError("source root must be an existing directory")
    if resolved_output.exists() and not resolved_output.is_dir():
        raise NativeExtractionError("output must be an existing directory or a new path")
    if (
        resolved_source == resolved_output
        or resolved_output.is_relative_to(resolved_source)
        or resolved_source.is_relative_to(resolved_output)
    ):
        raise NativeExtractionError("source root and output must not overlap")
    return resolved_source, resolved_output


def _replace_output_directory(staging: Path, output: Path) -> None:
    """Promote one owned workspace, restoring or preserving its previous-output child."""
    workspace = staging.parent
    backup = workspace / "previous-output"
    if backup.exists() or backup.is_symlink():
        raise NativeExtractionError("owned promotion backup must not already exist")
    moved_previous = False
    try:
        if output.exists():
            os.replace(output, backup)
            moved_previous = True
        os.replace(staging, output)
    except OSError as error:
        if moved_previous:
            try:
                os.replace(backup, output)
            except OSError as restore_error:
                raise NativeExtractionError(
                    "cannot promote extraction output; backup preserved for recovery"
                ) from restore_error
        shutil.rmtree(workspace)
        raise NativeExtractionError("cannot promote extraction output") from error
    if backup.exists():
        shutil.rmtree(backup)
    try:
        workspace.rmdir()
    except OSError as error:
        raise NativeExtractionError("cannot clean promotion workspace") from error


@app.command("extract-native")
def extract_native(
    manifest: Path = typer.Option(  # noqa: B008 - Typer declares CLI parameters this way.
        ..., "--manifest", exists=True, dir_okay=False, readable=True
    ),
    years: str = typer.Option(..., "--years"),
    output: Path = typer.Option(..., "--output", file_okay=False),  # noqa: B008 - Typer command parameter.
) -> None:
    """Verify then extract approved native PDFs, without printing their contents."""
    source_root_value = os.environ.get("SEN_QA_SOURCE_ROOT")
    if not source_root_value:
        typer.echo("SEN_QA_SOURCE_ROOT is required")
        raise typer.Exit(code=2)
    try:
        source_root, output = _resolve_extraction_paths(Path(source_root_value), output)
        requested_years = _parse_native_years(years)
        documents = load_manifest(manifest)
    except (ManifestError, NativeExtractionError, ValueError) as error:
        typer.echo(f"documents=0 pages=0 extracted=0 quarantined=0 failed=1 error={error}")
        raise typer.Exit(code=2) from error

    selected = tuple(document for document in documents if document.edition_year in requested_years)
    if len(selected) != len(requested_years):
        typer.echo("documents=0 pages=0 extracted=0 quarantined=0 failed=1 error=years are not in approved manifest")
        raise typer.Exit(code=2)
    if any(document.extraction_method != "native" for document in selected):
        typer.echo("documents=0 pages=0 extracted=0 quarantined=0 failed=1 error=selected document is not native")
        raise typer.Exit(code=2)

    sources: list[tuple[Path, SourceDocument]] = []
    for document in selected:
        try:
            source = resolve_source(source_root, document)
            verify_source(source, document)
        except ManifestError as error:
            typer.echo(f"documents=0 pages=0 extracted=0 quarantined=0 failed=1 error={error}")
            raise typer.Exit(code=1) from error
        sources.append((source, document))

    output_parent = output.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix=f".{output.name}.promotion-", dir=output_parent))
    staging = workspace / "new-output"
    staging.mkdir()
    extracted = 0
    quarantined = 0
    page_count = 0
    promotion_started = False
    try:
        for source, document in sources:
            records = extract_document(source, document)
            write_document_jsonl(staging / f"{document.doc_id}.jsonl", records)
            page_count += len(records)
            extracted += sum(record.status == "extracted" for record in records)
            quarantined += sum(record.status == "quarantined" for record in records)
        promotion_started = True
        _replace_output_directory(staging, output)
    except (NativeExtractionError, OSError) as error:
        if not promotion_started:
            shutil.rmtree(workspace, ignore_errors=True)
        typer.echo(f"documents={len(selected)} pages={page_count} extracted={extracted} quarantined={quarantined} failed=1 error={error}")
        raise typer.Exit(code=1) from error

    typer.echo(f"documents={len(selected)} pages={page_count} extracted={extracted} quarantined={quarantined} failed={int(quarantined > 0)}")
    if quarantined:
        raise typer.Exit(code=1)


@app.command("validate-ocr-models")
def validate_ocr_models(
    lock_path: Path = typer.Option(  # noqa: B008 - Typer declares CLI parameters this way.
        Path("config/models.lock.json"), "--lock", exists=True, dir_okay=False, readable=True
    ),
    model_root: Path = typer.Option(  # noqa: B008
        Path("/opt/models/paddleocr"), "--model-root", exists=True, file_okay=False, readable=True
    ),
) -> None:
    """Runtime local-byte validation with no download fallback."""
    try:
        lock = load_model_lock(lock_path)
        validate_installed_models(lock, model_root)
    except ModelLockError as error:
        typer.echo(f"models=0 failed=1 error={error}")
        raise typer.Exit(code=1) from error
    typer.echo(f"models={len(lock.models)} failed=0")


@app.command("extract-ocr")
def extract_ocr(
    year: int = typer.Option(..., "--year"),
    pages: str = typer.Option(..., "--pages"),
    output: Path = typer.Option(..., "--output", file_okay=False),  # noqa: B008
    manifest: Path = typer.Option(  # noqa: B008
        Path("data/manifests/sen_qa_sources.json"),
        "--manifest",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    lock_path: Path = typer.Option(  # noqa: B008
        Path("config/models.lock.json"), "--lock", exists=True, dir_okay=False, readable=True
    ),
    model_root: Path = typer.Option(  # noqa: B008
        Path("/opt/models/paddleocr"), "--model-root", file_okay=False
    ),
) -> None:
    """Verify and OCR one approved annual PDF using only locked local models."""
    source_root_value = os.environ.get("SEN_QA_SOURCE_ROOT")
    if not source_root_value:
        typer.echo("SEN_QA_SOURCE_ROOT is required")
        raise typer.Exit(code=2)
    image_digest = os.environ.get("SEN_QA_INGESTION_IMAGE_DIGEST")
    if not image_digest:
        typer.echo("SEN_QA_INGESTION_IMAGE_DIGEST is required")
        raise typer.Exit(code=2)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None:
        typer.echo("SEN_QA_INGESTION_IMAGE_DIGEST must be sha256:<64 lowercase hex>")
        raise typer.Exit(code=2)
    try:
        source_root, output = _resolve_extraction_paths(Path(source_root_value), output)
        page_indexes = _parse_ocr_pages(pages)
        documents = load_manifest(manifest)
    except (ManifestError, NativeExtractionError, ValueError) as error:
        typer.echo(f"documents=0 pages=0 extracted=0 quarantined=0 failed=1 error={error}")
        raise typer.Exit(code=2) from error
    selected = tuple(document for document in documents if document.edition_year == year)
    if len(selected) != 1 or selected[0].extraction_method != "ocr":
        typer.echo("documents=0 pages=0 extracted=0 quarantined=0 failed=1 error=year is not approved for OCR")
        raise typer.Exit(code=2)
    document = selected[0]
    if page_indexes[-1] > document.pdf_page_count:
        typer.echo("documents=0 pages=0 extracted=0 quarantined=0 failed=1 error=pages exceed approved document")
        raise typer.Exit(code=2)
    try:
        source = resolve_source(source_root, document)
        verify_source(source, document)
        lock = load_model_lock(lock_path)
        validate_installed_models(lock, model_root)
        adapter = create_paddle_adapter(lock, model_root)
    except (ManifestError, ModelLockError, OcrAdapterError) as error:
        typer.echo(f"documents=0 pages=0 extracted=0 quarantined=0 failed=1 error={error}")
        raise typer.Exit(code=1) from error

    managed_output = output / f"{document.doc_id}.jsonl"
    try:
        output.mkdir(parents=True, exist_ok=True)
        unmanaged = sorted(
            path.name for path in output.iterdir() if path != managed_output
        )
        if unmanaged:
            raise OcrExtractionError("unmanaged output file prevents OCR extraction")
        records = extract_ocr_document(source, document, page_indexes, adapter, image_digest)
        write_ocr_jsonl(managed_output, records)
        extracted = sum(record.status == "extracted" for record in records)
        quarantined = sum(record.status == "quarantined" for record in records)
    except (OcrExtractionError, NativeExtractionError, ValueError, OSError) as error:
        typer.echo("documents=1 pages=0 extracted=0 quarantined=0 failed=1 error=" + str(error))
        raise typer.Exit(code=1) from error
    typer.echo(
        f"documents=1 pages={len(records)} extracted={extracted} "
        f"quarantined={quarantined} failed={int(quarantined > 0)}"
    )
    if quarantined:
        raise typer.Exit(code=1)


_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "document.schema.json": Document,
    "case.schema.json": Case,
    "chunk.schema.json": Chunk,
    "law-ref.schema.json": LawRef,
    "case-relation.schema.json": CaseRelation,
}


@app.command("export-schemas")
def export_schemas(
    output: Path = typer.Option(..., "--output", file_okay=False),  # noqa: B008 - Typer command parameter.
) -> None:
    """Write managed schemas deterministically; overwrite stale managed files and reject unknown ones."""
    output.mkdir(parents=True, exist_ok=True)
    managed_names = set(_SCHEMA_MODELS)
    unexpected = sorted(path.name for path in output.glob("*.schema.json") if path.name not in managed_names)
    if unexpected:
        typer.echo(f"unexpected schema files: {', '.join(unexpected)}")
        raise typer.Exit(code=2)

    for filename, model in _SCHEMA_MODELS.items():
        schema = model.model_json_schema()
        schema["$id"] = f"data/schemas/{filename}"
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        rendered = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (output / filename).write_text(rendered, encoding="utf-8", newline="\n")
    typer.echo(f"exported={len(_SCHEMA_MODELS)} output={output}")


if __name__ == "__main__":
    app()
