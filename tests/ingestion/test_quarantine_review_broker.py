from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.ingestion import review_broker as broker_module
from src.ingestion.quarantine_review import (
    create_resolution_draft,
    load_resolution_authority,
)
from src.ingestion.review_broker import (
    BrokerConfig,
    BrokerError,
    QuarantineBrokerConfig,
    dispatch_request,
)

RELEASE_ID = "corpus-20260810042914-2f1ca61e"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
ACTOR = "uid:501:reviewer-a"


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


def _quarantine_row(*, suffix: str = "1") -> dict[str, object]:
    return {
        "doc_id": "fixture-2023",
        "edition_year": 2023,
        "location_id": "loc-" + suffix * 32,
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


def _draft(*rows: dict[str, object]) -> bytes:
    quarantine_bytes = b"".join(_canonical(row) for row in rows)
    return create_resolution_draft(
        release_id=RELEASE_ID,
        registry_sha256=SHA_A,
        manifest_sha256=SHA_B,
        raw_authority_sha256=SHA_C,
        parser_authority_sha256=SHA_D,
        parser_quarantines_bytes=quarantine_bytes,
        parser_quarantines_sha256=hashlib.sha256(quarantine_bytes).hexdigest(),
    )


def _config(tmp_path: Path, *rows: dict[str, object]) -> BrokerConfig:
    sidecar = tmp_path / "state" / "parser-quarantine-resolutions.json"
    sidecar.parent.mkdir(parents=True)
    raw = _draft(*(rows or (_quarantine_row(),)))
    sidecar.write_bytes(raw)
    os.chmod(sidecar, 0o600)
    annotation_root = tmp_path / "approved-annotations"
    annotation_root.mkdir()
    return BrokerConfig(
        database=tmp_path / "unused-review.sqlite3",
        registry=tmp_path / "unused-registry.json",
        expected_registry_sha256=SHA_A,
        manifest_root=tmp_path / "unused-manifests",
        quarantine=QuarantineBrokerConfig(
            sidecar=sidecar,
            annotation_manifest_root=annotation_root,
            reviewer_uids=(501, 502),
            annotation_owner_uids=(os.geteuid(),),
        ),
    )


def _head(config: BrokerConfig) -> str:
    assert config.quarantine is not None
    return hashlib.sha256(config.quarantine.sidecar.read_bytes()).hexdigest()


def _with_quarantine_paths(
    config: BrokerConfig,
    *,
    sidecar: Path,
    annotation_manifest_root: Path,
) -> BrokerConfig:
    return BrokerConfig(
        database=config.database,
        registry=config.registry,
        expected_registry_sha256=config.expected_registry_sha256,
        manifest_root=config.manifest_root,
        quarantine=QuarantineBrokerConfig(
            sidecar=sidecar,
            annotation_manifest_root=annotation_manifest_root,
            reviewer_uids=(501, 502),
            annotation_owner_uids=(os.geteuid(),),
        ),
    )


def _manifest(
    config: BrokerConfig,
    *,
    name: str,
    occurrence_id: str,
    event_id: str,
    disposition: str = "confirmed_noncase",
    annotations: list[dict[str, object]] | None = None,
) -> tuple[str, str]:
    assert config.quarantine is not None
    raw = _canonical(
        {
            "annotations": annotations or [],
            "disposition": disposition,
            "event_id": event_id,
            "occurred_at": "2026-08-10T10:00:00Z",
            "occurrence_id": occurrence_id,
            "schema_version": "sen-qa-parser-quarantine-annotation-manifest/v1",
        }
    )
    path = config.quarantine.annotation_manifest_root / name
    path.write_bytes(raw)
    os.chmod(path, 0o440)
    return name, hashlib.sha256(raw).hexdigest()


def _request(*, head: str, manifest_id: str, manifest_sha256: str) -> dict[str, object]:
    return {
        "operation": "resolve-quarantine",
        "expected_head_sha256": head,
        "annotation_manifest_id": manifest_id,
        "annotation_manifest_sha256": manifest_sha256,
    }


def _concurrent_cas_results(
    config: BrokerConfig,
) -> tuple[tuple[str, str], str]:
    assert config.quarantine is not None
    initial_head = _head(config)
    authority = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=initial_head
    )
    requests = []
    for index, resolution in enumerate(authority.resolutions, start=1):
        manifest_id, manifest_sha256 = _manifest(
            config,
            name=f"decision-{index}.json",
            occurrence_id=resolution.occurrence_id,
            event_id=f"event-{index}",
        )
        requests.append(
            _request(
                head=initial_head,
                manifest_id=manifest_id,
                manifest_sha256=manifest_sha256,
            )
        )

    def submit(request: dict[str, object]) -> str:
        try:
            dispatch_request(config, request, actor=ACTOR)
        except BrokerError as error:
            return error.code
        return "ok"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(submit, requests))
    assert len(results) == 2
    return (results[0], results[1]), initial_head


