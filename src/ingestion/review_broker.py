"""Root-deployed, value-free Unix-socket broker for manual review actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import socket
import sqlite3
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

from src.ingestion.review import (
    CanonicalReviewRegistry,
    ReviewError,
    ReviewStore,
    SegmentManifest,
    VerifiedCanonicalReviewRegistry,
)

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_AUTHORITY_BYTES = 16 * 1024 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTOR_RE = re.compile(r"^uid:[0-9]{1,10}:[A-Za-z0-9_.-]{1,64}$")
_MANIFEST_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_PEERCRED_SIZE = struct.calcsize("3i")


class BrokerError(ValueError):
    """A fixed-code broker failure that never includes review values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _raise(code: str) -> NoReturn:
    raise BrokerError(code) from None


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    """Immutable paths and external registry authority fixed by root."""

    database: Path
    registry: Path
    expected_registry_sha256: str
    manifest_root: Path

    def __post_init__(self) -> None:
        if (
            not isinstance(self.database, Path)
            or not isinstance(self.registry, Path)
            or not isinstance(self.manifest_root, Path)
            or type(self.expected_registry_sha256) is not str
            or _HASH_RE.fullmatch(self.expected_registry_sha256) is None
        ):
            _raise("configuration_invalid")


def peer_actor(peer: Any) -> str:
    """Resolve the review actor exclusively from Linux SO_PEERCRED."""

    failure = False
    actor: str | None = None
    try:
        option = getattr(socket, "SO_PEERCRED", 17)
        raw = peer.getsockopt(socket.SOL_SOCKET, option, _PEERCRED_SIZE)
        if type(raw) is not bytes or len(raw) != _PEERCRED_SIZE:
            failure = True
        else:
            _pid, uid, _gid = struct.unpack("3i", raw)
            username = pwd.getpwuid(uid).pw_name
            actor = f"uid:{uid}:{username}"
            if _ACTOR_RE.fullmatch(actor) is None:
                failure = True
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError):
        failure = True
    if failure or actor is None:
        _raise("peer_identity_invalid")
    return actor


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    rendered: dict[str, object] = {}
    for key, value in pairs:
        if key in rendered:
            _raise("request_invalid")
        rendered[key] = value
    return rendered


