from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from src.corpus.ids import make_case_id
from src.ingestion import review as review_module
from src.ingestion.review import (
    CanonicalReviewRegistry,
    ReviewConflictError,
    ReviewNotFoundError,
    ReviewReference,
    ReviewSourceLocation,
    ReviewStore,
    ReviewValidationError,
    SegmentManifest,
    VerifiedCanonicalReviewRegistry,
)

CONTENT_A = "a" * 64
CONTENT_B = "b" * 64
CONTENT_C = "c" * 64
CASE_1 = "senqa-2025-contract-contract-general-1"
CASE_2 = "senqa-2025-contract-contract-general-2"
CASE_3 = "senqa-2025-contract-contract-general-3"
CASE_4 = "senqa-2025-contract-contract-general-4"
CASE_5 = "senqa-2025-contract-contract-general-5"
CASE_6 = "senqa-2025-contract-contract-general-6"


def _unsigned_registry() -> CanonicalReviewRegistry:
    return CanonicalReviewRegistry.create(
        cases=[
            ReviewReference(
                case_id=case_id,
                content_sha256=content_hash,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=13,
                        bbox=(10.0, 20.0, 100.0, 200.0),
                        reason_code="critical-fields-unverified",
                    ),
                ),
            )
            for case_id, content_hash in (
                (CASE_1, CONTENT_A),
                (CASE_2, CONTENT_B),
                (CASE_3, CONTENT_A),
                (CASE_4, CONTENT_B),
                (CASE_5, CONTENT_A),
                (CASE_6, CONTENT_B),
            )
        ]
    )


def _verified_registry(
    registry: CanonicalReviewRegistry,
) -> VerifiedCanonicalReviewRegistry:
    rendered = registry.to_bytes()
    return CanonicalReviewRegistry.from_bytes(
        rendered, expected_sha256=hashlib.sha256(rendered).hexdigest()
    )


def _registry() -> VerifiedCanonicalReviewRegistry:
    return _verified_registry(_unsigned_registry())


@pytest.fixture
def review_store(tmp_path: Path) -> Iterator[ReviewStore]:
    current = datetime(2026, 8, 8, 1, 2, 3, tzinfo=UTC)
    with ReviewStore(
        tmp_path / "review.sqlite3",
        canonical_registry=_registry(),
        clock=lambda: current,
    ) as store:
        yield store


def _enqueue(
    store: ReviewStore, case_id: str = CASE_1, content_hash: str = CONTENT_A
) -> None:
    store.enqueue(
        case_id,
        content_sha256=content_hash,
        reason="quality_gate",
        actor_id="quality-gate",
    )


def _search_approve(
    store: ReviewStore,
    case_id: str = CASE_1,
    content_hash: str = CONTENT_A,
    reviewer_id: str = "reviewer-a",
) -> None:
    store.verify_critical_fields(
        case_id,
        reviewer_id=reviewer_id,
        reviewed_content_sha256=content_hash,
        reason="fields_checked",
    )
    store.approve_search(
        case_id,
        reviewer_id=reviewer_id,
        reviewed_content_sha256=content_hash,
        reason="search_checked",
    )


def test_quality_enqueue_is_first_state_transition_and_records_minimal_audit(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store)

    reviewed = review_store.get(CASE_1)
    assert reviewed.review_status == "needs_review"
    assert reviewed.search_eligible is False
    assert reviewed.answer_eligible is False
    event = review_store.events(CASE_1)[0]
    assert (event.before_state, event.after_state) == (
        "machine_extracted",
        "needs_review",
    )
    assert event.actor_id == "quality-gate"
    assert event.occurred_at == datetime(2026, 8, 8, 1, 2, 3, tzinfo=UTC)
    assert event.reviewed_content_sha256 == CONTENT_A
    assert event.batch_manifest_sha256 is None


def test_review_database_enables_wal_foreign_keys_and_busy_timeout(
    review_store: ReviewStore,
) -> None:
    settings = review_store.database_settings()
    assert settings.journal_mode == "wal"
    assert settings.foreign_keys is True
    assert settings.busy_timeout_ms >= 5_000
    assert settings.schema_version == 1


