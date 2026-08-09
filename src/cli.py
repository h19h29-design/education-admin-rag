import hashlib
import json
import os
import pwd
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, cast

import typer
from pydantic import BaseModel

from src.corpus.chunking import (
    ChunkingError,
    load_embedding_model_lock,
    verify_embedding_cache,
)
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
    MAX_SUPPORTED_PDF_PAGE_COUNT,
    ManifestError,
    SourceDocument,
    load_manifest,
    resolve_source,
    verify_source,
)
from src.ingestion.parse_metadata import (
    ParseMetadataError,
    build_parse_metadata,
    canonical_metadata_bytes,
)
from src.ingestion.review import (
    ReviewConflictError,
    ReviewError,
    ReviewPurpose,
    ReviewStore,
    ReviewValidationError,
    RunMode,
    SegmentManifest,
    validate_review_reason,
)
from src.retrieval.dense import DenseEncoder, DenseError
from src.retrieval.lexical import LexicalError, LexicalIndex
from src.retrieval.query import QueryError
from src.retrieval.service import SearchResponse

app = typer.Typer(no_args_is_help=True)
review_app = typer.Typer(no_args_is_help=True)
app.add_typer(review_app, name="review")


def _current_os_actor() -> str:
    """Bind CLI review actions to the effective operating-system account."""
    effective_uid = os.geteuid()
    try:
        username = pwd.getpwuid(effective_uid).pw_name
    except KeyError as error:
        raise ReviewValidationError("effective OS account is not resolvable") from error
    return f"uid:{effective_uid}:{username}"


_review_actor_provider: Callable[[], str] = _current_os_actor


def _review_actor(declared_reviewer_id: str | None) -> str:
    try:
        actor = _review_actor_provider()
    except (ReviewError, OSError):
        _review_cli_fail("actor_resolution_failed", exit_code=2)
    actor_parts = actor.split(":", maxsplit=2)
    pwd_username = (
        actor_parts[2]
        if len(actor_parts) == 3
        and actor_parts[0] == "uid"
        and actor_parts[1].isdecimal()
        else None
    )
    if declared_reviewer_id is not None and declared_reviewer_id not in {
        actor,
        pwd_username,
    }:
        _review_cli_fail("actor_mismatch", exit_code=2)
    return actor


def _review_cli_fail(code: str, *, exit_code: int = 1, updated: int = 0) -> NoReturn:
    typer.echo(f"updated={updated} failed=1 error_code={code}")
    raise typer.Exit(code=exit_code)


def _safe_review_error(error: BaseException, *, updated: int = 0) -> NoReturn:
    if isinstance(error, ReviewError):
        _review_cli_fail(error.code, updated=updated)
    _review_cli_fail("storage_error", updated=updated)


@app.callback()
def main() -> None:
    """Run education administration corpus commands."""


@app.command()
def version() -> None:
    typer.echo("education-admin-rag 0.1.0")


@app.command("inspect-lexical-plan")
def inspect_lexical_plan(
    database: Path = typer.Option(  # noqa: B008 - Typer declares CLI parameters this way.
        ..., "--db", exists=True, dir_okay=False, readable=True
    ),
    query: str = typer.Option(..., "--query"),
) -> None:
    """Emit only value-free FTS execution-plan metadata."""
    error_code: str | None = None
    plan = None
    try:
        plan = LexicalIndex(database).inspect_plan(query)
    except (LexicalError, QueryError) as error:
        error_code = error.code
    except (OSError, sqlite3.Error, TypeError, ValueError):
        error_code = "inspect_failed"
    if error_code is not None or plan is None:
        typer.echo(f"failed=1 error_code={error_code or 'inspect_failed'}")
        raise SystemExit(1) from None
    typer.echo(
        f"uses_fts={int(plan.uses_fts)} "
        f"full_table_scan={int(plan.full_table_scan)} "
        f"restricted_candidates={plan.restricted_candidates} "
        f"plan_steps={plan.plan_steps} failed=0"
    )