def read_request(peer: socket.socket) -> dict[str, object]:
    """Read exactly one bounded, newline-terminated JSON request."""

    payload = bytearray()
    while b"\n" not in payload:
        if len(payload) > _MAX_REQUEST_BYTES:
            _raise("request_too_large")
        try:
            chunk = peer.recv(min(4096, _MAX_REQUEST_BYTES + 1 - len(payload)))
        except OSError:
            _raise("request_invalid")
        if not chunk:
            _raise("request_invalid")
        payload.extend(chunk)
        if len(payload) > _MAX_REQUEST_BYTES:
            _raise("request_too_large")
    line, remainder = bytes(payload).split(b"\n", maxsplit=1)
    if remainder or not line:
        _raise("request_invalid")
    parsed: object | None = None
    failed = False
    try:
        parsed = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
    except (
        BrokerError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        failed = True
    if failed or type(parsed) is not dict:
        _raise("request_invalid")
    return cast(dict[str, object], parsed)


def _read_regular(path: Path, *, error_code: str) -> bytes:
    descriptor: int | None = None
    rendered: bytes | None = None
    failed = False
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_AUTHORITY_BYTES:
            failed = True
        else:
            remaining = _MAX_AUTHORITY_BYTES + 1
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            rendered = b"".join(chunks)
            if (
                len(rendered) > _MAX_AUTHORITY_BYTES
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or len(rendered) != before.st_size
            ):
                failed = True
    except OSError:
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed or rendered is None:
        _raise(error_code)
    return rendered


def _verified_registry(config: BrokerConfig) -> VerifiedCanonicalReviewRegistry:
    raw = _read_regular(config.registry, error_code="registry_invalid")
    verified = None
    try:
        verified = CanonicalReviewRegistry.from_bytes(
            raw, expected_sha256=config.expected_registry_sha256
        )
    except (RecursionError, TypeError, ValueError):
        pass
    if verified is None:
        _raise("registry_invalid")
    return verified


def _manifest_bytes(
    config: BrokerConfig, *, name: object, expected_sha256: object
) -> tuple[bytes, SegmentManifest]:
    if (
        type(name) is not str
        or _MANIFEST_NAME_RE.fullmatch(name) is None
        or type(expected_sha256) is not str
        or _HASH_RE.fullmatch(expected_sha256) is None
    ):
        _raise("manifest_invalid")
    raw = _read_regular(config.manifest_root / name, error_code="manifest_invalid")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        _raise("manifest_invalid")
    manifest = None
    try:
        manifest = SegmentManifest.from_bytes(raw)
    except (RecursionError, TypeError, ValueError):
        pass
    if manifest is None:
        _raise("manifest_invalid")
    return raw, manifest


def _require_fields(request: dict[str, object], expected: set[str]) -> None:
    if set(request) != expected:
        _raise("request_invalid")


def _review_fields(request: dict[str, object]) -> tuple[str, str, str]:
    case_id = request.get("case_id")
    content_sha256 = request.get("content_sha256")
    reason = request.get("reason")
    if not all(type(value) is str for value in (case_id, content_sha256, reason)):
        _raise("request_invalid")
    return cast(str, case_id), cast(str, content_sha256), cast(str, reason)


def dispatch_request(
    config: BrokerConfig, request: dict[str, object], *, actor: str
) -> dict[str, object]:
    """Validate and execute one allowlisted broker operation."""

    if (
        type(request) is not dict
        or type(actor) is not str
        or _ACTOR_RE.fullmatch(actor) is None
    ):
        _raise("request_invalid")
    operation = request.get("operation")
    if type(operation) is not str:
        _raise("request_invalid")
    verified = _verified_registry(config)
    if not config.database.is_file() or config.database.is_symlink():
        _raise("database_invalid")
    response: dict[str, object] | None = None
    failure_code: str | None = None
    try:
        with ReviewStore(config.database, canonical_registry=verified) as store:
            if operation in {"verify-fields", "approve-search", "reject"}:
                _require_fields(
                    request,
                    {"operation", "case_id", "content_sha256", "reason"},
                )
                case_id, content_sha256, reason = _review_fields(request)
                if operation == "verify-fields":
                    store.verify_critical_fields(
                        case_id,
                        reviewer_id=actor,
                        reviewed_content_sha256=content_sha256,
                        reason=reason,
                    )
                    status = "needs_review"
                elif operation == "approve-search":
                    store.approve_search(
                        case_id,
                        reviewer_id=actor,
                        reviewed_content_sha256=content_sha256,
                        reason=reason,
                    )
                    status = "search_approved"
                else:
                    store.reject(
                        case_id,
                        reviewer_id=actor,
                        reviewed_content_sha256=content_sha256,
                        reason=reason,
                    )
                    status = "rejected"
                response = {"failed": 0, "status": status, "updated": 1}
            elif operation == "approve-answer":
                _require_fields(
                    request,
                    {
                        "operation",
                        "case_id",
                        "content_sha256",
                        "reason",
                        "content_verified",
                        "basis_verified",
                        "privacy_verified",
                    },
                )
                case_id, content_sha256, reason = _review_fields(request)
                flags = tuple(
                    request[name]
                    for name in (
                        "content_verified",
                        "basis_verified",
                        "privacy_verified",
                    )
                )
                if any(type(value) is not bool for value in flags):
                    _raise("request_invalid")
                store.approve_answer(
                    case_id,
                    reviewer_id=actor,
                    reviewed_content_sha256=content_sha256,
                    reason=reason,
                    content_verified=cast(bool, flags[0]),
                    basis_verified=cast(bool, flags[1]),
                    privacy_verified=cast(bool, flags[2]),
                )
                response = {"failed": 0, "status": "approved", "updated": 1}
            elif operation == "assert-ready":
                _require_fields(request, {"operation", "purpose"})
                purpose = request["purpose"]
                if purpose not in {"search", "answer"}:
                    _raise("request_invalid")
                report = store.assert_ready(purpose=purpose)
                response = {
                    "blockers": dict(sorted(report.blockers.items())),
                    "eligible": report.eligible,
                    "failed": int(not report.ready),
                    "ready": report.ready,
                    "total": report.total,
                }
            elif operation == "approve-search-batch":
                _require_fields(
                    request,
                    {
                        "operation",
                        "manifest_name",
                        "manifest_sha256",
                        "reason",
                    },
                )
                batch_reason = request["reason"]
                if type(batch_reason) is not str:
                    _raise("request_invalid")
                raw, _manifest = _manifest_bytes(
                    config,
                    name=request["manifest_name"],
                    expected_sha256=request["manifest_sha256"],
                )
                updated = store.approve_search_batch(
                    raw,
                    manifest_sha256=cast(str, request["manifest_sha256"]),
                    reviewer_id=actor,
                    reason=batch_reason,
                )
                response = {"failed": 0, "updated": updated}
            elif operation == "run":
                _require_fields(
                    request,
                    {
                        "operation",
                        "mode",
                        "manifest_name",
                        "manifest_sha256",
                        "case_id",
                        "reason",
                        "content_verified",
                        "basis_verified",
                        "privacy_verified",
                    },
                )
                mode = request["mode"]
                run_case_id = request["case_id"]
                run_reason = request["reason"]
                flags = tuple(
                    request[name]
                    for name in (
                        "content_verified",
                        "basis_verified",
                        "privacy_verified",
                    )
                )
                if (
                    mode not in {"critical-fields-all", "answer-and-basis-all"}
                    or type(run_case_id) is not str
                    or type(run_reason) is not str
                    or any(type(value) is not bool for value in flags)
                ):
                    _raise("request_invalid")
                _raw, manifest = _manifest_bytes(
                    config,
                    name=request["manifest_name"],
                    expected_sha256=request["manifest_sha256"],
                )
                references = tuple(
                    reference
                    for reference in manifest.cases
                    if reference.case_id == run_case_id
                )
                if len(references) != 1:
                    _raise("request_invalid")
                canonical = store.canonical_reference(run_case_id)
                if canonical.content_sha256 != references[0].content_sha256:
                    _raise("request_invalid")
                updated = store.run_mode(
                    mode,
                    cases=(canonical,),
                    reviewer_id=actor,
                    reason=run_reason,
                    content_verified=cast(bool, flags[0]),
                    basis_verified=cast(bool, flags[1]),
                    privacy_verified=cast(bool, flags[2]),
                    manifest_sha256=cast(str, request["manifest_sha256"]),
                )
                response = {"failed": 0, "updated": updated}
            else:
                _raise("request_invalid")
    except BrokerError:
        raise
    except ReviewError as error:
        failure_code = error.code
    except (OSError, RecursionError, sqlite3.Error, TypeError, ValueError):
        failure_code = "review_failed"
    if failure_code is not None:
        _raise(failure_code)
    if response is None:
        _raise("review_failed")
    return response


def _send_response(peer: socket.socket, response: dict[str, object]) -> None:
    rendered = (
        json.dumps(response, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    try:
        peer.sendall(rendered)
    except OSError:
        return


def handle_connection(peer: socket.socket, config: BrokerConfig) -> None:
    """Handle one request and emit only fixed errors or value-free metadata."""

    try:
        peer.settimeout(5.0)
        actor = peer_actor(peer)
        request = read_request(peer)
        response = dispatch_request(config, request, actor=actor)
    except BrokerError as error:
        response = {"error_code": error.code, "failed": 1}
    _send_response(peer, response)


def serve(socket_path: Path, config: BrokerConfig) -> None:
    """Serve review requests on one root-selected Unix socket."""

    if (
        not isinstance(socket_path, Path)
        or not socket_path.is_absolute()
        or socket_path.exists()
        or socket_path.is_symlink()
        or not socket_path.parent.is_dir()
        or socket_path.parent.is_symlink()
    ):
        _raise("socket_invalid")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o660)
        listener.listen(16)
        while True:
            peer, _address = listener.accept()
            with peer:
                handle_connection(peer, config)
    finally:
        listener.close()
        try:
            socket_path.unlink()
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the SEN-QA review broker")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--registry-sha256", required=True)
    parser.add_argument("--manifest-root", type=Path, required=True)
    arguments = parser.parse_args()
    config = BrokerConfig(
        database=arguments.database,
        registry=arguments.registry,
        expected_registry_sha256=arguments.registry_sha256,
        manifest_root=arguments.manifest_root,
    )
    serve(arguments.socket, config)


if __name__ == "__main__":
    main()