def test_fresh_store_requires_hashed_canonical_registry_and_opaque_case_ids(
    tmp_path: Path,
) -> None:
    with pytest.raises(ReviewValidationError, match="opaque canonical case ID"):
        ReviewReference(
            case_id="010-1234-5678",
            content_sha256=CONTENT_A,
            source_locations=(
                ReviewSourceLocation(
                    page_id=13,
                    bbox=(10.0, 20.0, 100.0, 200.0),
                    reason_code="critical-fields-unverified",
                ),
            ),
        )
    with pytest.raises(ReviewValidationError, match="canonical registry"):
        ReviewStore(tmp_path / "missing-registry.sqlite3")
    with pytest.raises(ReviewValidationError, match="verified canonical registry"):
        ReviewStore(
            tmp_path / "self-consistent-registry.sqlite3",
            canonical_registry=_unsigned_registry(),  # type: ignore[arg-type]
        )
    unsigned = _unsigned_registry()
    with pytest.raises(TypeError):
        VerifiedCanonicalReviewRegistry(  # type: ignore[call-arg]
            registry=unsigned,
            fingerprint_sha256=unsigned.fingerprint_sha256,
        )
    assert not hasattr(VerifiedCanonicalReviewRegistry, "_from_verified_bytes")

    with ReviewStore(
        tmp_path / "bound.sqlite3", canonical_registry=_registry()
    ) as store:
        with pytest.raises(ReviewConflictError, match="canonical content binding"):
            store.enqueue(
                CASE_1,
                content_sha256=CONTENT_B,
                reason="quality_gate",
                actor_id="quality-gate",
            )
        reference = store.canonical_reference(CASE_1)
        assert reference.content_sha256 == CONTENT_A
        assert reference.source_locations == (
            ReviewSourceLocation(
                page_id=13,
                bbox=(10.0, 20.0, 100.0, 200.0),
                reason_code="critical-fields-unverified",
            ),
        )


@pytest.mark.parametrize(
    "case_id",
    [
        "senqa-2025-contract-contract-general-110123456789",
        "senqa-2025-contract-contract-general-sk-proj-sensitivevalue123456",
        "senqa-2025-contract-contract-general-p0-deadbeef",
        "senqa-2101-contract-contract-general-1",
    ],
)
def test_registry_rejects_value_bearing_or_invalid_case_ids(case_id: str) -> None:
    with pytest.raises(
        ReviewValidationError, match="opaque canonical case ID"
    ) as error:
        ReviewReference(case_id=case_id, content_sha256=CONTENT_A)
    assert case_id not in str(error.value)


def test_sensitive_case_number_is_rejected_before_review_registry() -> None:
    sensitive_case_number = "010-1234-5678"

    with pytest.raises(ValueError, match="case number") as error:
        make_case_id(2025, "계약", "계약 일반", sensitive_case_number)

    assert sensitive_case_number not in str(error.value)
    assert hashlib.sha256(sensitive_case_number.encode()).hexdigest()[:12] not in str(
        error.value
    )


@pytest.mark.parametrize(
    "field_overrides",
    [
        {"page_id": "13"},
        {"page_id": True},
        {"count": "1"},
        {"count": True},
    ],
)
def test_canonical_source_location_requires_exact_integer_fields(
    field_overrides: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "page_id": 13,
        "bbox": (10.0, 20.0, 100.0, 200.0),
        "reason_code": "critical-fields-unverified",
        "count": 1,
    }
    values.update(field_overrides)
    with pytest.raises(ReviewValidationError):
        ReviewSourceLocation(**values)  # type: ignore[arg-type]


def test_reason_codes_reject_value_bearing_data_before_storage(
    review_store: ReviewStore,
) -> None:
    unsafe_reason = "010-1234-5678"
    with pytest.raises(ReviewValidationError, match="allowlisted") as provenance_error:
        ReviewSourceLocation(
            page_id=13,
            bbox=(10.0, 20.0, 100.0, 200.0),
            reason_code=unsafe_reason,  # type: ignore[arg-type]
        )
    with pytest.raises(ReviewValidationError, match="allowlisted") as event_error:
        review_store.enqueue(
            CASE_1,
            content_sha256=CONTENT_A,
            reason=unsafe_reason,
            actor_id="quality-gate",
        )

    assert unsafe_reason not in str(provenance_error.value)
    assert unsafe_reason not in str(event_error.value)
    with pytest.raises(ReviewNotFoundError):
        review_store.get(CASE_1)


def test_clean_case_human_review_provenance_can_seed_the_review_store(
    tmp_path: Path,
) -> None:
    """Catches clean quality results being impossible to bind for human review."""
    location = ReviewSourceLocation(
        page_id=13,
        bbox=(10.0, 20.0, 100.0, 200.0),
        reason_code="human-review-required",
    )
    registry = CanonicalReviewRegistry.create(
        cases=[
            ReviewReference(
                case_id=CASE_1,
                content_sha256=CONTENT_A,
                source_locations=(location,),
            )
        ]
    )
    verified = _verified_registry(registry)

    with ReviewStore(
        tmp_path / "clean-review.sqlite3",
        canonical_registry=verified,
    ) as store:
        store.register_candidate(CASE_1, content_sha256=CONTENT_A)
        reference = store.canonical_reference(CASE_1)

    assert reference.source_locations == (location,)