@app.command("verify-embedding-models")
def verify_embedding_models(
    lock_path: Path = typer.Option(  # noqa: B008 - Typer declares CLI parameters this way.
        Path("config/models.lock.json"), "--lock", exists=True, dir_okay=False
    ),
    model_root: Path = typer.Option(..., "--model-root", file_okay=False),  # noqa: B008
    expected_lock_sha256: str = typer.Option(..., "--expected-lock-sha256"),
) -> None:
    """Verify the exact offline BGE-M3 runtime closure without loading the model."""
    error_code: str | None = None
    lock = None
    try:
        lock = load_embedding_model_lock(lock_path)
        verify_embedding_cache(
            lock,
            model_root,
            scope="full",
            expected_lock_sha256=expected_lock_sha256,
        )
    except ChunkingError:
        error_code = "embedding_cache_invalid"
    except (OSError, TypeError, ValueError):
        error_code = "embedding_verification_failed"
    if error_code is not None or lock is None:
        typer.echo(
            f"failed=1 error_code={error_code or 'embedding_verification_failed'}"
        )
        raise SystemExit(1) from None
    typer.echo(
        f"models=1 files={len(lock.files)} revision={lock.revision[:8]} failed=0"
    )


@app.command("dense-smoke")
def dense_smoke(
    text: str = typer.Option(..., "--text"),
    lock_path: Path = typer.Option(  # noqa: B008 - Typer declares CLI parameters this way.
        Path("config/models.lock.json"), "--lock", exists=True, dir_okay=False
    ),
) -> None:
    """Encode one value while emitting only normalized-vector metadata."""
    model_root_value = os.environ.get("SEN_QA_EMBEDDING_MODEL_ROOT")
    expected_lock_sha256 = os.environ.get("SEN_QA_EMBEDDING_LOCK_SHA256")
    error_code: str | None = None
    encoder = None
    vectors: tuple[tuple[float, ...], ...] = ()
    if model_root_value is None or expected_lock_sha256 is None:
        error_code = "embedding_environment_invalid"
    else:
        try:
            encoder = DenseEncoder.from_lock(
                lock_path,
                model_root=Path(model_root_value),
                expected_lock_sha256=expected_lock_sha256,
            )
            vectors = encoder.encode((text,))
        except DenseError as error:
            error_code = error.code
        except (OSError, RuntimeError, TypeError, ValueError):
            error_code = "dense_smoke_failed"
    if error_code is not None or encoder is None or len(vectors) != 1:
        typer.echo(f"failed=1 error_code={error_code or 'dense_smoke_failed'}")
        raise SystemExit(1) from None
    vector = vectors[0]
    normalized = int(
        bool(vector) and abs(sum(value * value for value in vector) - 1.0) <= 1e-6
    )
    if not normalized:
        typer.echo("failed=1 error_code=dense_smoke_failed")
        raise SystemExit(1) from None
    typer.echo(
        f"vectors=1 dimension={len(vector)} normalized=1 "
        f"revision={encoder.revision[:8]} failed=0"
    )


@review_app.command("verify-fields")
def review_verify_fields(
    database: Path = typer.Option(  # noqa: B008 - Typer declares CLI parameters this way.
        ..., "--db", exists=True, dir_okay=False, readable=True, writable=True
    ),
    case_id: str = typer.Option(..., "--case-id"),
    content_sha256: str = typer.Option(..., "--content-sha256"),
    reason: str = typer.Option(..., "--reason"),
    reviewer_id: str | None = typer.Option(None, "--reviewer-id"),
) -> None:
    """Attest that every critical field matches a content-addressed candidate."""
    actor = _review_actor(reviewer_id)
    try:
        with ReviewStore(database) as store:
            store.verify_critical_fields(
                case_id,
                reviewer_id=actor,
                reviewed_content_sha256=content_sha256,
                reason=reason,
            )
    except (ReviewError, sqlite3.Error, OSError) as error:
        _safe_review_error(error)
    typer.echo(f"updated=1 case_id={case_id} status=needs_review failed=0")


@review_app.command("approve-search")
def review_approve_search(
    database: Path = typer.Option(  # noqa: B008
        ..., "--db", exists=True, dir_okay=False, readable=True, writable=True
    ),
    case_id: str = typer.Option(..., "--case-id"),
    content_sha256: str = typer.Option(..., "--content-sha256"),
    reason: str = typer.Option(..., "--reason"),
    reviewer_id: str | None = typer.Option(None, "--reviewer-id"),
) -> None:
    """Approve a critical-field-verified case for staff search only."""
    actor = _review_actor(reviewer_id)
    try:
        with ReviewStore(database) as store:
            store.approve_search(
                case_id,
                reviewer_id=actor,
                reviewed_content_sha256=content_sha256,
                reason=reason,
            )
    except (ReviewError, sqlite3.Error, OSError) as error:
        _safe_review_error(error)
    typer.echo(f"updated=1 case_id={case_id} status=search_approved failed=0")


