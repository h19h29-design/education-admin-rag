from __future__ import annotations

import hashlib
import json
import socket
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from src.ingestion import review_broker as broker_module
from src.ingestion.review import (
    CanonicalReviewRegistry,
    ReviewReference,
    ReviewSourceLocation,
    ReviewStore,
    SegmentManifest,
)
from src.ingestion.review_broker import (
    BrokerConfig,
    BrokerError,
    dispatch_request,
    handle_connection,
    peer_actor,
    read_request,
)

CASE_ID = "senqa-2025-contract-contract-general-1"
CONTENT_SHA256 = "a" * 64


def _broker_config(tmp_path: Path) -> BrokerConfig:
    registry = CanonicalReviewRegistry.create(
        cases=(
            ReviewReference(
                case_id=CASE_ID,
                content_sha256=CONTENT_SHA256,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=13,
                        bbox=(10.0, 20.0, 100.0, 200.0),
                        reason_code="critical-fields-unverified",
                    ),
                ),
            ),
        )
    )
    rendered = registry.to_bytes()
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(rendered)
    expected_sha256 = hashlib.sha256(rendered).hexdigest()
    verified = CanonicalReviewRegistry.from_bytes(
        rendered, expected_sha256=expected_sha256
    )
    database = tmp_path / "review.sqlite3"
    with ReviewStore(database, canonical_registry=verified) as store:
        store.enqueue(
            CASE_ID,
            content_sha256=CONTENT_SHA256,
            reason="quality_gate",
        )
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    return BrokerConfig(
        database=database,
        registry=registry_path,
        expected_registry_sha256=expected_sha256,
        manifest_root=manifests,
    )


def test_broker_derives_actor_from_kernel_peer_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = SimpleNamespace(getsockopt=lambda *_args: struct.pack("3i", 1234, 501, 20))
    monkeypatch.setattr(
        "src.ingestion.review_broker.pwd.getpwuid",
        lambda uid: SimpleNamespace(pw_name="reviewer-a") if uid == 501 else None,
    )

    assert peer_actor(peer) == "uid:501:reviewer-a"