def test_quarantine_broker_appends_one_peer_actor_event_from_approved_manifest(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.quarantine is not None
    initial_head = _head(config)
    initial = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=initial_head
    )
    manifest_id, manifest_sha256 = _manifest(
        config,
        name="decision-0001.json",
        occurrence_id=initial.resolutions[0].occurrence_id,
        event_id="event-0001",
    )

    response = dispatch_request(
        config,
        _request(
            head=initial_head,
            manifest_id=manifest_id,
            manifest_sha256=manifest_sha256,
        ),
        actor=ACTOR,
    )

    assert response == {
        "failed": 0,
        "head_sha256": _head(config),
        "status": "resolution_appended",
        "updated": 1,
    }
    assert response["head_sha256"] != initial_head
    resolved = load_resolution_authority(
        config.quarantine.sidecar,
        expected_sha256=str(response["head_sha256"]),
    )
    assert len(resolved.events) == 1
    assert resolved.events[0].actor_id == ACTOR
    assert resolved.events[0].event_id == "event-0001"
    assert resolved.events[0].previous_event_sha256 is None
    assert resolved.resolutions[0].disposition == "confirmed_noncase"
    assert oct(config.quarantine.sidecar.stat().st_mode & 0o777) == "0o600"


def test_quarantine_request_never_accepts_inline_annotations_or_actor(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with pytest.raises(BrokerError, match="request_invalid"):
        dispatch_request(
            config,
            {
                "operation": "resolve-quarantine",
                "expected_head_sha256": _head(config),
                "annotation_manifest_id": "decision.json",
                "annotation_manifest_sha256": SHA_A,
                "annotations": [],
                "actor_id": "uid:999:forged",
            },
            actor=ACTOR,
        )


def test_quarantine_broker_rejects_stale_head_and_replay_without_second_event(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.quarantine is not None
    initial_head = _head(config)
    authority = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=initial_head
    )
    manifest_id, manifest_sha256 = _manifest(
        config,
        name="decision.json",
        occurrence_id=authority.resolutions[0].occurrence_id,
        event_id="event-replay",
    )
    request = _request(
        head=initial_head,
        manifest_id=manifest_id,
        manifest_sha256=manifest_sha256,
    )

    dispatch_request(config, request, actor=ACTOR)
    with pytest.raises(BrokerError, match="resolution_head_stale"):
        dispatch_request(config, request, actor=ACTOR)

    final_head = _head(config)
    final = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=final_head
    )
    assert len(final.events) == 1


def test_quarantine_broker_serializes_concurrent_compare_and_swap_clients(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, _quarantine_row(suffix="1"), _quarantine_row(suffix="2"))
    assert config.quarantine is not None
    results, _initial_head = _concurrent_cas_results(config)

    assert sorted(results) == ["ok", "resolution_head_stale"]
    final = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=_head(config)
    )
    assert len(final.events) == 1
    assert sum(item.disposition != "unresolved" for item in final.resolutions) == 1


def test_quarantine_broker_concurrent_compare_and_swap_stress(
    tmp_path: Path,
) -> None:
    for round_index in range(50):
        config = _config(
            tmp_path / f"round-{round_index}",
            _quarantine_row(suffix="1"),
            _quarantine_row(suffix="2"),
        )
        assert config.quarantine is not None

        results, _initial_head = _concurrent_cas_results(config)

        assert sorted(results) == ["ok", "resolution_head_stale"]
        final = load_resolution_authority(
            config.quarantine.sidecar, expected_sha256=_head(config)
        )
        assert len(final.events) == 1


