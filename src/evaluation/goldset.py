"""Strict public/private gold-set contracts with value-free release validation."""

from __future__ import annotations

import json
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.corpus.ids import validate_case_id

Focus = Literal["general", "law", "article", "amount", "date", "relation"]
GoldCaseType = Literal["qa", "audit"]

_MAX_ITEMS = 200
_MAX_EVIDENCE_PER_ITEM = 64
_REVIEWER_PATTERN = r"^reviewer-[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$"
_GoldModelT = TypeVar("_GoldModelT", bound=BaseModel)
_MAX_GOLDSET_BYTES = 32 * 1024 * 1024
_MAX_JSONL_LINE_BYTES = 256 * 1024
_DEFAULT_EVAL_ROOT = Path(__file__).resolve().parents[2] / "data" / "eval"
_DEFAULT_DEV_PATH = _DEFAULT_EVAL_ROOT / "retrieval-dev.jsonl"
_DEFAULT_BLIND_PATH = _DEFAULT_EVAL_ROOT / "retrieval-blind.jsonl"


class GoldsetError(ValueError):
    """A fixed, value-free gold-set contract failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _raise(code: str) -> NoReturn:
    raise GoldsetError(code) from None


class GoldModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        hide_input_in_errors=True,
    )


class EvidenceRequirement(GoldModel):
    case_id: str = Field(min_length=1, max_length=200)
    pdf_page_index: int = Field(ge=1, le=10_000)
    source_span_index: int = Field(ge=0, le=1_000_000)

    @model_validator(mode="after")
    def has_canonical_case_id(self) -> EvidenceRequirement:
        failed = False
        try:
            validate_case_id(self.case_id)
        except ValueError:
            failed = True
        if failed:
            raise ValueError("evidence case ID is invalid")
        return self


class _PublicStrata(GoldModel):
    question: str = Field(min_length=1, max_length=2_048)
    edition_year: int = Field(ge=2020, le=2025)
    domain: str = Field(min_length=1, max_length=120)
    case_type: GoldCaseType
    low_resolution_ocr: bool
    focus: Focus
    spacing_or_typo_variant: bool
    cross_year_relation: bool

    @model_validator(mode="after")
    def has_canonical_public_text(self) -> _PublicStrata:
        if (
            self.question != unicodedata.normalize("NFC", self.question)
            or self.domain != unicodedata.normalize("NFC", self.domain)
            or not self.question.strip()
            or not self.domain.strip()
        ):
            raise ValueError("public gold text is invalid")
        return self


class DevGoldItem(_PublicStrata):
    item_id: str = Field(pattern=r"^eval-dev-[0-9]{3}$")
    accepted_case_ids: tuple[str, ...] = Field(max_length=16)
    required_evidence: tuple[EvidenceRequirement, ...] = Field(
        max_length=_MAX_EVIDENCE_PER_ITEM
    )
    no_answer: bool
    author_id: str = Field(pattern=_REVIEWER_PATTERN)
    reviewer_id: str = Field(pattern=_REVIEWER_PATTERN)

    @model_validator(mode="after")
    def has_independent_consistent_labels(self) -> DevGoldItem:
        if self.author_id == self.reviewer_id:
            raise ValueError("gold author and reviewer must be independent")
        _validate_answer_contract(
            no_answer=self.no_answer,
            accepted_case_ids=self.accepted_case_ids,
            required_evidence=self.required_evidence,
        )
        return self


class BlindGoldQuestion(_PublicStrata):
    item_id: str = Field(pattern=r"^eval-blind-[0-9]{3}$")


class BlindGoldLabel(GoldModel):
    item_id: str = Field(pattern=r"^eval-blind-[0-9]{3}$")
    accepted_case_ids: tuple[str, ...] = Field(max_length=16)
    required_evidence: tuple[EvidenceRequirement, ...] = Field(
        max_length=_MAX_EVIDENCE_PER_ITEM
    )
    no_answer: bool
    author_id: str = Field(pattern=_REVIEWER_PATTERN)
    reviewer_id: str = Field(pattern=_REVIEWER_PATTERN)

    @model_validator(mode="after")
    def has_independent_consistent_labels(self) -> BlindGoldLabel:
        if self.author_id == self.reviewer_id:
            raise ValueError("gold author and reviewer must be independent")
        _validate_answer_contract(
            no_answer=self.no_answer,
            accepted_case_ids=self.accepted_case_ids,
            required_evidence=self.required_evidence,
        )
        return self


class ReleaseGoldItem(_PublicStrata):
    item_id: str = Field(pattern=r"^eval-(?:dev|blind)-[0-9]{3}$")
    accepted_case_ids: tuple[str, ...] = Field(max_length=16)
    required_evidence: tuple[EvidenceRequirement, ...] = Field(
        max_length=_MAX_EVIDENCE_PER_ITEM
    )
    no_answer: bool
    author_id: str = Field(pattern=_REVIEWER_PATTERN)
    reviewer_id: str = Field(pattern=_REVIEWER_PATTERN)

    @model_validator(mode="after")
    def has_independent_consistent_labels(self) -> ReleaseGoldItem:
        if self.author_id == self.reviewer_id:
            raise ValueError("gold author and reviewer must be independent")
        _validate_answer_contract(
            no_answer=self.no_answer,
            accepted_case_ids=self.accepted_case_ids,
            required_evidence=self.required_evidence,
        )
        return self


@dataclass(frozen=True, slots=True)
class GoldsetStrata:
    dev_items: int
    blind_items: int
    total_items: int
    items_by_year: tuple[tuple[int, int], ...]
    focused_items: int
    low_resolution_ocr_items: int
    spacing_or_typo_items: int
    cross_year_relation_items: int


def _validate_answer_contract(
    *,
    no_answer: bool,
    accepted_case_ids: tuple[str, ...],
    required_evidence: tuple[EvidenceRequirement, ...],
) -> None:
    failed = (
        len(set(accepted_case_ids)) != len(accepted_case_ids)
        or tuple(sorted(accepted_case_ids)) != accepted_case_ids
        or len(set(required_evidence)) != len(required_evidence)
        or tuple(
            sorted(
                required_evidence,
                key=lambda item: (
                    item.case_id,
                    item.pdf_page_index,
                    item.source_span_index,
                ),
            )
        )
        != required_evidence
        or (no_answer and bool(accepted_case_ids or required_evidence))
        or (not no_answer and (not accepted_case_ids or not required_evidence))
        or any(item.case_id not in accepted_case_ids for item in required_evidence)
    )
    for case_id in accepted_case_ids:
        try:
            validate_case_id(case_id)
        except ValueError:
            failed = True
    if failed:
        raise ValueError("gold answer contract is invalid")


def _model_fields(
    value: object, model_type: type[BaseModel]
) -> dict[str, object] | None:
    if type(value) is not model_type:
        return None
    try:
        fields = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        return None
    if type(fields) is not dict or set(fields) != set(model_type.model_fields):
        return None
    return dict(fields)


def _revalidate_evidence_tuple(value: object) -> tuple[dict[str, object], ...] | None:
    if type(value) is not tuple or len(value) > _MAX_EVIDENCE_PER_ITEM:
        return None
    checked: list[dict[str, object]] = []
    for item in cast(tuple[object, ...], value):
        fields = _model_fields(item, EvidenceRequirement)
        if fields is None:
            return None
        checked.append(fields)
    return tuple(checked)


def _revalidate_item(
    value: object, model_type: type[_GoldModelT]
) -> _GoldModelT | None:
    fields = _model_fields(value, model_type)
    if fields is None:
        return None
    if "required_evidence" in fields:
        evidence = _revalidate_evidence_tuple(fields["required_evidence"])
        if evidence is None:
            return None
        fields["required_evidence"] = evidence
    try:
        return model_type.model_validate(fields)
    except (ValidationError, TypeError, ValueError):
        return None


def validate_public_goldsets(dev: object, blind: object) -> GoldsetStrata:
    if (
        type(dev) is not tuple
        or len(dev) != 140
        or type(blind) is not tuple
        or len(blind) != 60
    ):
        _raise("goldset_invalid")
    checked_dev = tuple(
        _revalidate_item(item, DevGoldItem) for item in cast(tuple[object, ...], dev)
    )
    checked_blind = tuple(
        _revalidate_item(item, BlindGoldQuestion)
        for item in cast(tuple[object, ...], blind)
    )
    if any(item is None for item in (*checked_dev, *checked_blind)):
        _raise("goldset_invalid")
    dev_items = cast(tuple[DevGoldItem, ...], checked_dev)
    blind_items = cast(tuple[BlindGoldQuestion, ...], checked_blind)
    all_items: tuple[_PublicStrata, ...] = (*dev_items, *blind_items)
    item_ids = (
        *(item.item_id for item in dev_items),
        *(item.item_id for item in blind_items),
    )
    year_counts = tuple(
        (year, sum(item.edition_year == year for item in all_items))
        for year in range(2020, 2026)
    )
    focused = sum(
        item.focus in {"law", "article", "amount", "date"} for item in all_items
    )
    low_resolution = sum(item.low_resolution_ocr for item in all_items)
    typo = sum(item.spacing_or_typo_variant for item in all_items)
    related = sum(item.cross_year_relation for item in all_items)
    if (
        len(set(item_ids)) != _MAX_ITEMS
        or any(count < 25 for _, count in year_counts)
        or focused < 30
        or low_resolution < 30
        or typo < 20
        or related < 20
        or {item.case_type for item in all_items} != {"qa", "audit"}
    ):
        _raise("goldset_invalid")
    return GoldsetStrata(
        dev_items=140,
        blind_items=60,
        total_items=_MAX_ITEMS,
        items_by_year=year_counts,
        focused_items=focused,
        low_resolution_ocr_items=low_resolution,
        spacing_or_typo_items=typo,
        cross_year_relation_items=related,
    )


def _checked_canonical_evidence(
    value: object,
) -> dict[str, frozenset[tuple[int, int]]] | None:
    if type(value) is not dict or len(value) > 1_000_000:
        return None
    checked: dict[str, frozenset[tuple[int, int]]] = {}
    for case_id, references in cast(dict[object, object], value).items():
        if (
            type(case_id) is not str
            or type(references) is not frozenset
            or len(references) > 1_000_000
        ):
            return None
        try:
            validate_case_id(case_id)
        except ValueError:
            return None
        typed_references: set[tuple[int, int]] = set()
        for reference in cast(frozenset[object], references):
            if (
                type(reference) is not tuple
                or len(reference) != 2
                or type(reference[0]) is not int
                or type(reference[1]) is not int
                or reference[0] < 1
                or reference[0] > 10_000
                or reference[1] < 0
                or reference[1] > 1_000_000
            ):
                return None
            typed_references.add((reference[0], reference[1]))
        checked[case_id] = frozenset(typed_references)
    return checked


def combine_release_goldsets(
    dev: object,
    blind: object,
    blind_labels: object,
    *,
    canonical_evidence: object,
) -> tuple[ReleaseGoldItem, ...]:
    validate_public_goldsets(dev, blind)
    if type(blind_labels) is not tuple or len(blind_labels) != 60:
        _raise("goldset_invalid")
    dev_items = tuple(
        cast(DevGoldItem, _revalidate_item(item, DevGoldItem))
        for item in cast(tuple[object, ...], dev)
    )
    blind_items = tuple(
        cast(BlindGoldQuestion, _revalidate_item(item, BlindGoldQuestion))
        for item in cast(tuple[object, ...], blind)
    )
    labels = tuple(
        _revalidate_item(item, BlindGoldLabel)
        for item in cast(tuple[object, ...], blind_labels)
    )
    evidence_authority = _checked_canonical_evidence(canonical_evidence)
    if (
        any(label is None for label in labels)
        or evidence_authority is None
        or tuple(item.item_id for item in blind_items)
        != tuple(cast(BlindGoldLabel, label).item_id for label in labels)
    ):
        _raise("goldset_invalid")
    checked_labels = cast(tuple[BlindGoldLabel, ...], labels)
    release: list[ReleaseGoldItem] = []
    for item in dev_items:
        release.append(
            ReleaseGoldItem(
                **item.model_dump(mode="python"),
            )
        )
    for question, label in zip(blind_items, checked_labels, strict=True):
        release.append(
            ReleaseGoldItem(
                **question.model_dump(mode="python"),
                accepted_case_ids=label.accepted_case_ids,
                required_evidence=label.required_evidence,
                no_answer=label.no_answer,
                author_id=label.author_id,
                reviewer_id=label.reviewer_id,
            )
        )
    if sum(item.no_answer for item in release) < 30:
        _raise("goldset_invalid")
    for release_item in release:
        if any(
            case_id not in evidence_authority
            for case_id in release_item.accepted_case_ids
        ):
            _raise("goldset_invalid")
        if any(
            (reference.pdf_page_index, reference.source_span_index)
            not in evidence_authority.get(reference.case_id, frozenset())
            for reference in release_item.required_evidence
        ):
            _raise("goldset_invalid")
    return tuple(release)


def _read_regular_file(path: object, *, private: bool) -> bytes | None:
    if not isinstance(path, Path):
        return None
    absolute = Path(os.path.abspath(os.fspath(path)))
    if len(absolute.parts) < 2:
        return None
    directory_fd = -1
    descriptor = -1
    data = b""
    failed = False
    try:
        directory_fd = os.open(
            absolute.parts[0],
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        for component in absolute.parts[1:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(
            absolute.parts[-1],
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > _MAX_GOLDSET_BYTES
            or (private and (before.st_uid != os.geteuid() or before.st_mode & 0o077))
        ):
            failed = True
        else:
            remaining = _MAX_GOLDSET_BYTES + 1
            chunks: list[bytes] = []
            while remaining > 0:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if len(data) > _MAX_GOLDSET_BYTES or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                failed = True
    except OSError:
        failed = True
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                failed = True
    return None if failed else data


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("non-finite JSON number")


def _load_jsonl(
    path: object,
    model_type: type[_GoldModelT],
    *,
    expected_records: int,
    private: bool,
) -> tuple[_GoldModelT, ...]:
    data = _read_regular_file(path, private=private)
    if data is None or not data or not data.endswith(b"\n"):
        _raise("goldset_invalid")
    lines = data.splitlines()
    if len(lines) != expected_records or any(
        not line or len(line) > _MAX_JSONL_LINE_BYTES for line in lines
    ):
        _raise("goldset_invalid")
    records: list[_GoldModelT] = []
    failed = False
    try:
        for line in lines:
            payload = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if type(payload) is not dict:
                failed = True
                break
            records.append(model_type.model_validate_json(line))
    except (UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
        failed = True
    if failed or len(records) != expected_records:
        _raise("goldset_invalid")
    return tuple(records)


def load_public_goldsets(
    dev_path: Path = _DEFAULT_DEV_PATH,
    blind_path: Path = _DEFAULT_BLIND_PATH,
) -> tuple[tuple[DevGoldItem, ...], tuple[BlindGoldQuestion, ...]]:
    dev = _load_jsonl(dev_path, DevGoldItem, expected_records=140, private=False)
    blind = _load_jsonl(
        blind_path, BlindGoldQuestion, expected_records=60, private=False
    )
    validate_public_goldsets(dev, blind)
    return dev, blind


def load_release_goldsets(
    dev_path: Path,
    blind_path: Path,
    blind_labels_path: Path,
    *,
    canonical_evidence: object,
) -> tuple[ReleaseGoldItem, ...]:
    dev, blind = load_public_goldsets(dev_path, blind_path)
    labels = _load_jsonl(
        blind_labels_path,
        BlindGoldLabel,
        expected_records=60,
        private=True,
    )
    return combine_release_goldsets(
        dev,
        blind,
        labels,
        canonical_evidence=canonical_evidence,
    )