def test_review_store_exports_one_canonical_terminal_decision_snapshot(
    tmp_path: Path,
) -> None:
    """Catches storage accepting caller-authored approval labels or incomplete events."""
    registry = CanonicalReviewRegistry.create(
        cases=[
            ReviewReference(
                case_id=CASE_1,
                content_sha256=CONTENT_A,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=13,
                        bbox=(10.0, 20.0, 100.0, 200.0),
                        reason_code="human-review-required",
                    ),
                ),
            )
        ]
    )
    verified = _verified_registry(registry)
    with ReviewStore(
        tmp_path / "decision-review.sqlite3",
        canonical_registry=verified,
        clock=lambda: datetime(2026, 8, 8, 1, 2, 3, tzinfo=UTC),
    ) as store:
        _enqueue(store)
        _search_approve(store)
        store.approve_answer(
            CASE_1,
            reviewer_id="reviewer-b",
            reviewed_content_sha256=CONTENT_A,
            reason="answer_checked",
            content_verified=True,
            basis_verified=True,
            privacy_verified=True,
        )

        first = store.export_decision_snapshot()
        second = store.export_decision_snapshot()

    assert first == second
    payload = json.loads(first)
    assert payload["schema_version"] == "review-decision-snapshot-v1"
    assert payload["registry_fingerprint_sha256"] == verified.fingerprint_sha256
    assert len(payload["cases"]) == 1
    decision = payload["cases"][0]
    assert decision["case_id"] == CASE_1
    assert decision["promotion_envelope_sha256"] == CONTENT_A
    assert "canonical_case_sha256" not in decision
    assert decision["corrections"] == []
    assert decision["review_record"]["review_status"] == "approved"
    assert decision["review_record"]["version"] == len(decision["events"]) == 4
    assert [event["action"] for event in decision["events"]] == [
        "enqueue",
        "verify_fields",
        "approve_search",
        "approve_answer",
    ]


def test_review_decision_snapshot_rejects_nonterminal_or_partial_registry(
    tmp_path: Path,
) -> None:
    """Catches omitting unreviewed registry cases from a release snapshot."""
    registry = CanonicalReviewRegistry.create(
        cases=[
            ReviewReference(
                case_id=CASE_1,
                content_sha256=CONTENT_A,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=13,
                        bbox=(10.0, 20.0, 100.0, 200.0),
                        reason_code="human-review-required",
                    ),
                ),
            )
        ]
    )
    with ReviewStore(
        tmp_path / "partial-review.sqlite3",
        canonical_registry=_verified_registry(registry),
    ) as store:
        with pytest.raises(ReviewValidationError, match="complete terminal"):
            store.export_decision_snapshot()
        store.register_candidate(CASE_1, content_sha256=CONTENT_A)
        with pytest.raises(ReviewValidationError, match="complete terminal"):
            store.export_decision_snapshot()


