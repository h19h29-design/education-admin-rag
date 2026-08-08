import json
import os
from pathlib import Path

import typer

from src.corpus.models import Case, CaseRelation, Chunk, Document, LawRef
from src.ingestion.manifest import (
    ManifestError,
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


_SCHEMA_MODELS = {
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