def test_quarantine_lock_retries_macos_nofollow_create_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    assert config.quarantine is not None
    head = _head(config)
    authority = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=head
    )
    manifest_id, manifest_sha256 = _manifest(
        config,
        name="lock-race.json",
        occurrence_id=authority.resolutions[0].occurrence_id,
        event_id="event-lock-race",
    )
    real_open = os.open
    injected = False

    def fail_first_lock_create(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal injected
        if (
            not injected
            and dir_fd is not None
            and os.fspath(path).endswith(".lock")
            and flags & os.O_CREAT
        ):
            injected = True
            raise FileNotFoundError(errno.ENOENT, "injected lock creation race")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(broker_module.os, "open", fail_first_lock_create)

    response = dispatch_request(
        config,
        _request(
            head=head,
            manifest_id=manifest_id,
            manifest_sha256=manifest_sha256,
        ),
        actor=ACTOR,
    )

    assert injected is True
    assert response["status"] == "resolution_appended"


def test_quarantine_broker_preserves_two_sequential_hash_chained_decisions(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, _quarantine_row(suffix="1"), _quarantine_row(suffix="2"))
    assert config.quarantine is not None
    first_head = _head(config)
    authority = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=first_head
    )
    corrected = authority.resolutions[0]
    manifest_id, manifest_sha256 = _manifest(
        config,
        name="corrected.json",
        occurrence_id=corrected.occurrence_id,
        event_id="event-corrected",
        disposition="corrected",
        annotations=[
            {
                "role": "title",
                "source_span": corrected.source_spans[0].model_dump(mode="json"),
            }
        ],
    )
    first = dispatch_request(
        config,
        _request(
            head=first_head,
            manifest_id=manifest_id,
            manifest_sha256=manifest_sha256,
        ),
        actor=ACTOR,
    )
    second_occurrence = authority.resolutions[1]
    manifest_id, manifest_sha256 = _manifest(
        config,
        name="noncase.json",
        occurrence_id=second_occurrence.occurrence_id,
        event_id="event-noncase",
    )
    second = dispatch_request(
        config,
        _request(
            head=str(first["head_sha256"]),
            manifest_id=manifest_id,
            manifest_sha256=manifest_sha256,
        ),
        actor="uid:502:reviewer-b",
    )

    final = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=str(second["head_sha256"])
    )
    assert [event.event_id for event in final.events] == [
        "event-corrected",
        "event-noncase",
    ]
    assert final.events[1].previous_event_sha256 == final.events[0].event_sha256
    assert {item.disposition for item in final.resolutions} == {
        "confirmed_noncase",
        "corrected",
    }


def test_quarantine_broker_sanitizes_invalid_resolution_decision_context(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.quarantine is not None
    head = _head(config)
    authority = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=head
    )
    manifest_id, manifest_sha256 = _manifest(
        config,
        name="invalid-decision.json",
        occurrence_id=authority.resolutions[0].occurrence_id,
        event_id="event-invalid-decision",
        disposition="corrected",
        annotations=[],
    )

    with pytest.raises(BrokerError, match="resolution_decision_invalid") as captured:
        dispatch_request(
            config,
            _request(
                head=head,
                manifest_id=manifest_id,
                manifest_sha256=manifest_sha256,
            ),
            actor=ACTOR,
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("actor", "error_code"),
    [
        ("uid:999:unapproved", "peer_not_authorized"),
        ("uid:0:root", "peer_not_authorized"),
    ],
)
def test_quarantine_broker_requires_pinned_nonroot_peer_uid(
    tmp_path: Path, actor: str, error_code: str
) -> None:
    config = _config(tmp_path)
    with pytest.raises(BrokerError, match=error_code):
        dispatch_request(
            config,
            _request(
                head=_head(config),
                manifest_id="decision.json",
                manifest_sha256=SHA_A,
            ),
            actor=actor,
        )


