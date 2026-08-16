#!/usr/bin/env python3
"""Read-only, authority-bound search for the private SEN-QA preview index."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import unicodedata
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote

_WARNING = "unreviewed_incomplete_preview"
_CONFIG_SCHEMA = "sen-qa-preview-search-config/v1"
_ATTESTATION_SCHEMA = "sen-qa-preview-rag-attestation/v2"
_DATABASE_SCHEMA = "sen-qa-preview-rag/v2"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_ATTESTATION_BYTES = 1024 * 1024
_MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_QUERY_CHARACTERS = 2_048
_MAX_RESULTS = 20
_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_REQUIRED_META = {
    "schema_version": _DATABASE_SCHEMA,
    "warning_code": _WARNING,
    "production_eligible": "0",
    "complete_corpus": "0",
}
_EXACT_TOKEN_RE = re.compile(
    r"senqa-[0-9]{4}(?:-[a-z0-9]+){3,}"
    r"|\u300c[^\u300c\u300d\r\n]{1,120}\u300d"
    r"|\uc81c[0-9]+\uc870(?:\uc758[0-9]+)?(?:\uc81c[0-9]+\ud56d)?(?:\uc81c[0-9]+\ud638)?"
    r"|(?<![0-9])[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?(?:\uc6d0|\ub9cc\uc6d0|\uc5b5\uc6d0)?"
    r"|(?<![0-9])[0-9]+(?:\.[0-9]+)?%"
)


class PreviewSearchError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _raise(code: str) -> NoReturn:
    raise PreviewSearchError(code) from None


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _read_regular_file(path: Path, *, max_bytes: int) -> bytes:
    descriptor: int | None = None
    failed = False
    data = b""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            failed = True
        else:
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if len(data) > max_bytes or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
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
    if failed:
        _raise("authority_invalid")
    return data


def _load_json(raw: bytes) -> dict[str, object]:
    parsed: object = None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        pass
    if type(parsed) is not dict:
        _raise("authority_invalid")
    return parsed


def _load_config(path: Path) -> tuple[Path, Path, str]:
    raw = _read_regular_file(path, max_bytes=_MAX_CONFIG_BYTES)
    payload = _load_json(raw)
    if (
        set(payload)
        != {
            "schema_version",
            "database",
            "attestation",
            "expected_attestation_sha256",
        }
        or payload.get("schema_version") != _CONFIG_SCHEMA
    ):
        _raise("authority_invalid")
    database = payload.get("database")
    attestation = payload.get("attestation")
    expected = payload.get("expected_attestation_sha256")
    if (
        type(database) is not str
        or type(attestation) is not str
        or type(expected) is not str
        or _SHA256_RE.fullmatch(expected) is None
    ):
        _raise("authority_invalid")
    database_path = Path(database)
    attestation_path = Path(attestation)
    if not database_path.is_absolute() or not attestation_path.is_absolute():
        _raise("authority_invalid")
    return database_path, attestation_path, expected


def _verify_authority(database: Path, attestation: Path, expected: str) -> None:
    attestation_raw = _read_regular_file(attestation, max_bytes=_MAX_ATTESTATION_BYTES)
    if hashlib.sha256(attestation_raw).hexdigest() != expected:
        _raise("authority_invalid")
    payload = _load_json(attestation_raw)
    database_sha256 = payload.get("preview_db_sha256")
    if (
        payload.get("schema_version") != _ATTESTATION_SCHEMA
        or payload.get("warning_code") != _WARNING
        or payload.get("production_eligible") is not False
        or payload.get("complete_corpus") is not False
        or type(database_sha256) is not str
        or _SHA256_RE.fullmatch(database_sha256) is None
    ):
        _raise("authority_invalid")
    database_raw = _read_regular_file(database, max_bytes=_MAX_DATABASE_BYTES)
    if hashlib.sha256(database_raw).hexdigest() != database_sha256:
        _raise("authority_invalid")


def _normalize_query(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_QUERY_CHARACTERS
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs", "Co", "Cn"}
            and not character.isspace()
            for character in value
        )
    ):
        _raise("query_invalid")
    normalized = " ".join(unicodedata.normalize("NFC", value).split())
    if not normalized:
        _raise("query_invalid")
    return normalized


def _ngrams(value: str) -> tuple[str, ...]:
    compact = "".join(
        character
        for character in value
        if unicodedata.category(character)[0] in {"L", "N"}
    )
    return tuple(
        sorted(
            {
                compact[index : index + width]
                for width in (2, 3)
                for index in range(max(0, len(compact) - width + 1))
            }
        )
    )


def _phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _match_expression(query: str) -> str:
    exact = tuple(
        dict.fromkeys(match.group(0) for match in _EXACT_TOKEN_RE.finditer(query))
    )
    clauses = [f"exact_tokens:{_phrase(token)}" for token in exact]
    clauses.extend(f"char_ngrams:{_phrase(gram)}" for gram in _ngrams(query))
    clauses.extend(
        "{title question law_names body}:" + _phrase(term)
        for term in query.split()
        if term not in exact
    )
    if not clauses:
        _raise("query_invalid")
    return " OR ".join(clauses)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if path.is_symlink():
        _raise("authority_invalid")
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        _raise("authority_invalid")
    return connection


def _metadata(connection: sqlite3.Connection) -> None:
    try:
        rows = connection.execute("SELECT key,value FROM preview_meta").fetchall()
    except sqlite3.Error:
        _raise("authority_invalid")
    metadata = {row["key"]: row["value"] for row in rows}
    if any(metadata.get(key) != value for key, value in _REQUIRED_META.items()):
        _raise("authority_invalid")


def _search(path: Path, query: str, limit: int) -> list[dict[str, object]]:
    expression = _match_expression(query)
    connection = _connect_read_only(path)
    try:
        _metadata(connection)
        rows = connection.execute(
            """
            SELECT p.case_id,p.doc_id,p.edition_year,p.domain,p.part,p.subtopic,
                   p.case_no,p.review_status,p.pii_class,p.title,p.question,p.answer,
                   p.basis,p.facts,p.source_spans_json,p.candidate_sha256,
                   p.production_eligible,p.complete_corpus,p.warning_code
            FROM preview_fts f
            JOIN preview_cases p ON p.case_id=f.case_id
            WHERE preview_fts MATCH ?
            ORDER BY bm25(preview_fts,0.0,8.0,6.0,5.0,5.0,3.0,1.0),p.case_id
            LIMIT ?
            """,
            (expression, limit),
        ).fetchall()
    except sqlite3.Error:
        _raise("authority_invalid")
    finally:
        connection.close()
    results: list[dict[str, object]] = []
    for row in rows:
        if (
            row["pii_class"] in {"restricted", "public_credit"}
            or row["production_eligible"] != 0
            or row["complete_corpus"] != 0
            or row["warning_code"] != _WARNING
        ):
            _raise("policy_invalid")
        spans: object = None
        try:
            spans = json.loads(row["source_spans_json"])
        except (TypeError, json.JSONDecodeError):
            pass
        if type(spans) is not list or not spans:
            _raise("authority_invalid")
        pages: set[int] = set()
        citations: list[dict[str, object]] = []
        for span in spans:
            if type(span) is not dict:
                _raise("authority_invalid")
            page = span.get("pdf_page_index")
            bbox = span.get("bbox")
            text_sha256 = span.get("text_sha256")
            if (
                type(page) is not int
                or page < 1
                or type(bbox) is not list
                or len(bbox) != 4
                or type(text_sha256) is not str
                or _SHA256_RE.fullmatch(text_sha256) is None
            ):
                _raise("authority_invalid")
            pages.add(page)
            citations.append(
                {"pdf_page_index": page, "bbox": bbox, "text_sha256": text_sha256}
            )
        results.append(
            {
                "case_id": row["case_id"],
                "doc_id": row["doc_id"],
                "edition_year": row["edition_year"],
                "domain": row["domain"],
                "part": row["part"],
                "subtopic": row["subtopic"],
                "case_no": row["case_no"],
                "review_status": row["review_status"],
                "title": row["title"],
                "question": row["question"],
                "answer": row["answer"],
                "basis": row["basis"],
                "facts": row["facts"],
                "candidate_sha256": row["candidate_sha256"],
                "pdf_pages": sorted(pages),
                "citations": citations,
            }
        )
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--json", action="store_true", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("query", nargs=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if type(arguments.limit) is not int or not 1 <= arguments.limit <= _MAX_RESULTS:
            _raise("query_invalid")
        query = _normalize_query(arguments.query[0])
        database, attestation, expected = _load_config(arguments.config)
        _verify_authority(database, attestation, expected)
        response = {
            "schema_version": "sen-qa-preview-search-response/v1",
            "query": query,
            "warning_code": _WARNING,
            "production_eligible": False,
            "complete_corpus": False,
            "results": _search(database, query, arguments.limit),
        }
        rendered = _canonical_json(response)
        if len(rendered) > _MAX_OUTPUT_BYTES:
            _raise("output_invalid")
        sys.stdout.buffer.write(rendered)
        return 0
    except (PreviewSearchError, SystemExit) as error:
        code = error.code if isinstance(error, PreviewSearchError) else "query_invalid"
        sys.stdout.buffer.write(_canonical_json({"error_code": code}))
        return 2
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, UnicodeError):
        sys.stdout.buffer.write(_canonical_json({"error_code": "search_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
