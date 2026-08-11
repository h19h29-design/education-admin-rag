from __future__ import annotations

import json
import subprocess
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from src.integrations.gitlab_qa_bridge import (
    BridgeConfig,
    BridgeError,
    GitLabQaBridgeService,
    GitLabQaRequest,
    build_hermes_command,
    build_hermes_prompt,
    create_server,
    load_config,
    make_hermes_runner,
    parse_filter_output,
    run_answer_job,
    validate_and_transform,
)

REQUEST_ID = "senqa-0123456789abcdef0123456789abcdef"


def _request() -> GitLabQaRequest:
    return GitLabQaRequest(
        issue_iid=73,
        request_id=REQUEST_ID,
        question="수의계약이 가능한 경우를 알려줘",
        evidence={
            "complete_corpus": False,
            "production_eligible": False,
            "results": [
                {
                    "answer": "근거 답변",
                    "basis": "근거",
                    "candidate_sha256": "a" * 64,
                    "case_id": "senqa-2022-case-a",
                    "case_no": "1",
                    "citations": [
                        {
                            "bbox": [55.0, 66.0, 810.0, 700.0],
                            "pdf_page_index": 4,
                            "text_sha256": "b" * 64,
                        }
                    ],
                    "doc_id": "sen-qa-2022",
                    "domain": "계약",
                    "edition_year": 2022,
                    "facts": "사실",
                    "part": "계약 업무",
                    "pdf_pages": [4],
                    "question": "질문",
                    "review_status": "machine_extracted",
                    "subtopic": None,
                    "title": "근거 제목",
                }
            ],
            "schema_version": "sen-qa-preview-search-response/v1",
            "warning_code": "unreviewed_incomplete_preview",
        },
    )