def test_review_decision_snapshot_bounds_cases_events_and_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an unbounded DB snapshot exhausting memory before validation."""
    registry = CanonicalReviewRegistry.create(
        cases=[
            ReviewReference(
                case_id=case_id,
                content_sha256=content_hash,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=13,
                        bbox=(10.0, 20.0, 100.0, 200.0),
                        reason_code="human-review-required",
                    ),
                ),
            )
            for case_id, content_hash in (
                (CASE_1, CONTENT_A),
                (CASE_2, CONTENT_B),
            )
        ]
    )
    with ReviewStore(
        tmp_path / "bounded-decision-review.sqlite3",
        canonical_registry=_verified_registry(registry),
    ) as store:
        for case_id, content_hash in ((CASE_1, CONTENT_A), (CASE_2, CONTENT_B)):
            store.register_candidate(case_id, content_sha256=content_hash)
            store.reject(
                case_id,
                reviewer_id="reviewer-a",
                reason="bad_layout",
                reviewed_content_sha256=content_hash,
                expected_state="machine_extracted",
            )

        monkeypatch.setattr(review_module, "_MAX_DECISION_SNAPSHOT_CASES", 1)
        with pytest.raises(ReviewValidationError, match="bounded") as case_error:
            store.export_decision_snapshot()
        assert case_error.value.__cause__ is None
        assert case_error.value.__context__ is None

        monkeypatch.setattr(review_module, "_MAX_DECISION_SNAPSHOT_CASES", 2)
        monkeypatch.setattr(review_module, "_MAX_DECISION_SNAPSHOT_EVENTS", 1)
        with pytest.raises(ReviewValidationError, match="bounded") as event_error:
            store.export_decision_snapshot()
        assert event_error.value.__cause__ is None
        assert event_error.value.__context__ is None

        monkeypatch.setattr(review_module, "_MAX_DECISION_SNAPSHOT_EVENTS", 2)
        monkeypatch.setattr(review_module, "_MAX_DECISION_SNAPSHOT_BYTES", 1)
        with pytest.raises(ReviewValidationError, match="bounded") as byte_error:
            store.export_decision_snapshot()
        assert byte_error.value.__cause__ is None
        assert byte_error.value.__context__ is None


def test_review_decision_snapshot_sanitizes_malformed_database_rows(
    tmp_path: Path,
) -> None:
    """Catches corrupt SQLite values escaping through conversion diagnostics."""
    sentinel = "PRIVATE_DB_ROW_SENTINEL"
    registry = CanonicalReviewRegistry.create(
        cases=[
            ReviewReference(
                case_id=CASE_1,
                content_sha256=CONTENT_A,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=13,
                        bbox=(10.0, 20.0, 100.0, 200.0),
                        reason_code="human-review-required",
                    ),
                ),
            )
        ]
    )
    with ReviewStore(
        tmp_path / "malformed-decision-review.sqlite3",
        canonical_registry=_verified_registry(registry),
    ) as store:
        store.register_candidate(CASE_1, content_sha256=CONTENT_A)
        store.reject(
            CASE_1,
            reviewer_id="reviewer-a",
            reason="bad_layout",
            reviewed_content_sha256=CONTENT_A,
            expected_state="machine_extracted",
        )
        store._write_authorized = True
        try:
            store._connection.execute("PRAGMA ignore_check_constraints = ON")
            store._connection.execute(
                "UPDATE review_cases SET version = ? WHERE case_id = ?",
                (sentinel, CASE_1),
            )
        finally:
            store._write_authorized = False

        with pytest.raises(ReviewValidationError, match="invalid") as captured:
            store.export_decision_snapshot()

    rendered = "\n".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.__cause__),
            repr(captured.value.__context__),
        )
    )
    assert sentinel not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_registry_fingerprint_binds_sorted_quality_provenance() -> None:
    registry = CanonicalReviewRegistry.create(
        cases=[
            ReviewReference(
                case_id=CASE_2,
                content_sha256=CONTENT_B,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=13,
                        bbox=(10, 20, 100, 200),
                        reason_code="critical-fields-unverified",
                    ),
                    ReviewSourceLocation(
                        page_id=14,
                        bbox=(1, 2, 3, 4),
                        reason_code="restricted-pii",
                        count=2,
                    ),
                ),
            ),
            ReviewReference(
                case_id=CASE_1,
                content_sha256=CONTENT_A,
                source_locations=(
                    ReviewSourceLocation(
                        page_id=13,
                        bbox=(10, 20, 100, 200),
                        reason_code="critical-fields-unverified",
                    ),
                ),
            ),
        ]
    )
    rendered = registry.to_bytes()
    digest = hashlib.sha256(rendered).hexdigest()

    verified = CanonicalReviewRegistry.from_bytes(rendered, expected_sha256=digest)
    assert verified.registry == registry
    assert registry.fingerprint_sha256 == digest
    assert registry.cases[0].case_id == CASE_1
    assert registry.cases[1].source_locations[0].page_id == 13
    with pytest.raises(ReviewValidationError, match="registry hash mismatch"):
        CanonicalReviewRegistry.from_bytes(
            rendered.replace(b"critical-fields-unverified", b"different_reason"),
            expected_sha256=digest,
        )


def test_existing_store_rejects_a_different_canonical_registry(tmp_path: Path) -> None:
    database = tmp_path / "review.sqlite3"
    with ReviewStore(database, canonical_registry=_registry()):
        pass
    changed = _verified_registry(
        CanonicalReviewRegistry.create(
            cases=[
                ReviewReference(
                    case_id=CASE_1,
                    content_sha256=CONTENT_B,
                    source_locations=(
                        ReviewSourceLocation(
                            page_id=13,
                            bbox=(10.0, 20.0, 100.0, 200.0),
                            reason_code="critical-fields-unverified",
                        ),
                    ),
                )
            ]
        )
    )
    with pytest.raises(ReviewConflictError, match="registry fingerprint"):
        ReviewStore(database, canonical_registry=changed)


def test_concurrent_first_open_initializes_one_atomic_schema(tmp_path: Path) -> None:
    registry = _registry()

    def open_store(database: Path) -> int:
        with ReviewStore(database, canonical_registry=registry) as store:
            return store.database_settings().schema_version

    versions: list[int] = []
    for attempt in range(12):
        database = tmp_path / f"review-{attempt}.sqlite3"
        with ThreadPoolExecutor(max_workers=16) as executor:
            versions.extend(executor.map(open_store, [database] * 32))

    assert versions == [1] * (12 * 32)


@pytest.mark.parametrize(
    "extended_code",
    [
        sqlite3.SQLITE_BUSY | (1 << 8),
        sqlite3.SQLITE_LOCKED | (2 << 8),
    ],
)
def test_wal_negotiation_retries_extended_busy_and_locked_codes(
    extended_code: int,
) -> None:
    class WalCursor:
        def __init__(self, value: object) -> None:
            self.value = value

        def fetchone(self) -> tuple[object]:
            return (self.value,)

    class BusyThenWal:
        def __init__(self) -> None:
            self.attempts = 0
            self.configured_timeouts: list[int] = []

        def execute(self, statement: str) -> WalCursor:
            if statement == "PRAGMA busy_timeout":
                return WalCursor(5_000)
            if statement.startswith("PRAGMA busy_timeout = "):
                self.configured_timeouts.append(int(statement.rsplit(" ", 1)[1]))
                return WalCursor(None)
            assert statement == "PRAGMA journal_mode = WAL"
            self.attempts += 1
            if self.attempts == 1:
                error = sqlite3.OperationalError("database lock")
                error.sqlite_errorcode = extended_code
                raise error
            return WalCursor("wal")

    connection = BusyThenWal()

    review_module._enable_wal_with_deadline(
        cast(sqlite3.Connection, connection), busy_timeout_ms=100
    )

    assert connection.attempts == 2
    assert 1 <= connection.configured_timeouts[0] <= 100
    assert connection.configured_timeouts[-1] == 5_000


def test_answer_approval_requires_independent_second_reviewer(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store)
    _search_approve(review_store)

    with pytest.raises(ReviewValidationError, match="independent reviewer"):
        review_store.approve_answer(
            CASE_1,
            reviewer_id="reviewer-a",
            reviewed_content_sha256=CONTENT_A,
            reason="answer_checked",
            content_verified=True,
            basis_verified=True,
            privacy_verified=True,
        )

    review_store.approve_answer(
        CASE_1,
        reviewer_id="reviewer-b",
        reviewed_content_sha256=CONTENT_A,
        reason="answer_checked",
        content_verified=True,
        basis_verified=True,
        privacy_verified=True,
    )
    reviewed = review_store.get(CASE_1)
    assert reviewed.review_status == "approved"
    assert reviewed.search_eligible is True
    assert reviewed.answer_eligible is True


def test_account_rename_cannot_make_same_os_uid_an_independent_reviewer(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store)
    _search_approve(review_store, reviewer_id="uid:501:old-name")

    with pytest.raises(ReviewValidationError, match="independent reviewer"):
        review_store.approve_answer(
            CASE_1,
            reviewer_id="uid:501:new-name",
            reviewed_content_sha256=CONTENT_A,
            reason="answer_checked",
            content_verified=True,
            basis_verified=True,
            privacy_verified=True,
        )


@pytest.mark.parametrize(
    ("content_verified", "basis_verified", "privacy_verified"),
    [(False, True, True), (True, False, True), (True, True, False)],
)
def test_answer_approval_requires_content_basis_and_privacy_verification(
    review_store: ReviewStore,
    content_verified: bool,
    basis_verified: bool,
    privacy_verified: bool,
) -> None:
    _enqueue(review_store)
    _search_approve(review_store)

    with pytest.raises(ReviewValidationError, match="three verification flags"):
        review_store.approve_answer(
            CASE_1,
            reviewer_id="reviewer-b",
            reviewed_content_sha256=CONTENT_A,
            reason="answer_checked",
            content_verified=content_verified,
            basis_verified=basis_verified,
            privacy_verified=privacy_verified,
        )
    assert review_store.get(CASE_1).review_status == "search_approved"


def test_critical_fields_mode_verifies_and_search_approves_atomically(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store)
    review_store.run_mode(
        "critical-fields-all",
        cases=[ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A)],
        reviewer_id="reviewer-a",
        reason="critical_batch",
    )

    reviewed = review_store.get(CASE_1)
    assert reviewed.critical_field_review == "verified"
    assert reviewed.review_status == "search_approved"
    assert reviewed.search_eligible is True
    assert reviewed.answer_eligible is False
    assert [event.action for event in review_store.events(CASE_1)] == [
        "enqueue",
        "verify_fields",
        "approve_search",
    ]


def test_critical_fields_mode_rolls_back_verification_when_approval_event_fails(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store)
    with sqlite3.connect(review_store.path) as connection:
        connection.execute(
            """
            CREATE TRIGGER test_reject_search_event
            BEFORE INSERT ON review_events
            WHEN NEW.action = 'approve_search'
            BEGIN
                SELECT RAISE(ABORT, 'test failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        review_store.run_mode(
            "critical-fields-all",
            cases=[ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A)],
            reviewer_id="reviewer-a",
            reason="critical_batch",
        )

    reviewed = review_store.get(CASE_1)
    assert reviewed.critical_field_review == "pending"
    assert reviewed.review_status == "needs_review"
    assert [event.action for event in review_store.events(CASE_1)] == ["enqueue"]