@review_app.command("approve-answer")
def review_approve_answer(
    database: Path = typer.Option(  # noqa: B008
        ..., "--db", exists=True, dir_okay=False, readable=True, writable=True
    ),
    case_id: str = typer.Option(..., "--case-id"),
    content_sha256: str = typer.Option(..., "--content-sha256"),
    reason: str = typer.Option(..., "--reason"),
    content_verified: bool = typer.Option(False, "--content-verified"),
    basis_verified: bool = typer.Option(False, "--basis-verified"),
    privacy_verified: bool = typer.Option(False, "--privacy-verified"),
    reviewer_id: str | None = typer.Option(None, "--reviewer-id"),
) -> None:
    """Approve answer use after an independent reviewer confirms all three gates."""
    if not all((content_verified, basis_verified, privacy_verified)):
        _review_cli_fail("verification_required", exit_code=2)
    actor = _review_actor(reviewer_id)
    try:
        with ReviewStore(database) as store:
            store.approve_answer(
                case_id,
                reviewer_id=actor,
                reviewed_content_sha256=content_sha256,
                reason=reason,
                content_verified=content_verified,
                basis_verified=basis_verified,
                privacy_verified=privacy_verified,
            )
    except (ReviewError, sqlite3.Error, OSError) as error:
        _safe_review_error(error)
    typer.echo(f"updated=1 case_id={case_id} status=approved failed=0")


@review_app.command("reject")
def review_reject(
    database: Path = typer.Option(  # noqa: B008
        ..., "--db", exists=True, dir_okay=False, readable=True, writable=True
    ),
    case_id: str = typer.Option(..., "--case-id"),
    content_sha256: str = typer.Option(..., "--content-sha256"),
    reason: str = typer.Option(..., "--reason"),
    reviewer_id: str | None = typer.Option(None, "--reviewer-id"),
) -> None:
    """Reject one nonterminal candidate, making the review state terminal."""
    actor = _review_actor(reviewer_id)
    try:
        with ReviewStore(database) as store:
            store.reject(
                case_id,
                reviewer_id=actor,
                reviewed_content_sha256=content_sha256,
                reason=reason,
            )
    except (ReviewError, sqlite3.Error, OSError) as error:
        _safe_review_error(error)
    typer.echo(f"updated=1 case_id={case_id} status=rejected failed=0")


def _load_review_manifest(path: Path, expected_sha256: str) -> SegmentManifest:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ReviewValidationError("manifest cannot be read") from error
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ReviewValidationError("manifest hash mismatch")
    return SegmentManifest.from_bytes(raw)