@pytest.mark.parametrize(
    "invalid_kind",
    ["traversal", "symlink", "fifo", "duplicate", "malformed", "oversize"],
)
def test_quarantine_broker_rejects_unapproved_or_malformed_annotation_manifest(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    config = _config(tmp_path)
    assert config.quarantine is not None
    head = _head(config)
    authority = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=head
    )
    valid_id, valid_sha = _manifest(
        config,
        name="valid.json",
        occurrence_id=authority.resolutions[0].occurrence_id,
        event_id="event-invalid",
    )
    manifest_id = valid_id
    digest = valid_sha
    root = config.quarantine.annotation_manifest_root
    if invalid_kind == "traversal":
        manifest_id = "../valid.json"
    elif invalid_kind == "symlink":
        link = root / "link.json"
        link.symlink_to(root / valid_id)
        manifest_id = link.name
    elif invalid_kind == "fifo":
        fifo = root / "fifo.json"
        os.mkfifo(fifo)
        manifest_id = fifo.name
    elif invalid_kind == "duplicate":
        raw = (
            b'{"annotations":[],"annotations":[],"disposition":"confirmed_noncase",'
            b'"event_id":"event-invalid","occurred_at":"2026-08-10T10:00:00Z",'
            + f'"occurrence_id":"{authority.resolutions[0].occurrence_id}",'.encode()
            + b'"schema_version":"sen-qa-parser-quarantine-annotation-manifest/v1"}\n'
        )
        path = root / "duplicate.json"
        path.write_bytes(raw)
        os.chmod(path, 0o440)
        manifest_id = path.name
        digest = hashlib.sha256(raw).hexdigest()
    elif invalid_kind == "malformed":
        raw = _canonical(
            {
                "annotations": [{"role": "title", "source_span": {}}],
                "disposition": "corrected",
                "event_id": "event-invalid",
                "occurred_at": "2026-08-10T10:00:00Z",
                "occurrence_id": authority.resolutions[0].occurrence_id,
                "schema_version": "sen-qa-parser-quarantine-annotation-manifest/v1",
            }
        )
        path = root / "malformed.json"
        path.write_bytes(raw)
        os.chmod(path, 0o440)
        manifest_id = path.name
        digest = hashlib.sha256(raw).hexdigest()
    else:
        raw = b"{" + b" " * (2 * 1024 * 1024) + b"}\n"
        path = root / "oversize.json"
        path.write_bytes(raw)
        os.chmod(path, 0o440)
        manifest_id = path.name
        digest = hashlib.sha256(raw).hexdigest()

    with pytest.raises(BrokerError, match="annotation_manifest_invalid") as captured:
        dispatch_request(
            config,
            _request(
                head=head,
                manifest_id=manifest_id,
                manifest_sha256=digest,
            ),
            actor=ACTOR,
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_quarantine_broker_rejects_sidecar_mode_drift(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.quarantine is not None
    os.chmod(config.quarantine.sidecar, 0o640)

    with pytest.raises(BrokerError, match="resolution_authority_invalid"):
        dispatch_request(
            config,
            _request(
                head=_head(config),
                manifest_id="unused.json",
                manifest_sha256=SHA_A,
            ),
            actor=ACTOR,
        )


@pytest.mark.parametrize("invalid_kind", ["symlink", "fifo", "oversize"])
def test_quarantine_broker_rejects_nonregular_or_oversize_sidecar(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    config = _config(tmp_path)
    assert config.quarantine is not None
    sidecar = config.quarantine.sidecar
    if invalid_kind == "symlink":
        target = sidecar.with_name("saved-sidecar.json")
        sidecar.replace(target)
        sidecar.symlink_to(target)
    elif invalid_kind == "fifo":
        sidecar.unlink()
        os.mkfifo(sidecar)
    else:
        with sidecar.open("wb") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        os.chmod(sidecar, 0o600)

    with pytest.raises(BrokerError, match="resolution_authority_invalid"):
        dispatch_request(
            config,
            _request(
                head=SHA_A,
                manifest_id="unused.json",
                manifest_sha256=SHA_A,
            ),
            actor=ACTOR,
        )


def test_quarantine_broker_rejects_intermediate_sidecar_directory_symlink(
    tmp_path: Path,
) -> None:
    base = _config(tmp_path / "base")
    assert base.quarantine is not None
    real_root = tmp_path / "real-root"
    real_state = real_root / "state"
    real_state.mkdir(parents=True)
    sidecar = real_state / "parser-quarantine-resolutions.json"
    sidecar.write_bytes(base.quarantine.sidecar.read_bytes())
    os.chmod(sidecar, 0o600)
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "alias").symlink_to(real_root, target_is_directory=True)
    config = _with_quarantine_paths(
        base,
        sidecar=safe / "alias" / "state" / sidecar.name,
        annotation_manifest_root=base.quarantine.annotation_manifest_root,
    )

    with pytest.raises(BrokerError, match="resolution_lock_invalid"):
        dispatch_request(
            config,
            _request(
                head=hashlib.sha256(sidecar.read_bytes()).hexdigest(),
                manifest_id="unused.json",
                manifest_sha256=SHA_A,
            ),
            actor=ACTOR,
        )


def test_quarantine_broker_rejects_intermediate_manifest_directory_symlink(
    tmp_path: Path,
) -> None:
    base = _config(tmp_path / "base")
    assert base.quarantine is not None
    real_root = tmp_path / "real-root"
    approved = real_root / "approved"
    approved.mkdir(parents=True)
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "alias").symlink_to(real_root, target_is_directory=True)
    config = _with_quarantine_paths(
        base,
        sidecar=base.quarantine.sidecar,
        annotation_manifest_root=safe / "alias" / "approved",
    )
    head = _head(config)
    authority = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=head
    )
    manifest_id, manifest_sha256 = _manifest(
        config,
        name="decision.json",
        occurrence_id=authority.resolutions[0].occurrence_id,
        event_id="event-intermediate-link",
    )

    with pytest.raises(BrokerError, match="annotation_manifest_invalid"):
        dispatch_request(
            config,
            _request(
                head=head,
                manifest_id=manifest_id,
                manifest_sha256=manifest_sha256,
            ),
            actor=ACTOR,
        )