def test_answer_run_mode_requires_explicit_content_basis_and_privacy_checks(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store)
    _search_approve(review_store)
    references = [ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A)]

    with pytest.raises(ReviewValidationError, match="three verification flags"):
        review_store.run_mode(
            "answer-and-basis-all",
            cases=references,
            reviewer_id="reviewer-b",
            reason="answer_batch",
        )

    review_store.run_mode(
        "answer-and-basis-all",
        cases=references,
        reviewer_id="reviewer-b",
        reason="answer_batch",
        content_verified=True,
        basis_verified=True,
        privacy_verified=True,
    )
    assert review_store.get(CASE_1).review_status == "approved"


@pytest.mark.parametrize(
    "operation",
    ["approve_search", "approve_answer", "verify_wrong_state", "enqueue_again"],
)
def test_forbidden_or_noop_transitions_are_rejected_without_new_event(
    review_store: ReviewStore,
    operation: str,
) -> None:
    _enqueue(review_store)
    before = len(review_store.events(CASE_1))

    with pytest.raises((ReviewConflictError, ReviewValidationError)):
        if operation == "approve_search":
            review_store.approve_search(
                CASE_1,
                reviewer_id="reviewer-a",
                reviewed_content_sha256=CONTENT_A,
                reason="skip_fields",
            )
        elif operation == "approve_answer":
            review_store.approve_answer(
                CASE_1,
                reviewer_id="reviewer-b",
                reviewed_content_sha256=CONTENT_A,
                reason="skip_search",
                content_verified=True,
                basis_verified=True,
                privacy_verified=True,
            )
        elif operation == "verify_wrong_state":
            _search_approve(review_store)
            before = len(review_store.events(CASE_1))
            review_store.verify_critical_fields(
                CASE_1,
                reviewer_id="reviewer-a",
                reviewed_content_sha256=CONTENT_A,
                reason="repeat",
            )
        else:
            review_store.enqueue(
                CASE_1,
                content_sha256=CONTENT_A,
                reason="repeat",
                actor_id="quality-gate",
            )

    assert len(review_store.events(CASE_1)) == before