@review_app.command("run")
def review_run(
    mode: str = typer.Option(..., "--mode"),
    database: Path = typer.Option(  # noqa: B008
        ..., "--db", exists=True, dir_okay=False, readable=True, writable=True
    ),
    manifest: Path = typer.Option(  # noqa: B008
        ..., "--manifest", exists=True, dir_okay=False, readable=True
    ),
    manifest_sha256: str = typer.Option(..., "--manifest-sha256"),
    reason: str = typer.Option(..., "--reason"),
    content_verified: bool = typer.Option(False, "--content-verified"),
    basis_verified: bool = typer.Option(False, "--basis-verified"),
    privacy_verified: bool = typer.Option(False, "--privacy-verified"),
    reviewer_id: str | None = typer.Option(None, "--reviewer-id"),
) -> None:
    """Review every content-addressed case in a canonical segment manifest."""
    updated = 0
    if mode not in {"critical-fields-all", "answer-and-basis-all"}:
        _review_cli_fail("invalid_mode", exit_code=2)
    if mode == "answer-and-basis-all" and not all(
        (content_verified, basis_verified, privacy_verified)
    ):
        _review_cli_fail("verification_required", exit_code=2)
    actor = _review_actor(reviewer_id)
    try:
        reason = validate_review_reason(reason)
        parsed = _load_review_manifest(manifest, manifest_sha256)
        with ReviewStore(database) as store:
            canonical_cases = tuple(
                store.canonical_reference(reference.case_id)
                for reference in parsed.cases
            )
        for supplied, canonical in zip(parsed.cases, canonical_cases, strict=True):
            if supplied.content_sha256 != canonical.content_sha256:
                raise ReviewConflictError("canonical content binding mismatch")
        with ReviewStore(database) as store:
            completed_cases = tuple(
                store.run_case_complete(
                    cast(RunMode, mode),
                    reference=reference,
                    manifest_sha256=manifest_sha256,
                )
                for reference in canonical_cases
            )
        for reference, completed in zip(canonical_cases, completed_cases, strict=True):
            if completed:
                continue
            typer.echo(
                f"case_id={reference.case_id} content_sha256={reference.content_sha256}"
            )
            for location in reference.source_locations:
                bbox = ",".join(f"{coordinate:g}" for coordinate in location.bbox)
                typer.echo(
                    f"page_id={location.page_id} bbox={bbox} "
                    f"reason_code={location.reason_code} count={location.count}"
                )
            try:
                confirmed = typer.confirm("confirm reviewed metadata", default=False)
            except typer.Abort:
                _review_cli_fail("confirmation_required", exit_code=2, updated=updated)
            if not confirmed:
                _review_cli_fail("confirmation_required", exit_code=2, updated=updated)
            with ReviewStore(database) as store:
                updated += store.run_mode(
                    cast(RunMode, mode),
                    cases=(reference,),
                    reviewer_id=actor,
                    reason=reason,
                    content_verified=content_verified,
                    basis_verified=basis_verified,
                    privacy_verified=privacy_verified,
                    manifest_sha256=manifest_sha256,
                )
    except (ReviewError, sqlite3.Error, OSError) as error:
        _safe_review_error(error, updated=updated)
    typer.echo(f"updated={updated} mode={mode} failed=0")


@review_app.command("approve-search-batch")
def review_approve_search_batch(
    database: Path = typer.Option(  # noqa: B008
        ..., "--db", exists=True, dir_okay=False, readable=True, writable=True
    ),
    manifest: Path = typer.Option(  # noqa: B008
        ..., "--manifest", exists=True, dir_okay=False, readable=True
    ),
    manifest_sha256: str = typer.Option(..., "--manifest-sha256"),
    reason: str = typer.Option(..., "--reason"),
    reviewer_id: str | None = typer.Option(None, "--reviewer-id"),
) -> None:
    """Atomically search-approve every case in one canonical hashed segment."""
    actor = _review_actor(reviewer_id)
    try:
        raw = manifest.read_bytes()
        with ReviewStore(database) as store:
            updated = store.approve_search_batch(
                raw,
                manifest_sha256=manifest_sha256,
                reviewer_id=actor,
                reason=reason,
            )
    except (ReviewError, sqlite3.Error, OSError) as error:
        _safe_review_error(error)
    typer.echo(f"updated={updated} failed=0")