def test_broker_rejects_declared_actor_and_uses_peer_actor(tmp_path: Path) -> None:
    config = _broker_config(tmp_path)

    with pytest.raises(BrokerError, match="request_invalid") as captured:
        dispatch_request(
            config,
            {
                "operation": "verify-fields",
                "case_id": CASE_ID,
                "content_sha256": CONTENT_SHA256,
                "reason": "fields_checked",
                "reviewer_id": "forged-reviewer",
            },
            actor="uid:501:reviewer-a",
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_broker_executes_review_with_peer_actor_and_returns_value_free_status(
    tmp_path: Path,
) -> None:
    config = _broker_config(tmp_path)

    response = dispatch_request(
        config,
        {
            "operation": "verify-fields",
            "case_id": CASE_ID,
            "content_sha256": CONTENT_SHA256,
            "reason": "fields_checked",
        },
        actor="uid:501:reviewer-a",
    )

    assert response == {"failed": 0, "status": "needs_review", "updated": 1}
    with ReviewStore(config.database) as store:
        record = store.get(CASE_ID)
        assert record.critical_reviewer_id == "uid:501:reviewer-a"


def test_broker_assert_ready_returns_only_counts_and_blocker_codes(
    tmp_path: Path,
) -> None:
    config = _broker_config(tmp_path)

    response = dispatch_request(
        config,
        {"operation": "assert-ready", "purpose": "answer"},
        actor="uid:501:reviewer-a",
    )

    assert response["failed"] == 1
    assert response["ready"] is False
    assert response["total"] == 1
    assert response["eligible"] == 0
    assert set(response) == {"blockers", "eligible", "failed", "ready", "total"}
    blockers = cast(dict[object, object], response["blockers"])
    assert all(
        isinstance(key, str) and isinstance(value, int)
        for key, value in blockers.items()
    )


def test_broker_full_approval_flow_enforces_independent_peer_actor(
    tmp_path: Path,
) -> None:
    config = _broker_config(tmp_path)
    first_actor = "uid:501:reviewer-a"
    second_actor = "uid:502:reviewer-b"

    dispatch_request(
        config,
        {
            "operation": "verify-fields",
            "case_id": CASE_ID,
            "content_sha256": CONTENT_SHA256,
            "reason": "fields_checked",
        },
        actor=first_actor,
    )
    dispatch_request(
        config,
        {
            "operation": "approve-search",
            "case_id": CASE_ID,
            "content_sha256": CONTENT_SHA256,
            "reason": "search_checked",
        },
        actor=first_actor,
    )
    with pytest.raises(BrokerError, match="invalid_input"):
        dispatch_request(
            config,
            {
                "operation": "approve-answer",
                "case_id": CASE_ID,
                "content_sha256": CONTENT_SHA256,
                "reason": "answer_checked",
                "content_verified": True,
                "basis_verified": True,
                "privacy_verified": True,
            },
            actor=first_actor,
        )
    response = dispatch_request(
        config,
        {
            "operation": "approve-answer",
            "case_id": CASE_ID,
            "content_sha256": CONTENT_SHA256,
            "reason": "answer_checked",
            "content_verified": True,
            "basis_verified": True,
            "privacy_verified": True,
        },
        actor=second_actor,
    )

    assert response == {"failed": 0, "status": "approved", "updated": 1}


def test_broker_batch_manifest_is_root_bounded_and_hash_pinned(tmp_path: Path) -> None:
    config = _broker_config(tmp_path)
    actor = "uid:501:reviewer-a"
    dispatch_request(
        config,
        {
            "operation": "verify-fields",
            "case_id": CASE_ID,
            "content_sha256": CONTENT_SHA256,
            "reason": "fields_checked",
        },
        actor=actor,
    )
    manifest = SegmentManifest.create(
        segment_id="segment-1",
        cases=(ReviewReference(case_id=CASE_ID, content_sha256=CONTENT_SHA256),),
    )
    rendered = manifest.to_bytes()
    path = config.manifest_root / "segment-1.json"
    path.write_bytes(rendered)
    digest = hashlib.sha256(rendered).hexdigest()

    response = dispatch_request(
        config,
        {
            "operation": "approve-search-batch",
            "manifest_name": path.name,
            "manifest_sha256": digest,
            "reason": "segment_checked",
        },
        actor=actor,
    )

    assert response == {"failed": 0, "updated": 1}
    with pytest.raises(BrokerError, match="manifest_invalid"):
        dispatch_request(
            config,
            {
                "operation": "approve-search-batch",
                "manifest_name": "../segment-1.json",
                "manifest_sha256": digest,
                "reason": "segment_checked",
            },
            actor=actor,
        )


def test_socket_handler_emits_canonical_value_free_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _broker_config(tmp_path)
    monkeypatch.setattr(broker_module, "peer_actor", lambda _peer: "uid:501:reviewer-a")
    left, right = socket.socketpair()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            handled = executor.submit(handle_connection, left, config)
            right.sendall(b'{"operation":"assert-ready","purpose":"answer"}\n')
            response = json.loads(right.recv(4096).decode("ascii"))
            handled.result(timeout=1)
    finally:
        left.close()
        right.close()

    assert response == {
        "blockers": {"needs_review": 1},
        "eligible": 0,
        "failed": 1,
        "ready": False,
        "total": 1,
    }


def test_socket_handler_bounds_idle_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _broker_config(tmp_path)
    monkeypatch.setattr(broker_module, "peer_actor", lambda _peer: "uid:501:reviewer-a")

    class IdlePeer:
        timeout: float | None = None
        response = b""

        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def recv(self, _size: int) -> bytes:
            raise TimeoutError

        def sendall(self, response: bytes) -> None:
            self.response = response

    peer = IdlePeer()

    handle_connection(cast(socket.socket, peer), config)

    assert peer.timeout == 5.0
    assert json.loads(peer.response) == {"error_code": "request_invalid", "failed": 1}


def test_broker_request_reader_is_bounded_and_rejects_duplicate_json_keys() -> None:
    left, right = socket.socketpair()
    try:
        right.sendall(b'{"operation":"assert-ready","operation":"reject"}\n')
        with pytest.raises(BrokerError, match="request_invalid"):
            read_request(left)
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    try:
        right.sendall(b'{"operation":' + b"[" * 2_000 + b"0" + b"]" * 2_000 + b"}\n")
        with pytest.raises(BrokerError, match="request_invalid"):
            read_request(left)
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            sent = executor.submit(right.sendall, b"x" * 65_537)
            with pytest.raises(BrokerError, match="request_too_large"):
                read_request(left)
            sent.result(timeout=1)
    finally:
        left.close()
        right.close()


def test_broker_errors_never_echo_untrusted_values(tmp_path: Path) -> None:
    config = _broker_config(tmp_path)
    sentinel = "PRIVATE_REVIEW_SENTINEL"

    with pytest.raises(BrokerError) as captured:
        dispatch_request(
            config,
            {"operation": sentinel},
            actor="uid:501:reviewer-a",
        )

    rendered = " ".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.__cause__),
            repr(captured.value.__context__),
            json.dumps(captured.value.code),
        )
    )
    assert sentinel not in rendered


def test_broker_sanitizes_corrupt_review_database_failures(tmp_path: Path) -> None:
    config = _broker_config(tmp_path)
    config.database.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(BrokerError, match="review_failed") as captured:
        dispatch_request(
            config,
            {"operation": "assert-ready", "purpose": "answer"},
            actor="uid:501:reviewer-a",
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_manual_review_runbook_launches_the_fixed_broker_boundary() -> None:
    runbook = Path("docs/runbooks/manual-review.md").read_text(encoding="utf-8")

    assert "src.ingestion.review_broker" in runbook
    assert "--network none" in runbook
    assert "--read-only" in runbook
    assert "--cap-drop ALL" in runbook
    assert '--user "$SEN_QA_SERVICE_UID:$SEN_QA_SERVICE_GID"' in runbook
    assert '--group-add "$SEN_QA_REVIEW_GID"' in runbook
    assert '"$SEN_QA_BROKER_IMAGE"' in runbook
    assert "-m 2750" in runbook
    assert '"$SEN_QA_SOURCE_DIR:/data/source:ro"' in runbook
    assert '"$SEN_QA_RAW_DIR:/data/raw:ro"' in runbook
    assert '"$SEN_QA_CANONICAL_DIR:/data/canonical:ro"' in runbook
    assert '"$SEN_QA_REVIEW_STATE_DIR:/data/review-state:rw"' in runbook
