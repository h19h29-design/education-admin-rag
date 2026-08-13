from __future__ import annotations

import hashlib
import importlib
import json
import os
import stat
from pathlib import Path
from typing import Any, cast

import pytest

from src.ingestion.manifest import NativeReviewLayoutSegment
from src.ingestion.parse_common import LayoutSegmentProvenance, _layout_registry_sha256
from src.ingestion.review import (
    CanonicalReviewRegistry,
    ReviewReference,
    ReviewSourceLocation,
)

RELEASE_ID = "corpus-20260810042914-2f1ca61e"
PARSER_SHA = "1" * 64
RAW_SHA = "2" * 64
MANIFEST_SHA = "5" * 64
SOURCE_SHA = "3" * 64
RENDER_SHA = "4" * 64


def _reference(number: int, *, content: str | None = None) -> ReviewReference:
    return ReviewReference(
        case_id=f"senqa-2025-contract-contract-general-{number}",
        content_sha256=content or f"{number:x}" * 64,
        source_locations=(
            ReviewSourceLocation(
                page_id=number,
                bbox=(10.0, 20.0, 100.0, 200.0),
                reason_code="critical-fields-unverified",
            ),
        ),
    )


def _registry(references: tuple[ReviewReference, ...]):
    registry = CanonicalReviewRegistry.create(cases=references)
    raw = registry.to_bytes()
    return CanonicalReviewRegistry.from_bytes(
        raw, expected_sha256=hashlib.sha256(raw).hexdigest()
    )


def _ocr_provenance(page: int) -> LayoutSegmentProvenance:
    registry_sha = _layout_registry_sha256(
        detector_version="vision-layout-v1",
        doc_id="sen-qa-2025",
        edition_year=2025,
        sampling_status="sampling_required",
        segment_start_pdf_page=1,
        segment_end_pdf_page=20,
        source_sha256=SOURCE_SHA,
    )
    return LayoutSegmentProvenance(
        segment_id="layout-segment-" + "a" * 32,
        segment_key="approved-document-body",
        segment_start_pdf_page=1,
        segment_end_pdf_page=20,
        registry_policy_version="layout-segment-registry-v1",
        registry_sha256=registry_sha,
        detector_version="vision-layout-v1",
        region_count=1,
        sampling_status="sampling_required",
        doc_id="sen-qa-2025",
        edition_year=2025,
        source_sha256=SOURCE_SHA,
        pdf_page_index=page,
        render_sha256=RENDER_SHA,
    )


def _one_case_sampling_authority():
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        build_sampling_authority,
    )

    reference = _reference(1)
    return build_sampling_authority(
        release_id=RELEASE_ID,
        registry=_registry((reference,)),
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=(
            SamplingCandidate(
                reference=reference,
                edition_year=2025,
                extraction_source="ocr",
                pii_class="none",
                review_status="needs_review",
                layout_segment_provenances=(_ocr_provenance(1),),
            ),
        ),
    )


def test_review_sampling_contract_is_exposed_by_a_dedicated_module() -> None:
    """Catches the sampling workflow disappearing behind generic batch approval."""
    module = importlib.import_module("src.ingestion.review_sampling")

    assert callable(module.build_sampling_authority)


def test_ocr_sample_plan_is_release_authority_bound_and_deterministic() -> None:
    """Catches input order or unbound randomness changing the authoritative sample."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        build_sampling_authority,
    )

    references = tuple(_reference(number) for number in range(1, 12))
    registry = _registry(references)
    candidates = tuple(
        SamplingCandidate(
            reference=reference,
            edition_year=2025,
            extraction_source="ocr",
            pii_class="none",
            review_status="needs_review",
            layout_segment_provenances=(_ocr_provenance(number),),
        )
        for number, reference in enumerate(references, start=1)
    )

    first = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=registry,
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=candidates,
    )
    second = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=registry,
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=tuple(reversed(candidates)),
    )

    assert first.to_bytes() == second.to_bytes()
    assert first.inventory.release_id == RELEASE_ID
    assert first.inventory.registry_sha256 == registry.fingerprint_sha256
    assert first.inventory.parser_authority_sha256 == PARSER_SHA
    assert first.inventory.raw_authority_sha256 == RAW_SHA
    assert first.inventory.manifest_sha256 == MANIFEST_SHA
    assert len(first.plans) == 1
    assert first.plans[0].sample_size == 5
    assert tuple(item.case_id for item in first.plans[0].selected) == (
        "senqa-2025-contract-contract-general-6",
        "senqa-2025-contract-contract-general-10",
        "senqa-2025-contract-contract-general-8",
        "senqa-2025-contract-contract-general-11",
        "senqa-2025-contract-contract-general-2",
    )
    payload = json.loads(first.to_bytes())
    assert payload["inventory"]["segments"][0]["layout_provenance"][0] == {
        "pdf_page_index": 1,
        "region_count": 1,
        "render_sha256": RENDER_SHA,
    }


def test_native_without_parser_segment_provenance_fails_closed_at_model_seam() -> None:
    """Catches document/year grouping being invented when native segments are absent."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        build_sampling_authority,
    )

    reference = _reference(1)
    registry = _registry((reference,))
    authority = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=registry,
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=(
            SamplingCandidate(
                reference=reference,
                edition_year=2022,
                extraction_source="native",
                pii_class="none",
                review_status="needs_review",
                layout_segment_provenances=(),
            ),
        ),
    )

    assert authority.plans == ()
    assert authority.inventory.blockers == (
        "native_layout_segment_provenance_missing_at_ParsedCaseCandidate.layout_segment_provenances",
    )


