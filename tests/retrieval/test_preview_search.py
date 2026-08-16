from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path("scripts/senqa_preview_search.py")


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


def _preview(tmp_path: Path) -> Path:
    database = tmp_path / "preview.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE preview_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE preview_cases(
          case_id TEXT PRIMARY KEY,doc_id TEXT NOT NULL,edition_year INTEGER NOT NULL,
          domain TEXT NOT NULL,part TEXT NOT NULL,subtopic TEXT,case_no TEXT NOT NULL,
          case_type TEXT NOT NULL,review_status TEXT NOT NULL,
          critical_field_review TEXT NOT NULL,pii_class TEXT NOT NULL,title TEXT NOT NULL,
          question TEXT NOT NULL,answer TEXT NOT NULL,basis TEXT NOT NULL,facts TEXT NOT NULL,
          source_spans_json TEXT NOT NULL,candidate_sha256 TEXT NOT NULL,
          preview_indexed INTEGER NOT NULL,production_eligible INTEGER NOT NULL,
          complete_corpus INTEGER NOT NULL,warning_code TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE VIRTUAL TABLE preview_fts USING fts5(
          case_id UNINDEXED,title,question,law_names,exact_tokens,char_ngrams,body,
          tokenize='unicode61 remove_diacritics 0'
        );
        """
    )
    meta = {
        "schema_version": "sen-qa-preview-rag/v2",
        "release_id": "corpus-test-12345678",
        "candidate_count": "1",
        "indexed_case_count": "1",
        "excluded_policy_count": "0",
        "parser_quarantine_count": "1",
        "registry_sha256": "a" * 64,
        "candidate_aggregate_sha256": "b" * 64,
        "warning_code": "unreviewed_incomplete_preview",
        "production_eligible": "0",
        "complete_corpus": "0",
    }
    connection.executemany(
        "INSERT INTO preview_meta(key,value) VALUES (?,?)", sorted(meta.items())
    )
    spans = json.dumps(
        [
            {
                "bbox": [0.1, 0.2, 0.8, 0.3],
                "doc_id": "sen-qa-2025",
                "pdf_page_index": 13,
                "text_sha256": "c" * 64,
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        "INSERT INTO preview_cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "senqa-2025-contract-general-1",
            "sen-qa-2025",
            2025,
            "계약",
            "일반",
            None,
            "1",
            "qa",
            "needs_review",
            "pending",
            "none",
            "학교회계 계약",
            "학교회계 계약 질문",
            "검증용 답변",
            "검증용 근거",
            "",
            spans,
            "d" * 64,
            1,
            0,
            0,
            "unreviewed_incomplete_preview",
        ),
    )
    connection.execute(
        "INSERT INTO preview_fts VALUES (?,?,?,?,?,?,?)",
        (
            "senqa-2025-contract-general-1",
            "학교회계 계약",
            "학교회계 계약 질문",
            "",
            "",
            "학교 학교회 회계 학교회 학교회계",
            "검증용 답변 검증용 근거",
        ),
    )
    connection.commit()
    connection.close()
    os.chmod(database, 0o600)
    database_sha = hashlib.sha256(database.read_bytes()).hexdigest()
    attestation = tmp_path / "preview-attestation.json"
    attestation.write_bytes(
        _canonical(
            {
                "schema_version": "sen-qa-preview-rag-attestation/v2",
                "preview_db_sha256": database_sha,
                "warning_code": "unreviewed_incomplete_preview",
                "production_eligible": False,
                "complete_corpus": False,
            }
        )
    )
    config = tmp_path / "config.json"
    config.write_bytes(
        _canonical(
            {
                "schema_version": "sen-qa-preview-search-config/v1",
                "database": str(database),
                "attestation": str(attestation),
                "expected_attestation_sha256": hashlib.sha256(
                    attestation.read_bytes()
                ).hexdigest(),
            }
        )
    )
    os.chmod(config, 0o600)
    return config


def _run(
    config: Path, query: str, *, limit: int = 5
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--config",
            str(config),
            "--json",
            "--limit",
            str(limit),
            "--",
            query,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_search_returns_grounded_preview_results(tmp_path: Path) -> None:
    completed = _run(_preview(tmp_path), "학교회계")
    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["warning_code"] == "unreviewed_incomplete_preview"
    assert payload["production_eligible"] is False
    assert payload["complete_corpus"] is False
    assert payload["results"][0]["pdf_pages"] == [13]
    assert payload["results"][0]["case_id"] == "senqa-2025-contract-general-1"


def test_search_is_deterministic_and_empty_is_grounded(tmp_path: Path) -> None:
    config = _preview(tmp_path)
    first = _run(config, "없는검색어")
    second = _run(config, "없는검색어")
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert json.loads(first.stdout)["results"] == []


@pytest.mark.parametrize("query", ["", "x" * 2049, "bad\x01query"])
def test_query_contract_is_value_free(tmp_path: Path, query: str) -> None:
    completed = _run(_preview(tmp_path), query)
    assert completed.returncode == 2
    assert completed.stdout == '{"error_code":"query_invalid"}\n'
    if query:
        assert query not in completed.stdout


def test_limit_overflow_is_rejected(tmp_path: Path) -> None:
    completed = _run(_preview(tmp_path), "학교", limit=21)
    assert completed.returncode == 2
    assert completed.stdout == '{"error_code":"query_invalid"}\n'


def test_symlink_config_is_rejected(tmp_path: Path) -> None:
    config = _preview(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(config)
    completed = _run(link, "학교")
    assert completed.returncode == 2
    assert completed.stdout == '{"error_code":"authority_invalid"}\n'


def test_attestation_mismatch_is_rejected(tmp_path: Path) -> None:
    config = _preview(tmp_path)
    payload = json.loads(config.read_bytes())
    payload["expected_attestation_sha256"] = "f" * 64
    config.write_bytes(_canonical(payload))
    completed = _run(config, "학교")
    assert completed.returncode == 2
    assert completed.stdout == '{"error_code":"authority_invalid"}\n'


def test_policy_row_is_rejected_at_query_time(tmp_path: Path) -> None:
    config = _preview(tmp_path)
    payload = json.loads(config.read_bytes())
    database = Path(payload["database"])
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE preview_cases SET pii_class='restricted' WHERE case_id=?",
        ("senqa-2025-contract-general-1",),
    )
    connection.commit()
    connection.close()
    attestation = Path(payload["attestation"])
    attestation_payload = json.loads(attestation.read_bytes())
    attestation_payload["preview_db_sha256"] = hashlib.sha256(
        database.read_bytes()
    ).hexdigest()
    attestation.write_bytes(_canonical(attestation_payload))
    payload["expected_attestation_sha256"] = hashlib.sha256(
        attestation.read_bytes()
    ).hexdigest()
    config.write_bytes(_canonical(payload))
    completed = _run(config, "학교")
    assert completed.returncode == 2
    assert completed.stdout == '{"error_code":"policy_invalid"}\n'