def test_filter_output_becomes_exact_qa_request() -> None:
    payload = {
        "command": "ask",
        "evidence": _request().evidence,
        "project": "h19h19/education-admin-rag",
        "project_id": 428,
        "question": _request().question,
        "request_id": REQUEST_ID,
        "target_iid": 73,
    }

    checked = parse_filter_output(
        (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    )

    assert checked == _request()


def test_silent_and_malformed_filter_output_are_not_jobs() -> None:
    assert parse_filter_output(b"[SILENT]\n") is None
    assert parse_filter_output(b"not-json\n") is None


def test_prompt_treats_question_as_data_and_requires_grounded_preview_answer() -> None:
    prompt = build_hermes_prompt(_request())

    assert "도구를 호출하지 마세요" in prompt
    assert "근거 JSON에 없는 사실을 추가하지 마세요" in prompt
    assert "unreviewed_incomplete_preview" in prompt
    assert '"case_id":"senqa-2022-case-a"' in prompt
    assert '"question":"수의계약이 가능한 경우를 알려줘"' in prompt
    assert "production_eligible=false" in prompt


def test_hermes_command_pins_profile_and_zero_tool_allowlist(tmp_path: Path) -> None:
    hermes = tmp_path / "hermes"
    command = build_hermes_command(hermes, "PROMPT")

    assert command == (
        str(hermes),
        "-p",
        "hermes2",
        "-z",
        "PROMPT",
        "-t",
        "context_engine",
        "--ignore-rules",
    )


def test_hostile_filter_fields_fail_value_free() -> None:
    marker = "PRIVATE_BRIDGE_SENTINEL"
    payload = {
        "command": "ask",
        "evidence": _request().evidence,
        "project": "h19h19/education-admin-rag",
        "project_id": 428,
        "question": marker,
        "request_id": "bad",
        "target_iid": 73,
    }

    with pytest.raises(BridgeError) as caught:
        parse_filter_output(json.dumps(payload).encode())

    rendered = repr(caught.value) + str(caught.value)
    assert rendered == "BridgeError('bridge_invalid')bridge_invalid"
    assert marker not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_webhook_secret_and_event_are_checked_before_filter() -> None:
    calls: list[bytes] = []

    def filter_runner(raw: bytes) -> bytes:
        calls.append(raw)
        return b"[SILENT]\n"

    marker = b"PRIVATE_WEBHOOK_SENTINEL"
    with pytest.raises(BridgeError):
        validate_and_transform(
            marker,
            event="Note Hook",
            supplied_secret="wrong",
            expected_secret="correct-secret-12",
            filter_runner=filter_runner,
        )

    assert calls == []


def test_valid_webhook_runs_filter_once() -> None:
    expected = _request()

    def filter_runner(raw: bytes) -> bytes:
        assert raw == b'{"object_kind":"note"}'
        return (
            json.dumps(
                {
                    "command": "ask",
                    "evidence": expected.evidence,
                    "project": "h19h19/education-admin-rag",
                    "project_id": 428,
                    "question": expected.question,
                    "request_id": expected.request_id,
                    "target_iid": expected.issue_iid,
                }
            ).encode()
            + b"\n"
        )

    checked = validate_and_transform(
        b'{"object_kind":"note"}',
        event="Note Hook",
        supplied_secret="correct-secret-12",
        expected_secret="correct-secret-12",
        filter_runner=filter_runner,
    )

    assert checked == expected


def test_answer_job_runs_hermes_without_tools_then_delivers() -> None:
    captured: dict[str, object] = {}

    def hermes_runner(command: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "근거 답변\n".encode(), b"")

    def deliver(
        *, issue_iid: object, request_id: object, content: object, token: object
    ) -> str:
        captured["delivery"] = (issue_iid, request_id, content, token)
        return "91"

    note_id = run_answer_job(
        _request(),
        hermes_path=Path("/opt/hermes"),
        delivery_token="response-token",
        hermes_runner=hermes_runner,
        deliver=deliver,
    )

    command = captured["command"]
    assert command[0:4] == ("/opt/hermes", "-p", "hermes2", "-z")
    assert command[-3:] == ("-t", "context_engine", "--ignore-rules")
    assert captured["delivery"] == (
        73,
        REQUEST_ID,
        "근거 답변",
        "response-token",
    )
    assert note_id == "91"


def test_config_is_exact_local_only_and_does_not_embed_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filter_path = tmp_path / "filter"
    filter_path.write_text("#!/bin/sh\n")
    filter_path.chmod(0o500)
    hermes_path = tmp_path / "hermes"
    hermes_path.write_text("#!/bin/sh\n")
    hermes_path.chmod(0o500)
    search_path = tmp_path / "search"
    search_path.write_text("#!/bin/sh\n")
    search_path.chmod(0o500)
    search_config = tmp_path / "config.json"
    search_config.write_text("{}\n")
    search_config.chmod(0o600)
    values = {
        "SENQA_GITLAB_WEBHOOK_SECRET": "w" * 32,
        "SENQA_GITLAB_RESPONSE_TOKEN": "response-token",
        "SENQA_GITLAB_BOT_USERNAME": "senqa-worker-bot",
        "SENQA_GITLAB_QA_FILTER": str(filter_path),
        "SENQA_HERMES_COMMAND": str(hermes_path),
        "SENQA_PREVIEW_SEARCH_COMMAND": str(search_path),
        "SENQA_PREVIEW_SEARCH_CONFIG": str(search_config),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    config = load_config()

    assert isinstance(config, BridgeConfig)
    assert config.bind_host == "127.0.0.1"
    assert config.bind_port == 8645
    assert repr(config).count("response-token") == 0
    assert repr(config).count("w" * 32) == 0


def test_missing_bridge_secret_fails_value_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "PRIVATE_CONFIG_SENTINEL"
    monkeypatch.delenv("SENQA_GITLAB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("SENQA_GITLAB_RESPONSE_TOKEN", marker)

    with pytest.raises(BridgeError) as caught:
        load_config()

    assert marker not in repr(caught.value) + str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_http_server_accepts_only_exact_gitlab_qa_route() -> None:
    calls: list[tuple[bytes, str, str]] = []

    class Service:
        def accept(self, raw: bytes, *, event: str, supplied_secret: str) -> str:
            calls.append((raw, event, supplied_secret))
            return "accepted"

    server = create_server("127.0.0.1", 0, Service())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(
            "POST",
            "/webhooks/gitlab-qa",
            body=b'{"object_kind":"note"}',
            headers={
                "Content-Length": "22",
                "Content-Type": "application/json",
                "X-Gitlab-Event": "Note Hook",
                "X-Gitlab-Token": "secret",
            },
        )
        response = connection.getresponse()
        assert response.status == 202
        assert response.read() == b'{"status":"accepted"}\n'
        connection.close()

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("POST", "/wrong", body=b"{}")
        response = connection.getresponse()
        assert response.status == 404
        assert response.read() == b""
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert calls == [(b'{"object_kind":"note"}', "Note Hook", "secret")]


def test_bridge_service_suppresses_inflight_gitlab_retries(tmp_path: Path) -> None:
    request = _request()
    jobs: list[object] = []
    config = BridgeConfig(
        webhook_secret="w" * 32,
        delivery_token="response-token",
        bot_username="senqa-worker-bot",
        filter_path=tmp_path / "filter",
        hermes_path=Path("/opt/hermes"),
        search_path=tmp_path / "search",
        search_config=tmp_path / "config.json",
    )

    def filter_runner(_raw: bytes) -> bytes:
        return (
            json.dumps(
                {
                    "command": "ask",
                    "evidence": request.evidence,
                    "project": "h19h19/education-admin-rag",
                    "project_id": 428,
                    "question": request.question,
                    "request_id": request.request_id,
                    "target_iid": request.issue_iid,
                }
            ).encode()
            + b"\n"
        )

    service = GitLabQaBridgeService(
        config,
        filter_runner=filter_runner,
        answer_runner=lambda _request: None,
        thread_launcher=jobs.append,
    )

    assert (
        service.accept(b"{}", event="Note Hook", supplied_secret="w" * 32) == "accepted"
    )
    assert (
        service.accept(b"{}", event="Note Hook", supplied_secret="w" * 32)
        == "duplicate"
    )
    assert len(jobs) == 1

    jobs[0]()
    assert (
        service.accept(b"{}", event="Note Hook", supplied_secret="w" * 32) == "accepted"
    )


def test_hermes_runner_isolates_the_child_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        command: object, **options: object
    ) -> subprocess.CompletedProcess[bytes]:
        captured.update(options)
        options["stdout"].write(b"answer")
        return subprocess.CompletedProcess(command, 0, None, None)

    monkeypatch.setattr("src.integrations.gitlab_qa_bridge.subprocess.run", fake_run)

    completed = make_hermes_runner()(("/opt/hermes", "-z", "prompt"))

    assert captured["start_new_session"] is True
    assert completed.stdout == b"answer"