def test_quarantine_broker_holds_sidecar_parent_across_ancestor_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _config(tmp_path / "base")
    assert base.quarantine is not None
    safe = tmp_path / "safe"
    alias = safe / "alias"
    state = alias / "state"
    state.mkdir(parents=True)
    sidecar_name = "parser-quarantine-resolutions.json"
    initial_raw = base.quarantine.sidecar.read_bytes()
    (state / sidecar_name).write_bytes(initial_raw)
    os.chmod(state / sidecar_name, 0o600)
    attacker = tmp_path / "attacker"
    attacker_state = attacker / "state"
    attacker_state.mkdir(parents=True)
    attacker_sidecar = attacker_state / sidecar_name
    attacker_sidecar.write_bytes(initial_raw)
    os.chmod(attacker_sidecar, 0o600)
    config = _with_quarantine_paths(
        base,
        sidecar=state / sidecar_name,
        annotation_manifest_root=base.quarantine.annotation_manifest_root,
    )
    initial_head = hashlib.sha256(initial_raw).hexdigest()
    authority = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=initial_head
    )
    manifest_id, manifest_sha256 = _manifest(
        config,
        name="aba-sidecar.json",
        occurrence_id=authority.resolutions[0].occurrence_id,
        event_id="event-aba-sidecar",
    )
    parked = safe / "original-alias"
    real_open = os.open
    swapped = False

    def swap_ancestor_then_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        rendered = os.fspath(path)
        is_sidecar_leaf = (
            dir_fd is None and Path(rendered) == config.quarantine.sidecar
        ) or (dir_fd is not None and rendered == sidecar_name)
        if not swapped and is_sidecar_leaf:
            alias.rename(parked)
            alias.symlink_to(attacker, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(broker_module.os, "open", swap_ancestor_then_open)
    response = dispatch_request(
        config,
        _request(
            head=initial_head,
            manifest_id=manifest_id,
            manifest_sha256=manifest_sha256,
        ),
        actor=ACTOR,
    )

    original_raw = (parked / "state" / sidecar_name).read_bytes()
    assert response["head_sha256"] == hashlib.sha256(original_raw).hexdigest()
    assert original_raw != initial_raw
    assert attacker_sidecar.read_bytes() == initial_raw


def test_quarantine_broker_holds_manifest_parent_across_ancestor_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _config(tmp_path / "base")
    assert base.quarantine is not None
    safe = tmp_path / "safe"
    alias = safe / "alias"
    approved = alias / "approved"
    approved.mkdir(parents=True)
    attacker = tmp_path / "attacker"
    attacker_approved = attacker / "approved"
    attacker_approved.mkdir(parents=True)
    config = _with_quarantine_paths(
        base,
        sidecar=base.quarantine.sidecar,
        annotation_manifest_root=approved,
    )
    head = _head(config)
    authority = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=head
    )
    manifest_id, manifest_sha256 = _manifest(
        config,
        name="aba-manifest.json",
        occurrence_id=authority.resolutions[0].occurrence_id,
        event_id="event-aba-manifest",
    )
    attacker_manifest = attacker_approved / manifest_id
    attacker_manifest.write_bytes(b'{"attacker":true}\n')
    os.chmod(attacker_manifest, 0o440)
    parked = safe / "original-alias"
    real_open = os.open
    swapped = False

    def swap_ancestor_then_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        rendered = os.fspath(path)
        is_manifest_leaf = (
            dir_fd is None
            and Path(rendered)
            == config.quarantine.annotation_manifest_root / manifest_id
        ) or (dir_fd is not None and rendered == manifest_id)
        if not swapped and is_manifest_leaf:
            alias.rename(parked)
            alias.symlink_to(attacker, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(broker_module.os, "open", swap_ancestor_then_open)
    response = dispatch_request(
        config,
        _request(
            head=head,
            manifest_id=manifest_id,
            manifest_sha256=manifest_sha256,
        ),
        actor=ACTOR,
    )

    assert response["status"] == "resolution_appended"


@pytest.mark.parametrize("crash_point", ["before", "after"])
def test_quarantine_sidecar_replace_is_complete_across_rename_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    config = _config(tmp_path)
    assert config.quarantine is not None
    initial_raw = config.quarantine.sidecar.read_bytes()
    initial_head = hashlib.sha256(initial_raw).hexdigest()
    authority = load_resolution_authority(
        config.quarantine.sidecar, expected_sha256=initial_head
    )
    manifest_id, manifest_sha256 = _manifest(
        config,
        name="crash.json",
        occurrence_id=authority.resolutions[0].occurrence_id,
        event_id="event-crash",
    )
    real_replace = os.replace

    class SimulatedCrash(BaseException):
        pass

    def crash_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if crash_point == "after":
            real_replace(
                source,
                target,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
        raise SimulatedCrash

    monkeypatch.setattr(broker_module.os, "replace", crash_replace)
    with pytest.raises(SimulatedCrash):
        dispatch_request(
            config,
            _request(
                head=initial_head,
                manifest_id=manifest_id,
                manifest_sha256=manifest_sha256,
            ),
            actor=ACTOR,
        )

    final_raw = config.quarantine.sidecar.read_bytes()
    if crash_point == "before":
        assert final_raw == initial_raw
    else:
        assert final_raw != initial_raw
        final = load_resolution_authority(
            config.quarantine.sidecar,
            expected_sha256=hashlib.sha256(final_raw).hexdigest(),
        )
        assert len(final.events) == 1
    assert not tuple(config.quarantine.sidecar.parent.glob(".*.tmp"))


def test_quarantine_broker_errors_do_not_echo_manifest_values(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sentinel = "PRIVATE_ANNOTATION_SENTINEL"
    with pytest.raises(BrokerError) as captured:
        dispatch_request(
            config,
            _request(
                head=_head(config),
                manifest_id=sentinel,
                manifest_sha256=SHA_A,
            ),
            actor=ACTOR,
        )
    assert sentinel not in repr(captured.value)


def test_manual_review_runbook_wires_root_fixed_quarantine_broker_contract() -> None:
    runbook = Path("docs/runbooks/manual-review.md").read_text(encoding="utf-8")

    assert (
        "--quarantine-sidecar /data/review-state/parser-quarantine-resolutions.json"
        in runbook
    )
    assert "--annotation-manifest-root /data/approved-quarantine-annotations" in runbook
    assert (
        '"$SEN_QA_ANNOTATION_DIR:/data/approved-quarantine-annotations:ro"' in runbook
    )
    assert "--quarantine-reviewer-uid" in runbook
    assert "--annotation-owner-uid" in runbook
    assert '"operation":"resolve-quarantine"' in runbook
    assert '"expected_head_sha256"' in runbook
    assert '"annotation_manifest_id"' in runbook
    assert '"annotation_manifest_sha256"' in runbook
    assert "두 실제 NAS reviewer UID" in runbook


def test_broker_main_builds_root_fixed_quarantine_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture(socket_path: Path, config: BrokerConfig) -> None:
        captured.update(socket=socket_path, config=config)

    monkeypatch.setattr(broker_module, "serve", capture)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "review-broker",
            "--socket",
            str(tmp_path / "review.sock"),
            "--database",
            str(tmp_path / "review.sqlite3"),
            "--registry",
            str(tmp_path / "registry.json"),
            "--registry-sha256",
            SHA_A,
            "--manifest-root",
            str(tmp_path / "manifests"),
            "--quarantine-sidecar",
            str(tmp_path / "parser-quarantine-resolutions.json"),
            "--annotation-manifest-root",
            str(tmp_path / "approved-annotations"),
            "--quarantine-reviewer-uid",
            "502",
            "--quarantine-reviewer-uid",
            "501",
            "--annotation-owner-uid",
            "0",
        ],
    )

    broker_module.main()

    config = captured["config"]
    assert isinstance(config, BrokerConfig)
    assert config.quarantine == QuarantineBrokerConfig(
        sidecar=tmp_path / "parser-quarantine-resolutions.json",
        annotation_manifest_root=tmp_path / "approved-annotations",
        reviewer_uids=(501, 502),
        annotation_owner_uids=(0,),
    )
