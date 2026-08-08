import json
import os
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


def _replace_output_directory(staging: Path, output: Path) -> None:
    """Promote a complete staged run, restoring the previous run if promotion fails."""
    backup = output.with_name(f".{output.name}.previous")
    if backup.exists():
        shutil.rmtree(backup)
    moved_previous = False
    try:
        if output.exists():
            os.replace(output, backup)
            moved_previous = True
        os.replace(staging, output)
    except OSError as error:
        if moved_previous and not output.exists() and backup.exists():
            os.replace(backup, output)
        raise NativeExtractionError("cannot promote extraction output") from error
    if backup.exists():
        shutil.rmtree(backup)


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
        requested_years = _parse_native_years(years)
        documents = load_manifest(manifest)
    except (ManifestError, ValueError) as error:
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
            source = resolve_source(Path(source_root_value), document)
            verify_source(source, document)
        except ManifestError as error:
            typer.echo(f"documents=0 pages=0 extracted=0 quarantined=0 failed=1 error={error}")
            raise typer.Exit(code=1) from error
        sources.append((source, document))

    output_parent = output.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output_parent))
    extracted = 0
    quarantined = 0
    page_count = 0
    try:
        for source, document in sources:
            records = extract_document(source, document)
            write_document_jsonl(staging / f"{document.doc_id}.jsonl", records)
            page_count += len(records)
            extracted += sum(record.status == "extracted" for record in records)
            quarantined += sum(record.status == "quarantined" for record in records)
        _replace_output_directory(staging, output)
    except (NativeExtractionError, OSError) as error:
        shutil.rmtree(staging, ignore_errors=True)
        typer.echo(f"documents={len(selected)} pages={page_count} extracted={extracted} quarantined={quarantined} failed=1 error={error}")
        raise typer.Exit(code=1) from error

    typer.echo(f"documents={len(selected)} pages={page_count} extracted={extracted} quarantined={quarantined} failed={int(quarantined > 0)}")
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
