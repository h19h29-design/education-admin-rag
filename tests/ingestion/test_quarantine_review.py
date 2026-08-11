from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.ingestion.parse_common import parser_page_from_raw_page
from src.ingestion.quarantine_review import (
    QuarantineResolutionError,
    ResolutionAnnotation,
    ResolutionSourceSpan,
    VerifiedQuarantineResolutionAuthority,
    append_resolution_event,
    create_resolution_draft,
    load_resolution_authority,
    reparse_with_resolution,
)
from tests.ingestion.test_page_continuation import _raw_page

RELEASE_ID = "corpus-20260810042914-2f1ca61e"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _quarantine_row() -> dict[str, object]:
    return {
        "doc_id": "fixture-2023",
        "edition_year": 2023,
        "location_id": "loc-" + "1" * 32,
        "page_ids": [7],
        "reason_code": "ambiguous_boundary",
        "source_spans": [
            {
                "bbox": [10.0, 20.0, 300.0, 40.0],
                "page_label": "7",
                "pdf_page_index": 7,
                "text_sha256": SHA_A,
            }
        ],
        "span_count": 1,
    }


def _quarantine_bytes(*rows: dict[str, object]) -> bytes:
    return b"".join(
        (
            json.dumps(
                row,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        for row in rows
    )


def _draft(quarantine_bytes: bytes) -> bytes:
    return create_resolution_draft(
        release_id=RELEASE_ID,
        registry_sha256=SHA_A,
        manifest_sha256=SHA_B,
        raw_authority_sha256=SHA_C,
        parser_authority_sha256=SHA_D,
        parser_quarantines_bytes=quarantine_bytes,
        parser_quarantines_sha256=hashlib.sha256(quarantine_bytes).hexdigest(),
    )


def _load_bytes(tmp_path: Path, raw: bytes):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "parser-quarantine-resolutions.json"
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return load_resolution_authority(
        path, expected_sha256=hashlib.sha256(raw).hexdigest()
    )


def test_draft_assigns_distinct_stable_occurrences_to_repeated_exact_rows(
    tmp_path: Path,
) -> None:
    """Catches repeated parser occurrences being silently deduplicated."""
    quarantine_bytes = _quarantine_bytes(_quarantine_row(), _quarantine_row())

    raw = _draft(quarantine_bytes)
    path = tmp_path / "parser-quarantine-resolutions.json"
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    expected_sha256 = hashlib.sha256(raw).hexdigest()
    authority = load_resolution_authority(path, expected_sha256=expected_sha256)

    assert authority.quarantine_count == 2
    assert [item.occurrence_ordinal for item in authority.resolutions] == [1, 2]
    assert len({item.occurrence_id for item in authority.resolutions}) == 2
    assert all(item.disposition == "unresolved" for item in authority.resolutions)
    assert all(item.annotations == () for item in authority.resolutions)
    assert authority.events == ()
    assert b"PRIVATE-QUARANTINE-TEXT" not in raw


def test_external_authority_wrapper_cannot_be_directly_initialized() -> None:
    """Catches downstream code bypassing the externally supplied file SHA."""
    with pytest.raises(TypeError):
        VerifiedQuarantineResolutionAuthority()  # type: ignore[call-arg]


def test_corrected_disposition_is_one_hash_chained_actor_event(
    tmp_path: Path,
) -> None:
    """Catches a corrected state being detached from reviewer and exact evidence."""
    authority = _load_bytes(tmp_path, _draft(_quarantine_bytes(_quarantine_row())))
    occurrence = authority.resolutions[0]
    annotation = ResolutionAnnotation(
        role="title",
        source_span=ResolutionSourceSpan(
            pdf_page_index=7,
            page_label="7",
            bbox=(10.0, 20.0, 300.0, 40.0),
            text_sha256=SHA_A,
        ),
    )

    resolved_raw = append_resolution_event(
        authority,
        occurrence_id=occurrence.occurrence_id,
        disposition="corrected",
        annotations=(annotation,),
        actor_id="uid:501:reviewer-a",
        event_id="event-0001",
        occurred_at="2026-08-10T10:00:00Z",
    )
    resolved = _load_bytes(tmp_path / "resolved", resolved_raw)

    assert resolved.resolutions[0].disposition == "corrected"
    assert resolved.resolutions[0].annotations == (annotation,)
    assert len(resolved.events) == 1
    event = resolved.events[0]
    assert event.actor_id == "uid:501:reviewer-a"
    assert event.previous_event_sha256 is None
    assert (
        event.event_sha256
        == hashlib.sha256(
            b"sen-qa-parser-quarantine-resolution-event-v1\0"
            + json.dumps(
                {
                    "actor_id": "uid:501:reviewer-a",
                    "annotations": [annotation.model_dump(mode="json")],
                    "disposition": "corrected",
                    "event_id": "event-0001",
                    "occurred_at": "2026-08-10T10:00:00Z",
                    "occurrence_id": occurrence.occurrence_id,
                    "previous_event_sha256": None,
                    "reviewed_occurrence_sha256": event.reviewed_occurrence_sha256,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()
    )
    assert b"PRIVATE-QUARANTINE-TEXT" not in resolved_raw


def test_corrected_disposition_requires_every_original_span_exactly_once(
    tmp_path: Path,
) -> None:
    """Catches corrected output silently dropping part of quarantined evidence."""
    row = _quarantine_row()
    row["source_spans"].append(  # type: ignore[union-attr]
        {
            "bbox": [10.0, 50.0, 300.0, 70.0],
            "page_label": "7",
            "pdf_page_index": 7,
            "text_sha256": SHA_B,
        }
    )
    row["span_count"] = 2
    authority = _load_bytes(tmp_path, _draft(_quarantine_bytes(row)))
    first = authority.resolutions[0].source_spans[0]

    with pytest.raises(QuarantineResolutionError, match="resolution_event_invalid"):
        append_resolution_event(
            authority,
            occurrence_id=authority.resolutions[0].occurrence_id,
            disposition="corrected",
            annotations=(ResolutionAnnotation(role="title", source_span=first),),
            actor_id="uid:501:reviewer-a",
            event_id="event-partial",
            occurred_at="2026-08-10T10:00:30Z",
        )


def test_upstream_failure_cannot_be_human_resolved(tmp_path: Path) -> None:
    """Catches human annotation being used instead of required re-extraction."""
    row = {
        "doc_id": "fixture-2024",
        "edition_year": 2024,
        "location_id": "loc-" + "2" * 32,
        "occurrence_count": 1,
        "page_ids": [9],
        "reason_code": "ocr-adapter-failed",
        "source_spans": [],
        "span_count": 0,
    }
    authority = _load_bytes(tmp_path, _draft(_quarantine_bytes(row)))

    with pytest.raises(QuarantineResolutionError, match="resolution_event_invalid"):
        append_resolution_event(
            authority,
            occurrence_id=authority.resolutions[0].occurrence_id,
            disposition="confirmed_noncase",
            annotations=(),
            actor_id="uid:501:reviewer-a",
            event_id="event-0002",
            occurred_at="2026-08-10T10:01:00Z",
        )


def test_loader_rejects_self_resealed_occurrence_provenance_drift(
    tmp_path: Path,
) -> None:
    """Catches occurrence evidence changing under a newly computed file digest."""
    raw = _draft(_quarantine_bytes(_quarantine_row()))
    payload = json.loads(raw)
    payload["resolutions"][0]["location_id"] = "loc-" + "9" * 32
    forged = (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    path = tmp_path / "forged.json"
    path.write_bytes(forged)
    os.chmod(path, 0o600)

    with pytest.raises(
        QuarantineResolutionError, match="resolution_authority_invalid"
    ) as caught:
        load_resolution_authority(
            path, expected_sha256=hashlib.sha256(forged).hexdigest()
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("case", ["wrong-sha", "mode", "symlink"])
def test_loader_requires_external_sha_owner_mode_and_nofollow(
    tmp_path: Path,
    case: str,
) -> None:
    """Catches the external seal or no-follow file boundary being bypassed."""
    raw = _draft(_quarantine_bytes(_quarantine_row()))
    target = tmp_path / "authority.json"
    target.write_bytes(raw)
    os.chmod(target, 0o600)
    path = target
    expected = hashlib.sha256(raw).hexdigest()
    if case == "wrong-sha":
        expected = "f" * 64
    elif case == "mode":
        os.chmod(target, 0o644)
    else:
        path = tmp_path / "link.json"
        path.symlink_to(target)

    with pytest.raises(QuarantineResolutionError, match="resolution_authority_invalid"):
        load_resolution_authority(path, expected_sha256=expected)


def test_loader_rejects_intermediate_directory_symlink(tmp_path: Path) -> None:
    """Catches an ancestor symlink redirecting a sealed authority read."""
    raw = _draft(_quarantine_bytes(_quarantine_row()))
    real = tmp_path / "real"
    real.mkdir()
    target = real / "authority.json"
    target.write_bytes(raw)
    os.chmod(target, 0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(QuarantineResolutionError, match="resolution_authority_invalid"):
        load_resolution_authority(
            alias / "authority.json",
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )


def test_loader_detects_ancestor_replacement_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an ancestor being replaced after descriptor validation."""
    raw = _draft(_quarantine_bytes(_quarantine_row()))
    safe = tmp_path / "safe"
    safe.mkdir()
    alias = safe / "authority-root"
    alias.mkdir()
    target = alias / "authority.json"
    target.write_bytes(raw)
    os.chmod(target, 0o600)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    attacker_target = attacker / "authority.json"
    attacker_target.write_bytes(raw)
    os.chmod(attacker_target, 0o600)
    displaced = safe / "authority-root-displaced"
    original_open = os.open
    swapped = False

    def swap_ancestor_then_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and path == "authority.json" and dir_fd is not None:
            swapped = True
            alias.rename(displaced)
            alias.symlink_to(attacker, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        "src.ingestion.quarantine_review.os.open", swap_ancestor_then_open
    )

    with pytest.raises(QuarantineResolutionError, match="resolution_authority_invalid"):
        load_resolution_authority(
            target,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )

    assert swapped


def test_confirmed_noncase_whole_occurrence_is_excluded_by_closed_reparse(
    tmp_path: Path,
) -> None:
    """Catches confirmed noncase evidence remaining a parser quarantine."""
    raw_page = _raw_page(
        doc_id="fixture-2023",
        year=2023,
        source="ocr",
        page={
            "pdf_page_index": 7,
            "page_label": "7",
            "lines": [
                {"text": "PRIVATE-QUARANTINE-TEXT", "bbox": [10.0, 20.0, 300.0, 40.0]}
            ],
        },
    )
    page = parser_page_from_raw_page(
        raw_page,
        normalized_text=None,
        page_role_hint="body",
        source_sha256=SHA_A,
        upstream_review_status="needs_review",
        critical_review_policy="all-fields-human-verification",
    )
    line = page.lines[0]
    row = {
        "doc_id": page.doc_id,
        "edition_year": page.edition_year,
        "location_id": "loc-" + "3" * 32,
        "page_ids": [7],
        "reason_code": "ambiguous_boundary",
        "source_spans": [
            {
                "bbox": list(line.bbox),
                "page_label": "7",
                "pdf_page_index": 7,
                "text_sha256": line.raw_text_sha256,
            }
        ],
        "span_count": 1,
    }
    quarantine_bytes = _quarantine_bytes(row)
    authority = _load_bytes(tmp_path, _draft(quarantine_bytes))
    resolved_raw = append_resolution_event(
        authority,
        occurrence_id=authority.resolutions[0].occurrence_id,
        disposition="confirmed_noncase",
        annotations=(),
        actor_id="uid:501:reviewer-a",
        event_id="event-noncase",
        occurred_at="2026-08-10T10:02:00Z",
    )
    resolved = _load_bytes(tmp_path / "resolved-noncase", resolved_raw)

    results = reparse_with_resolution(
        ((page,),),
        authority=resolved,
        expected_registry_sha256=SHA_A,
        expected_manifest_sha256=SHA_B,
        expected_raw_authority_sha256=SHA_C,
        expected_parser_authority_sha256=SHA_D,
        parser_quarantines_bytes=quarantine_bytes,
        expected_parser_quarantines_sha256=hashlib.sha256(quarantine_bytes).hexdigest(),
    )

    assert len(results) == 1
    assert results[0].cases == ()
    assert results[0].quarantines == ()
    assert b"PRIVATE-QUARANTINE-TEXT" not in resolved_raw