def test_rejected_is_terminal_and_approved_cannot_be_rejected(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store, CASE_3, CONTENT_A)
    review_store.reject(
        CASE_3,
        reviewer_id="reviewer-a",
        reviewed_content_sha256=CONTENT_A,
        reason="bad_layout",
    )
    with pytest.raises(ReviewConflictError, match="terminal"):
        review_store.verify_critical_fields(
            CASE_3,
            reviewer_id="reviewer-a",
            reviewed_content_sha256=CONTENT_A,
            reason="retry",
        )

    _enqueue(review_store, CASE_4, CONTENT_B)
    _search_approve(review_store, CASE_4, CONTENT_B)
    review_store.approve_answer(
        CASE_4,
        reviewer_id="reviewer-b",
        reviewed_content_sha256=CONTENT_B,
        reason="answer_checked",
        content_verified=True,
        basis_verified=True,
        privacy_verified=True,
    )
    with pytest.raises(ReviewConflictError, match="terminal"):
        review_store.reject(
            CASE_4,
            reviewer_id="reviewer-c",
            reviewed_content_sha256=CONTENT_B,
            reason="late_reject",
        )


def test_content_hash_drift_and_stale_expected_state_fail_closed(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store)
    with pytest.raises(ReviewConflictError, match="content hash drift"):
        review_store.verify_critical_fields(
            CASE_1,
            reviewer_id="reviewer-a",
            reviewed_content_sha256=CONTENT_B,
            reason="fields_checked",
        )
    with pytest.raises(ReviewConflictError, match="stale expected state"):
        review_store.verify_critical_fields(
            CASE_1,
            reviewer_id="reviewer-a",
            reviewed_content_sha256=CONTENT_A,
            reason="fields_checked",
            expected_state="machine_extracted",
        )
    assert [event.action for event in review_store.events(CASE_1)] == ["enqueue"]


def test_duplicate_event_id_is_rejected_and_second_case_rolls_back(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store, CASE_1, CONTENT_A)
    _enqueue(review_store, CASE_2, CONTENT_B)
    review_store.reject(
        CASE_1,
        reviewer_id="reviewer-a",
        reviewed_content_sha256=CONTENT_A,
        reason="bad_layout",
        event_id="event-fixed",
    )
    with pytest.raises(ReviewConflictError, match="duplicate event"):
        review_store.reject(
            CASE_2,
            reviewer_id="reviewer-a",
            reviewed_content_sha256=CONTENT_B,
            reason="bad_layout",
            event_id="event-fixed",
        )
    assert review_store.get(CASE_2).review_status == "needs_review"
    assert [event.action for event in review_store.events(CASE_2)] == ["enqueue"]