@review_app.command("assert-ready")
def review_assert_ready(
    database: Path = typer.Option(  # noqa: B008
        ..., "--db", exists=True, dir_okay=False, readable=True, writable=True
    ),
    purpose: str = typer.Option("answer", "--purpose"),
) -> None:
    """Fail unless every queued case is eligible, emitting blocker counts only."""
    if purpose not in {"search", "answer"}:
        _review_cli_fail("invalid_purpose", exit_code=2)
    try:
        with ReviewStore(database) as store:
            report = store.assert_ready(purpose=cast(ReviewPurpose, purpose))
    except (ReviewError, sqlite3.Error, OSError) as error:
        _safe_review_error(error)
    blockers = ",".join(f"{code}:{count}" for code, count in report.blockers.items())
    failed = int(not report.ready)
    typer.echo(
        f"ready={int(report.ready)} total={report.total} eligible={report.eligible} "
        f"blockers={blockers or 'none'} failed={failed}"
    )
    if not report.ready:
        raise typer.Exit(code=1)


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
    maximum_digits = len(str(MAX_SUPPORTED_PDF_PAGE_COUNT))
    maximum_length = MAX_SUPPORTED_PDF_PAGE_COUNT * (maximum_digits * 2 + 2)
    if (
        not selection
        or len(selection) > maximum_length
        or selection.count(",") + 1 > MAX_SUPPORTED_PDF_PAGE_COUNT
        or re.fullmatch(r"[0-9,-]+", selection) is None
    ):
        raise ValueError("pages must be positive numbers or inclusive ranges")
    intervals: list[tuple[int, int]] = []
    selected_count = 0
    previous_end = 0
    for part in selection.split(","):
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2 or not all(bound.isdecimal() for bound in bounds):
                raise ValueError("pages must be positive numbers or inclusive ranges")
            start_text, end_text = bounds
        else:
            if not part.isdecimal():
                raise ValueError("pages must be positive numbers or inclusive ranges")
            start_text = end_text = part
        if len(start_text) > maximum_digits or len(end_text) > maximum_digits:
            raise ValueError("pages must be positive numbers or inclusive ranges")
        start, end = int(start_text), int(end_text)
        interval_count = end - start + 1
        if (
            start < 1
            or end < start
            or end > MAX_SUPPORTED_PDF_PAGE_COUNT
            or selected_count + interval_count > MAX_SUPPORTED_PDF_PAGE_COUNT
        ):
            raise ValueError("pages range is invalid")
        if start <= previous_end:
            raise ValueError("pages must be unique and ascending")
        intervals.append((start, end))
        selected_count += interval_count
        previous_end = end
    return tuple(page for start, end in intervals for page in range(start, end + 1))


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
        raise NativeExtractionError(
            "output must be an existing directory or a new path"
        )
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
        typer.echo(
            f"documents=0 pages=0 extracted=0 quarantined=0 failed=1 error={error}"
        )
        raise typer.Exit(code=2) from error

    selected = tuple(
        document for document in documents if document.edition_year in requested_years
    )
    if len(selected) != len(requested_years):
        typer.echo(
            "documents=0 pages=0 extracted=0 quarantined=0 failed=1 error=years are not in approved manifest"
        )
        raise typer.Exit(code=2)
    if any(document.extraction_method != "native" for document in selected):
        typer.echo(
            "documents=0 pages=0 extracted=0 quarantined=0 failed=1 error=selected document is not native"
        )
        raise typer.Exit(code=2)

    sources: list[tuple[Path, SourceDocument]] = []
    for document in selected:
        try:
            source = resolve_source(source_root, document)
            verify_source(source, document)
        except ManifestError as error:
            typer.echo(
                f"documents=0 pages=0 extracted=0 quarantined=0 failed=1 error={error}"
            )
            raise typer.Exit(code=1) from error
        sources.append((source, document))

    output_parent = output.parent
    output_parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.promotion-", dir=output_parent)
    )
    staging = workspace / "new-output"
    staging.mkdir()
    extracted = 0
    quarantined = 0
    page_count = 0
    promotion_started = False
    try:
        for source, document in sources:
            records = extract_document(source, document)
            write_document_jsonl(
                staging / f"{document.doc_id}.jsonl",
                records,
                document=document,
            )
            page_count += len(records)
            extracted += sum(record.status == "extracted" for record in records)
            quarantined += sum(record.status == "quarantined" for record in records)
        promotion_started = True
        _replace_output_directory(staging, output)
    except (NativeExtractionError, OSError) as error:
        if not promotion_started:
            shutil.rmtree(workspace, ignore_errors=True)
        typer.echo(
            f"documents={len(selected)} pages={page_count} extracted={extracted} quarantined={quarantined} failed=1 error={error}"
        )
        raise typer.Exit(code=1) from error

    typer.echo(
        f"documents={len(selected)} pages={page_count} extracted={extracted} quarantined={quarantined} failed={int(quarantined > 0)}"
    )
    if quarantined:
        raise typer.Exit(code=1)