def test_manifest_native_segment_is_sampled_without_synthesizing_a_group() -> None:
    """Catches native sampling ignoring the explicit source-manifest stratum."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        build_sampling_authority,
    )

    references = tuple(_reference(number) for number in range(1, 7))
    registry = _registry(references)
    segment = NativeReviewLayoutSegment(
        segment_id="native-layout-2022-body-v1",
        start_pdf_page=7,
        end_pdf_page=384,
        sampling_policy="native-layout-sample",
        policy_version="native-review-layout-segment-v1",
    )

    authority = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=registry,
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=tuple(
            SamplingCandidate(
                reference=reference,
                edition_year=2022,
                extraction_source="native",
                pii_class="none",
                review_status="needs_review",
                layout_segment_provenances=(),
                native_layout_segment=segment,
                doc_id="sen-qa-2022",
                source_sha256=SOURCE_SHA,
            )
            for reference in references
        ),
    )

    assert authority.inventory.blockers == ()
    assert authority.plans[0].segment_id == "native-layout-2022-body-v1"
    assert authority.plans[0].sample_size == 5
    assert authority.inventory.segments[0].layout.to_payload() == {
        "doc_id": "sen-qa-2022",
        "edition_year": 2022,
        "manifest_sha256": MANIFEST_SHA,
        "policy_version": "native-review-layout-segment-v1",
        "sampling_policy": "native-layout-sample",
        "segment_end_pdf_page": 384,
        "segment_id": "native-layout-2022-body-v1",
        "segment_start_pdf_page": 7,
        "source_sha256": SOURCE_SHA,
    }


def test_all_fields_years_and_non_approvable_cases_never_enter_sample_plan() -> None:
    """Catches 2023/24 or unsafe terminal cases leaking into batch approval."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        build_sampling_authority,
    )

    references = tuple(_reference(number) for number in range(1, 8))
    registry = _registry(references)
    candidates = (
        SamplingCandidate(
            reference=references[0],
            edition_year=2023,
            extraction_source="ocr",
            pii_class="none",
            review_status="needs_review",
            layout_segment_provenances=(),
        ),
        SamplingCandidate(
            reference=references[1],
            edition_year=2024,
            extraction_source="ocr",
            pii_class="none",
            review_status="needs_review",
            layout_segment_provenances=(),
        ),
        *tuple(
            SamplingCandidate(
                reference=reference,
                edition_year=2025,
                extraction_source="ocr",
                pii_class=(
                    "restricted"
                    if number == 3
                    else "public_credit"
                    if number == 4
                    else "none"
                ),
                review_status="rejected" if number == 5 else "needs_review",
                layout_segment_provenances=(_ocr_provenance(number),),
            )
            for number, reference in enumerate(references[2:], start=3)
        ),
    )

    authority = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=registry,
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=candidates,
    )

    assert authority.inventory.all_fields_case_ids == (
        references[0].case_id,
        references[1].case_id,
    )
    assert tuple(item.case_id for item in authority.plans[0].members) == (
        references[5].case_id,
        references[6].case_id,
    )