def test_audit_rows_cannot_be_updated_or_deleted(review_store: ReviewStore) -> None:
    _enqueue(review_store)
    database = review_store.path
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE review_events SET reason = ? WHERE case_id = ?",
                ("changed", CASE_1),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM review_events WHERE case_id = ?", (CASE_1,))


def test_raw_sql_cannot_bypass_review_state_or_insert_spoofed_audit(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store)
    with sqlite3.connect(review_store.path) as connection:
        with pytest.raises(sqlite3.OperationalError, match="review_write_authorized"):
            connection.execute(
                """
                UPDATE review_cases
                SET critical_field_review='verified', critical_reviewer_id='spoofed'
                WHERE case_id=?
                """,
                (CASE_1,),
            )
        with pytest.raises(sqlite3.OperationalError, match="review_write_authorized"):
            connection.execute(
                """
                INSERT INTO review_events(
                    event_id, case_id, action, before_state, after_state, actor_id,
                    occurred_at, reason, reviewed_content_sha256, batch_manifest_sha256
                ) VALUES (
                    'spoofed-event', ?, 'verify_fields', 'needs_review',
                    'needs_review', 'spoofed', '2026-08-08T00:00:00.000000Z',
                    'spoofed', ?, NULL
                )
                """,
                (CASE_1, CONTENT_A),
            )

    reviewed = review_store.get(CASE_1)
    assert reviewed.critical_field_review == "pending"
    assert [event.action for event in review_store.events(CASE_1)] == ["enqueue"]


def test_batch_manifest_is_deterministic_and_approval_emits_per_case_events(
    review_store: ReviewStore,
) -> None:
    for case_id, content_hash in ((CASE_2, CONTENT_B), (CASE_1, CONTENT_A)):
        _enqueue(review_store, case_id, content_hash)
        review_store.verify_critical_fields(
            case_id,
            reviewer_id="reviewer-a",
            reviewed_content_sha256=content_hash,
            reason="fields_checked",
        )
    manifest = SegmentManifest.create(
        segment_id="segment-2025-a",
        cases=[
            ReviewReference(case_id=CASE_2, content_sha256=CONTENT_B),
            ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A),
        ],
    )
    manifest_bytes = manifest.to_bytes()
    assert manifest_bytes == (
        b'{"cases":[{"case_id":"'
        + CASE_1.encode()
        + b'","content_sha256":"'
        + CONTENT_A.encode()
        + b'"},{"case_id":"'
        + CASE_2.encode()
        + b'","content_sha256":"'
        + CONTENT_B.encode()
        + b'"}],"schema_version":"sen-qa-review-segment/v1",'
        b'"segment_id":"segment-2025-a"}\n'
    )
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()

    count = review_store.approve_search_batch(
        manifest_bytes,
        manifest_sha256=manifest_hash,
        reviewer_id="reviewer-a",
        reason="segment_sample_checked",
    )

    assert count == 2
    for case_id in (CASE_1, CASE_2):
        assert review_store.get(case_id).review_status == "search_approved"
        event = review_store.events(case_id)[-1]
        assert event.action == "approve_search"
        assert event.batch_manifest_sha256 == manifest_hash


def test_batch_hash_or_case_mismatch_is_all_or_nothing(
    review_store: ReviewStore,
) -> None:
    for case_id, content_hash in ((CASE_1, CONTENT_A), (CASE_2, CONTENT_B)):
        _enqueue(review_store, case_id, content_hash)
        review_store.verify_critical_fields(
            case_id,
            reviewer_id="reviewer-a",
            reviewed_content_sha256=content_hash,
            reason="fields_checked",
        )
    valid_manifest = SegmentManifest.create(
        segment_id="segment-a",
        cases=[
            ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A),
            ReviewReference(case_id=CASE_2, content_sha256=CONTENT_B),
        ],
    ).to_bytes()
    with pytest.raises(ReviewValidationError, match="manifest hash mismatch"):
        review_store.approve_search_batch(
            valid_manifest,
            manifest_sha256=CONTENT_C,
            reviewer_id="reviewer-a",
            reason="segment_checked",
        )

    drifted_manifest = SegmentManifest.create(
        segment_id="segment-a",
        cases=[
            ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A),
            ReviewReference(case_id=CASE_2, content_sha256=CONTENT_C),
        ],
    ).to_bytes()
    with pytest.raises(ReviewConflictError, match="content hash drift"):
        review_store.approve_search_batch(
            drifted_manifest,
            manifest_sha256=hashlib.sha256(drifted_manifest).hexdigest(),
            reviewer_id="reviewer-a",
            reason="segment_checked",
        )

    assert review_store.get(CASE_1).review_status == "needs_review"
    assert review_store.get(CASE_2).review_status == "needs_review"
    assert all(len(review_store.events(case_id)) == 2 for case_id in (CASE_1, CASE_2))


