from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from src.corpus.ids import make_case_id
from src.evaluation.goldset import (
    BlindGoldLabel,
    BlindGoldQuestion,
    DevGoldItem,
    EvidenceRequirement,
    GoldsetError,
    ReleaseGoldItem,
    combine_release_goldsets,
    load_public_goldsets,
    load_release_goldsets,
    validate_public_goldsets,
)


def _case_id(year: int) -> str:
    return make_case_id(year, "계약", "계약", "general")


def _dev_item(index: int) -> DevGoldItem:
    year = 2020 + index % 6
    no_answer = index < 15
    case_id = _case_id(year)
    return DevGoldItem(
        item_id=f"eval-dev-{index + 1:03d}",
        question=f"개발 평가 질문 {index + 1}",
        accepted_case_ids=() if no_answer else (case_id,),
        required_evidence=()
        if no_answer
        else (
            EvidenceRequirement(
                case_id=case_id,
                pdf_page_index=13,
                source_span_index=0,
            ),
        ),
        edition_year=year,
        domain="계약",
        case_type="audit" if index % 11 == 0 else "qa",
        no_answer=no_answer,
        low_resolution_ocr=index < 30,
        focus="law" if index < 30 else "general",
        spacing_or_typo_variant=index < 20,
        cross_year_relation=index < 20,
        author_id="reviewer-author-a",
        reviewer_id="reviewer-approver-b",
    )


def _blind_question(index: int) -> BlindGoldQuestion:
    global_index = 140 + index
    return BlindGoldQuestion(
        item_id=f"eval-blind-{index + 1:03d}",
        question=f"블공개 평가 질문 {index + 1}",
        edition_year=2020 + global_index % 6,
        domain="계약",
        case_type="audit" if index % 13 == 0 else "qa",
        low_resolution_ocr=False,
        focus="general",
        spacing_or_typo_variant=False,
        cross_year_relation=False,
    )


def _blind_label(index: int) -> BlindGoldLabel:
    year = 2020 + (140 + index) % 6
    no_answer = index < 15
    case_id = _case_id(year)
    return BlindGoldLabel(
        item_id=f"eval-blind-{index + 1:03d}",
        accepted_case_ids=() if no_answer else (case_id,),
        required_evidence=()
        if no_answer
        else (
            EvidenceRequirement(
                case_id=case_id,
                pdf_page_index=13,
                source_span_index=0,
            ),
        ),
        no_answer=no_answer,
        author_id="reviewer-author-c",
        reviewer_id="reviewer-approver-d",
    )


def test_public_goldset_has_required_size_and_nonsecret_strata() -> None:
    dev = tuple(_dev_item(index) for index in range(140))
    blind = tuple(_blind_question(index) for index in range(60))

    summary = validate_public_goldsets(dev, blind)

    assert (summary.dev_items, summary.blind_items, summary.total_items) == (
        140,
        60,
        200,
    )
    assert dict(summary.items_by_year) == {
        2020: 34,
        2021: 34,
        2022: 33,
        2023: 33,
        2024: 33,
        2025: 33,
    }
    assert summary.focused_items == 30
    assert summary.low_resolution_ocr_items == 30
    assert summary.spacing_or_typo_items == 20
    assert summary.cross_year_relation_items == 20


def test_blind_public_record_forbids_answers_and_case_labels() -> None:
    payload = _blind_question(0).model_dump(mode="python")
    payload["accepted_case_ids"] = ("PRIVATE_CASE_LABEL",)

    with pytest.raises(ValidationError) as captured:
        BlindGoldQuestion.model_validate(payload)

    rendered = f"{captured.value!s} {captured.value!r}"
    assert "PRIVATE_CASE_LABEL" not in rendered
    assert not hasattr(_blind_question(0), "no_answer")
    assert not hasattr(_blind_question(0), "accepted_case_ids")


def test_author_and_reviewer_must_be_independent() -> None:
    payload = _dev_item(20).model_dump(mode="python")
    payload["reviewer_id"] = payload["author_id"]

    with pytest.raises(ValidationError, match="independent") as captured:
        DevGoldItem.model_validate(payload)

    assert captured.value.__cause__ is None

    release_payload = {
        **_dev_item(20).model_dump(mode="python"),
        "reviewer_id": "reviewer-author-a",
    }
    with pytest.raises(ValidationError, match="independent"):
        ReleaseGoldItem.model_validate(release_payload)