def test_completed_sample_events_authorize_search_only_manifest(tmp_path: Path) -> None:
    """Catches generic full approval or incomplete samples replacing sample evidence."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        SamplingReviewLedger,
        build_sampling_authority,
        load_sampling_authority,
        write_sampling_authority,
    )

    references = tuple(_reference(number) for number in range(1, 7))
    registry = _registry(references)
    authority = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=registry,
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=tuple(
            SamplingCandidate(
                reference=reference,
                edition_year=2025,
                extraction_source="ocr",
                pii_class="none",
                review_status="needs_review",
                layout_segment_provenances=(_ocr_provenance(number),),
            )
            for number, reference in enumerate(references, start=1)
        ),
    )
    plan = authority.plans[0]
    path = tmp_path / "sampling-authority.json"
    write_sampling_authority(path, authority)
    verified = load_sampling_authority(path, expected_sha256=authority.sha256)
    ledger = SamplingReviewLedger(authority=verified, plan_sha256=plan.sha256)
    with pytest.raises(ValueError, match="sample_events_incomplete"):
        ledger.search_approval_manifest()
    for selected in plan.selected:
        ledger.record(
            case_id=selected.case_id,
            reviewed_content_sha256=selected.content_sha256,
            reviewer_id="uid:501:reviewer-a",
            outcome="passed",
            reason_code="segment_sample_checked",
        )

    manifest = ledger.search_approval_manifest()

    assert len(ledger.events) == 5
    assert len(manifest.members) == 6
    assert manifest.search_eligible is True
    assert manifest.answer_eligible is False
    assert manifest.sample_events_sha256 == ledger.events_sha256
    assert manifest.plan_sha256 == plan.sha256


def test_one_sample_error_escalates_entire_segment_and_blocks_batch(
    tmp_path: Path,
) -> None:
    """Catches a sampled error being averaged away while unsampled cases are approved."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        SamplingReviewLedger,
        build_sampling_authority,
        load_sampling_authority,
        write_sampling_authority,
    )

    references = tuple(_reference(number) for number in range(1, 7))
    authority = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=_registry(references),
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=tuple(
            SamplingCandidate(
                reference=reference,
                edition_year=2025,
                extraction_source="ocr",
                pii_class="none",
                review_status="needs_review",
                layout_segment_provenances=(_ocr_provenance(number),),
            )
            for number, reference in enumerate(references, start=1)
        ),
    )
    plan = authority.plans[0]
    path = tmp_path / "sampling-authority.json"
    write_sampling_authority(path, authority)
    verified = load_sampling_authority(path, expected_sha256=authority.sha256)
    ledger = SamplingReviewLedger(authority=verified, plan_sha256=plan.sha256)
    selected = plan.selected[0]
    ledger.record(
        case_id=selected.case_id,
        reviewed_content_sha256=selected.content_sha256,
        reviewer_id="uid:501:reviewer-a",
        outcome="error",
        reason_code="invalid_layout",
    )

    with pytest.raises(ValueError, match="segment_escalated"):
        ledger.search_approval_manifest()
    escalation = ledger.critical_fields_escalation()
    assert escalation.mode == "critical-fields-all"
    assert tuple(
        (item.case_id, item.content_sha256) for item in escalation.members
    ) == tuple((item.case_id, item.content_sha256) for item in plan.members)