def test_batch_replay_is_rejected(review_store: ReviewStore) -> None:
    _enqueue(review_store)
    review_store.verify_critical_fields(
        CASE_1,
        reviewer_id="reviewer-a",
        reviewed_content_sha256=CONTENT_A,
        reason="fields_checked",
    )
    rendered = SegmentManifest.create(
        segment_id="segment-a",
        cases=[ReviewReference(case_id=CASE_1, content_sha256=CONTENT_A)],
    ).to_bytes()
    digest = hashlib.sha256(rendered).hexdigest()
    review_store.approve_search_batch(
        rendered,
        manifest_sha256=digest,
        reviewer_id="reviewer-a",
        reason="segment_checked",
    )
    with pytest.raises(ReviewConflictError, match="replay"):
        review_store.approve_search_batch(
            rendered,
            manifest_sha256=digest,
            reviewer_id="reviewer-a",
            reason="segment_checked",
        )
    assert len(review_store.events(CASE_1)) == 3


def test_assert_ready_returns_only_counts_and_blocker_codes(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store, CASE_5, CONTENT_A)
    _enqueue(review_store, CASE_6, CONTENT_B)
    _search_approve(review_store, CASE_6, CONTENT_B)

    report = review_store.assert_ready(purpose="answer")

    assert report.ready is False
    assert report.total == 2
    assert report.eligible == 0
    assert report.blockers == {"needs_review": 1, "search_approved": 1}
    assert "case-" not in repr(report)


def test_assert_ready_uses_one_consistent_database_snapshot(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store, CASE_1, CONTENT_A)
    _search_approve(review_store, CASE_1, CONTENT_A)
    review_store.approve_answer(
        CASE_1,
        reviewer_id="reviewer-b",
        reviewed_content_sha256=CONTENT_A,
        reason="answer_checked",
        content_verified=True,
        basis_verified=True,
        privacy_verified=True,
    )
    count_queries = 0

    def interleave_on_second_count(statement: str) -> None:
        nonlocal count_queries
        if statement.lstrip().startswith("SELECT COUNT"):
            count_queries += 1
            if count_queries == 2:
                with ReviewStore(review_store.path) as writer:
                    writer.enqueue(
                        CASE_5,
                        content_sha256=CONTENT_A,
                        reason="quality_gate",
                        actor_id="quality-gate",
                    )

    review_store._connection.set_trace_callback(interleave_on_second_count)
    try:
        report = review_store.assert_ready(purpose="answer")
    finally:
        review_store._connection.set_trace_callback(None)

    assert report.eligible + sum(report.blockers.values()) == report.total
    assert count_queries == 0


def test_nas_runbook_keeps_review_database_behind_service_boundary() -> None:
    runbook = (
        Path(__file__).parents[2] / "docs" / "runbooks" / "manual-review.md"
    ).read_text(encoding="utf-8")

    assert '"$SEN_QA_SOURCE_DIR" "$SEN_QA_RAW_DIR" "$SEN_QA_CANONICAL_DIR"' in runbook
    assert "SO_PEERCRED" in runbook
    assert "PRODUCTION RELEASE BLOCKED" in runbook
    assert "service account만 DB/WAL/SHM" in runbook
    assert (
        'install -d -o root -g "$SEN_QA_INGESTION_GROUP" -m 0710 \\\n  "$SEN_QA_ROOT"'
    ) in runbook
    assert "g:$SEN_QA_REVIEW_GROUP:--x,g:$SEN_QA_SERVICE_GROUP:--x" in runbook
    assert "g:$SEN_QA_SERVICE_GROUP:r-X" in runbook
    assert 'sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test -x "$SEN_QA_ROOT"' in runbook
    assert 'sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test ! -r "$SEN_QA_ROOT"' in runbook
    assert 'sudo -u "$SEN_QA_SERVICE_USER" test -x "$SEN_QA_ROOT"' in runbook
    assert 'sudo -u "$SEN_QA_SERVICE_USER" test ! -r "$SEN_QA_ROOT"' in runbook
    assert 'sudo -u "$SEN_QA_SERVICE_USER" test -r "$SEN_QA_SOURCE_DIR"' in runbook
    assert 'sudo -u "$SEN_QA_SERVICE_USER" test ! -w "$SEN_QA_CANONICAL_DIR"' in runbook
    assert '! -group "$SEN_QA_SERVICE_GROUP"' in runbook
    assert 'sudo -n "$SEN_QA_DOCKER" run' not in runbook


def test_review_database_schema_contains_no_candidate_text_columns(
    review_store: ReviewStore,
) -> None:
    _enqueue(review_store)
    review_store.close()
    with sqlite3.connect(review_store.path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(review_cases)").fetchall()
        }
    assert not columns & {
        "text",
        "title",
        "question",
        "answer",
        "basis",
        "facts",
        "source_text",
    }