@app.command("validate-ocr-models")
def validate_ocr_models(
    lock_path: Path = typer.Option(  # noqa: B008 - Typer declares CLI parameters this way.
        Path("config/models.lock.json"),
        "--lock",
        exists=True,
        dir_okay=False,
        readable=True,
    ),
    model_root: Path = typer.Option(  # noqa: B008
        Path("/opt/models/paddleocr"),
        "--model-root",
        exists=True,
        file_okay=False,
        readable=True,
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
        Path("config/models.lock.json"),
        "--lock",
        exists=True,
        dir_okay=False,
        readable=True,
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
        typer.echo(
            f"documents=0 pages=0 extracted=0 quarantined=0 failed=1 error={error}"
        )
        raise typer.Exit(code=2) from error
    selected = tuple(
        document for document in documents if document.edition_year == year
    )
    if len(selected) != 1 or selected[0].extraction_method != "ocr":
        typer.echo(
            "documents=0 pages=0 extracted=0 quarantined=0 failed=1 error=year is not approved for OCR"
        )
        raise typer.Exit(code=2)
    document = selected[0]
    if page_indexes[-1] > document.pdf_page_count:
        typer.echo(
            "documents=0 pages=0 extracted=0 quarantined=0 failed=1 error=pages exceed approved document"
        )
        raise typer.Exit(code=2)
    try:
        source = resolve_source(source_root, document)
        verify_source(source, document)
        lock = load_model_lock(lock_path)
        validate_installed_models(lock, model_root)
        adapter = create_paddle_adapter(lock, model_root)
    except (ManifestError, ModelLockError, OcrAdapterError) as error:
        typer.echo(
            f"documents=0 pages=0 extracted=0 quarantined=0 failed=1 error={error}"
        )
        raise typer.Exit(code=1) from error

    managed_output = output / f"{document.doc_id}.jsonl"
    try:
        output.mkdir(parents=True, exist_ok=True)
        unmanaged = sorted(
            path.name for path in output.iterdir() if path != managed_output
        )
        if unmanaged:
            raise OcrExtractionError("unmanaged output file prevents OCR extraction")
        records = extract_ocr_document(
            source, document, page_indexes, adapter, image_digest
        )
        write_ocr_jsonl(
            managed_output,
            records,
            document=document,
            expected_image_digest=image_digest,
            selected_page_indexes=page_indexes,
        )
        extracted = sum(record.status == "extracted" for record in records)
        quarantined = sum(record.status == "quarantined" for record in records)
    except (OcrExtractionError, NativeExtractionError, ValueError, OSError) as error:
        typer.echo(
            "documents=1 pages=0 extracted=0 quarantined=0 failed=1 error=" + str(error)
        )
        raise typer.Exit(code=1) from error
    typer.echo(
        f"documents=1 pages={len(records)} extracted={extracted} "
        f"quarantined={quarantined} failed={int(quarantined > 0)}"
    )
    if quarantined:
        raise typer.Exit(code=1)


@app.command("parse-metadata")
def parse_metadata(
    input_path: Path = typer.Option(..., "--input"),  # noqa: B008
    manifest: Path = typer.Option(..., "--manifest"),  # noqa: B008
    year: int = typer.Option(..., "--year"),
    pages: str = typer.Option(..., "--pages"),
) -> None:
    """Validate annual extractor JSONL and emit only canonical aggregate metadata."""
    error_code: str | None = None
    rendered: bytes | None = None
    try:
        metadata = build_parse_metadata(
            input_path,
            manifest_path=manifest,
            edition_year=year,
            pages=pages,
            expected_image_digest=os.environ.get("SEN_QA_INGESTION_IMAGE_DIGEST"),
        )
        rendered = canonical_metadata_bytes(metadata)
    except ParseMetadataError as error:
        error_code = error.code
    except (OSError, RecursionError, OverflowError, TypeError, ValueError):
        error_code = "parse_failed"
    if error_code is not None or rendered is None:
        typer.echo(f"failed=1 error_code={error_code or 'parse_failed'}")
        raise SystemExit(1) from None
    typer.echo(rendered.decode("ascii"))


_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "document.schema.json": Document,
    "case.schema.json": Case,
    "chunk.schema.json": Chunk,
    "law-ref.schema.json": LawRef,
    "case-relation.schema.json": CaseRelation,
    "search-result.schema.json": SearchResponse,
}


@app.command("export-schemas")
def export_schemas(
    output: Path = typer.Option(..., "--output", file_okay=False),  # noqa: B008 - Typer command parameter.
) -> None:
    """Write managed schemas deterministically; overwrite stale managed files and reject unknown ones."""
    output.mkdir(parents=True, exist_ok=True)
    managed_names = set(_SCHEMA_MODELS)
    unexpected = sorted(
        path.name
        for path in output.glob("*.schema.json")
        if path.name not in managed_names
    )
    if unexpected:
        typer.echo(f"unexpected schema files: {', '.join(unexpected)}")
        raise typer.Exit(code=2)

    for filename, model in _SCHEMA_MODELS.items():
        schema = model.model_json_schema()
        schema["$id"] = f"data/schemas/{filename}"
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        rendered = (
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        (output / filename).write_text(rendered, encoding="utf-8", newline="\n")
    typer.echo(f"exported={len(_SCHEMA_MODELS)} output={output}")


if __name__ == "__main__":
    app()