def test_sampling_authority_writer_is_owner_only_nofollow_and_bounded(
    tmp_path: Path,
) -> None:
    """Catches sampling authority writes following links or becoming group-readable."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        build_sampling_authority,
        write_sampling_authority,
    )

    reference = _reference(1)
    authority = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=_registry((reference,)),
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=(
            SamplingCandidate(
                reference=reference,
                edition_year=2025,
                extraction_source="ocr",
                pii_class="none",
                review_status="needs_review",
                layout_segment_provenances=(_ocr_provenance(1),),
            ),
        ),
    )
    path = tmp_path / "sampling-authority.json"

    write_sampling_authority(path, authority)

    assert path.read_bytes() == authority.to_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    target = tmp_path / "target"
    target.write_bytes(b"untouched")
    link = tmp_path / "link"
    os.symlink(target, link)
    with pytest.raises(ValueError, match="sampling_write_invalid"):
        write_sampling_authority(link, authority)
    assert target.read_bytes() == b"untouched"


def test_sampling_authority_load_rejects_intermediate_symlink(tmp_path: Path) -> None:
    """Catches leaf-only O_NOFOLLOW loading through a linked parent directory."""
    from src.ingestion.review_sampling import (
        SamplingValidationError,
        load_sampling_authority,
        write_sampling_authority,
    )

    authority = _one_case_sampling_authority()
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    target = real_parent / "sampling-authority.json"
    write_sampling_authority(target, authority)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(SamplingValidationError) as captured:
        load_sampling_authority(
            linked_parent / target.name,
            expected_sha256=authority.sha256,
        )

    assert str(captured.value) == "sampling_authority_invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_sampling_authority_write_rejects_intermediate_symlink(
    tmp_path: Path,
) -> None:
    """Catches exclusive writes escaping through a linked parent directory."""
    from src.ingestion.review_sampling import (
        SamplingValidationError,
        write_sampling_authority,
    )

    authority = _one_case_sampling_authority()
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    target = real_parent / "sampling-authority.json"

    with pytest.raises(SamplingValidationError) as captured:
        write_sampling_authority(linked_parent / target.name, authority)

    assert not target.exists()
    assert str(captured.value) == "sampling_write_invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def _swap_ancestor_when_leaf_opens(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ancestor: Path,
    displaced: Path,
    alternate: Path,
    leaf_name: str,
) -> None:
    real_open = os.open
    swapped = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == leaf_name and dir_fd is not None and not swapped:
            ancestor.rename(displaced)
            ancestor.symlink_to(alternate, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)


def test_sampling_authority_load_rejects_deterministic_ancestor_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an ancestor replacement between parent acquisition and leaf read."""
    from src.ingestion.review_sampling import (
        SamplingValidationError,
        load_sampling_authority,
        write_sampling_authority,
    )

    authority = _one_case_sampling_authority()
    ancestor = tmp_path / "stable-parent"
    ancestor.mkdir()
    target = ancestor / "sampling-authority.json"
    write_sampling_authority(target, authority)
    displaced = tmp_path / "displaced-parent"
    alternate = tmp_path / "alternate-parent"
    alternate.mkdir()
    _swap_ancestor_when_leaf_opens(
        monkeypatch,
        ancestor=ancestor,
        displaced=displaced,
        alternate=alternate,
        leaf_name=target.name,
    )

    with pytest.raises(SamplingValidationError) as captured:
        load_sampling_authority(target, expected_sha256=authority.sha256)

    assert str(captured.value) == "sampling_authority_invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_sampling_authority_write_rejects_deterministic_ancestor_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an ancestor replacement between parent acquisition and leaf creation."""
    from src.ingestion.review_sampling import (
        SamplingValidationError,
        write_sampling_authority,
    )

    authority = _one_case_sampling_authority()
    ancestor = tmp_path / "stable-parent"
    ancestor.mkdir()
    target = ancestor / "sampling-authority.json"
    displaced = tmp_path / "displaced-parent"
    alternate = tmp_path / "alternate-parent"
    alternate.mkdir()
    _swap_ancestor_when_leaf_opens(
        monkeypatch,
        ancestor=ancestor,
        displaced=displaced,
        alternate=alternate,
        leaf_name=target.name,
    )

    with pytest.raises(SamplingValidationError) as captured:
        write_sampling_authority(target, authority)

    assert not (displaced / target.name).exists()
    assert not (alternate / target.name).exists()
    assert str(captured.value) == "sampling_write_invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_sampling_public_boundary_rejects_wrong_types_without_value_context() -> None:
    """Catches malformed caller values escaping the fixed sampling error code."""
    from src.ingestion.review_sampling import (
        SamplingValidationError,
        build_sampling_authority,
    )

    sentinel = "PRIVATE-SAMPLING-SENTINEL"
    reference = _reference(1)
    with pytest.raises(SamplingValidationError) as captured:
        build_sampling_authority(
            release_id=cast(Any, {sentinel: sentinel}),
            registry=_registry((reference,)),
            parser_authority_sha256=PARSER_SHA,
            raw_authority_sha256=RAW_SHA,
            manifest_sha256=MANIFEST_SHA,
            candidates=cast(Any, ()),
        )

    assert str(captured.value) == "sampling_input_invalid"
    assert sentinel not in str(captured.value) + repr(captured.value)