def test_release_join_requires_private_labels_and_canonical_evidence() -> None:
    dev = tuple(_dev_item(index) for index in range(140))
    blind = tuple(_blind_question(index) for index in range(60))
    labels = tuple(_blind_label(index) for index in range(60))
    canonical_evidence = {
        _case_id(year): frozenset(((13, 0),)) for year in range(2020, 2026)
    }

    release = combine_release_goldsets(
        dev,
        blind,
        labels,
        canonical_evidence=canonical_evidence,
    )

    assert len(release) == 200
    assert sum(item.no_answer for item in release) == 30
    assert all(item.author_id != item.reviewer_id for item in release)


def test_release_join_rejects_wrong_or_value_bearing_private_labels() -> None:
    dev = tuple(_dev_item(index) for index in range(140))
    blind = tuple(_blind_question(index) for index in range(60))
    labels = [_blind_label(index) for index in range(60)]
    labels[0] = BlindGoldLabel.model_construct(
        **{
            **labels[0].__dict__,
            "item_id": "PRIVATE_BLIND_SENTINEL",
        }
    )

    with pytest.raises(GoldsetError, match="goldset_invalid") as captured:
        combine_release_goldsets(
            dev,
            blind,
            tuple(labels),
            canonical_evidence={
                _case_id(year): frozenset(((13, 0),)) for year in range(2020, 2026)
            },
        )

    rendered = " ".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.__cause__),
            repr(captured.value.__context__),
        )
    )
    assert "PRIVATE" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def _write_jsonl(path: Path, records: tuple[BaseModel, ...]) -> None:
    path.write_text(
        "".join(
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


def test_public_and_private_jsonl_loaders_preserve_the_split_contract(
    tmp_path: Path,
) -> None:
    dev_path = tmp_path / "retrieval-dev.jsonl"
    blind_path = tmp_path / "retrieval-blind.jsonl"
    labels_path = tmp_path / "retrieval-blind-labels.jsonl"
    dev = tuple(_dev_item(index) for index in range(140))
    blind = tuple(_blind_question(index) for index in range(60))
    labels = tuple(_blind_label(index) for index in range(60))
    _write_jsonl(dev_path, dev)
    _write_jsonl(blind_path, blind)
    _write_jsonl(labels_path, labels)
    labels_path.chmod(0o600)

    loaded_dev, loaded_blind = load_public_goldsets(dev_path, blind_path)
    release = load_release_goldsets(
        dev_path,
        blind_path,
        labels_path,
        canonical_evidence={
            _case_id(year): frozenset(((13, 0),)) for year in range(2020, 2026)
        },
    )

    assert loaded_dev == dev
    assert loaded_blind == blind
    assert len(release) == 200


def test_goldset_loaders_reject_leaked_blind_fields_symlinks_and_open_labels(
    tmp_path: Path,
) -> None:
    dev_path = tmp_path / "retrieval-dev.jsonl"
    blind_path = tmp_path / "retrieval-blind.jsonl"
    labels_path = tmp_path / "retrieval-blind-labels.jsonl"
    dev = tuple(_dev_item(index) for index in range(140))
    blind = tuple(_blind_question(index) for index in range(60))
    labels = tuple(_blind_label(index) for index in range(60))
    _write_jsonl(dev_path, dev)
    _write_jsonl(blind_path, blind)
    _write_jsonl(labels_path, labels)
    labels_path.chmod(0o600)

    leaked = blind[0].model_dump(mode="json")
    leaked["no_answer"] = True
    blind_path.write_text(
        json.dumps(leaked, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(GoldsetError, match="goldset_invalid"):
        load_public_goldsets(dev_path, blind_path)

    _write_jsonl(blind_path, blind)
    symlink_path = tmp_path / "retrieval-blind-link.jsonl"
    symlink_path.symlink_to(blind_path)
    with pytest.raises(GoldsetError, match="goldset_invalid"):
        load_public_goldsets(dev_path, symlink_path)

    labels_path.chmod(0o640)
    with pytest.raises(GoldsetError, match="goldset_invalid") as captured:
        load_release_goldsets(
            dev_path,
            blind_path,
            labels_path,
            canonical_evidence={
                _case_id(year): frozenset(((13, 0),)) for year in range(2020, 2026)
            },
        )

    assert os.stat(labels_path).st_mode & 0o077
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