def test_ledger_rejects_caller_constructed_unverified_authority() -> None:
    """Catches a caller-minted plan bypassing the externally pinned authority file."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        SamplingReviewLedger,
        SamplingValidationError,
        build_sampling_authority,
    )

    reference = _reference(1)
    authority = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=_registry((reference,)),
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=(
            SamplingCandidate(
                reference=reference,
                edition_year=2025,
                extraction_source="ocr",
                pii_class="none",
                review_status="needs_review",
                layout_segment_provenances=(_ocr_provenance(1),),
            ),
        ),
    )

    with pytest.raises(SamplingValidationError, match="sampling_input_invalid"):
        SamplingReviewLedger(
            authority=cast(Any, authority),
            plan_sha256=authority.plans[0].sha256,
        )


def test_ledger_recursively_rejects_mutated_verified_authority(
    tmp_path: Path,
) -> None:
    """Catches nested authority mutation after an externally pinned load."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        SamplingReviewLedger,
        SamplingValidationError,
        build_sampling_authority,
        load_sampling_authority,
        write_sampling_authority,
    )

    reference = _reference(1)
    authority = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=_registry((reference,)),
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=(
            SamplingCandidate(
                reference=reference,
                edition_year=2025,
                extraction_source="ocr",
                pii_class="none",
                review_status="needs_review",
                layout_segment_provenances=(_ocr_provenance(1),),
            ),
        ),
    )
    path = tmp_path / "sampling-authority.json"
    write_sampling_authority(path, authority)
    verified = load_sampling_authority(path, expected_sha256=authority.sha256)
    object.__setattr__(
        verified.inventory, "release_id", "corpus-20260810042915-deadbeef"
    )

    with pytest.raises(SamplingValidationError) as captured:
        SamplingReviewLedger(
            authority=verified,
            plan_sha256=authority.plans[0].sha256,
        )

    assert str(captured.value) == "sampling_input_invalid"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_verified_authority_loader_rejects_recursive_plan_drift(
    tmp_path: Path,
) -> None:
    """Catches canonical bytes whose plan selection no longer matches the inventory."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        SamplingValidationError,
        build_sampling_authority,
        load_sampling_authority,
    )

    references = tuple(_reference(number) for number in range(1, 7))
    authority = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=_registry(references),
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=tuple(
            SamplingCandidate(
                reference=reference,
                edition_year=2025,
                extraction_source="ocr",
                pii_class="none",
                review_status="needs_review",
                layout_segment_provenances=(_ocr_provenance(number),),
            )
            for number, reference in enumerate(references, start=1)
        ),
    )
    payload = json.loads(authority.to_bytes())
    payload["plans"][0]["selected"] = payload["plans"][0]["members"][:5]
    payload["plans"][0]["plan_sha256"] = "0" * 64
    raw = (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    path = tmp_path / "forged-sampling-authority.json"
    path.write_bytes(raw)
    os.chmod(path, 0o600)

    with pytest.raises(SamplingValidationError, match="sampling_authority_invalid"):
        load_sampling_authority(path, expected_sha256=hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize("case", ["wrong-hash", "symlink", "type-bomb"])
def test_verified_authority_loader_is_nofollow_and_value_free(
    tmp_path: Path, case: str
) -> None:
    """Catches unpinned paths or malformed values escaping the fixed loader code."""
    from src.ingestion.review_sampling import (
        SamplingCandidate,
        SamplingValidationError,
        build_sampling_authority,
        load_sampling_authority,
        write_sampling_authority,
    )

    sentinel = "PRIVATE-SAMPLING-AUTHORITY-SENTINEL"
    reference = _reference(1)
    authority = build_sampling_authority(
        release_id=RELEASE_ID,
        registry=_registry((reference,)),
        parser_authority_sha256=PARSER_SHA,
        raw_authority_sha256=RAW_SHA,
        manifest_sha256=MANIFEST_SHA,
        candidates=(
            SamplingCandidate(
                reference=reference,
                edition_year=2025,
                extraction_source="ocr",
                pii_class="none",
                review_status="needs_review",
                layout_segment_provenances=(_ocr_provenance(1),),
            ),
        ),
    )
    target = tmp_path / f"{sentinel}.json"
    write_sampling_authority(target, authority)
    selected_path: object = target
    selected_sha: object = authority.sha256
    if case == "wrong-hash":
        selected_sha = "0" * 64
    elif case == "symlink":
        link = tmp_path / "authority-link"
        link.symlink_to(target)
        selected_path = link
    else:
        selected_path = {sentinel: sentinel}

    with pytest.raises(SamplingValidationError) as captured:
        load_sampling_authority(
            cast(Any, selected_path), expected_sha256=cast(Any, selected_sha)
        )

    diagnostics = str(captured.value) + repr(captured.value)
    assert str(captured.value) == "sampling_authority_invalid"
    assert sentinel not in diagnostics
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
