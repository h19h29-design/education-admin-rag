# 교육행정 질문답변 말뭉치·RAG 기반 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 서울특별시교육청 2020~2025년 질문·답변 사례집 PDF 6권을 재현 가능하게 구조화하고, 원문 페이지까지 추적되는 검증된 말뭉치와 SQLite FTS5 + `BAAI/bge-m3` + Qdrant 하이브리드 검색 기반을 구축한다.

**Architecture:** 원본 PDF는 읽기 전용 source volume에 두고, 연도별 native/OCR 추출기가 immutable page JSONL을 만든다. 연도별 파서와 품질 게이트가 승인된 사례만 canonical SQLite/JSONL로 승격하며, lexical·dense 색인은 같은 release ID로 생성한다. 검색은 각 색인의 상위 25건을 RRF(`k=60`)로 결합하고 상위 8개 부모 사례와 정확한 근거 span만 반환한다.

**Tech Stack:** Python 3.11, uv, Pydantic 2, PyMuPDF, PaddleOCR 한국어 모델, SQLite 3 WAL/FTS5, sentence-transformers `BAAI/bge-m3`, Qdrant, Typer, pytest, Ruff, mypy, Docker Compose.

## Global Constraints

- 기준 설계는 `docs/superpowers/specs/2026-08-08-education-admin-corpus-rag-foundation-design.md`다. 구현 중 계약을 바꾸려면 먼저 설계 변경을 별도 커밋으로 승인받는다.
- 기존 `교육행정_AI_Launcher.html`의 UI·내장 2,418건·legacy ID는 비교용 기준선으로 보존하고 새 canonical 원문으로 복사하지 않는다. 단, 노출된 키와 브라우저 직접 AI 호출 제거는 출시 전 보안 예외로 별도 커밋한다.
- 현재 작업 폴더의 미커밋 `교육행정_AI_Launcher.html`, `README.md`, `index.html`, `run.command`, `tests/test_launcher.py`는 사용자 작업으로 간주한다. 구현은 `superpowers:using-git-worktrees`로 만든 깨끗한 `codex/rag-foundation` worktree에서 수행한다.
- 원본 PDF, OCR 이미지, canonical DB, Qdrant snapshot은 Git에 커밋하지 않는다. Git에는 manifest, 스키마, 코드, 합성 fixture, 골드 질문, 작은 보고서 요약만 넣는다.
- 원본 파일명·SHA-256·페이지 수가 manifest와 다르면 해당 문서 처리를 즉시 실패시킨다. 실패 페이지를 빈 텍스트로 대체하지 않는다.
- 자동 정규화는 금액, 비율, 날짜, 법령 조문, 문서번호, 가능·불가 결론, 익명화 기호를 변경하지 않는다.
- `machine_extracted`, `needs_review`, `rejected`는 `search_eligible=false`, `answer_eligible=false`다. `search_approved`는 검색만 허용하고 `approved`만 후속 모델 근거로 사용할 수 있다.
- `restricted`와 `public_credit`은 일반 검색·답변 색인에서 제외한다. 개인정보 보고서에는 실제 탐지값을 쓰지 않고 종류·건수·위치 ID만 쓴다.
- OCR·임베딩 모델의 정확한 revision/digest를 lock manifest에 기록한다. 운영 ingestion 중 임의 인터넷 다운로드를 허용하지 않는다.
- 모든 테스트는 네트워크 없이 실행 가능해야 한다. 실제 PaddleOCR·Qdrant·6권 전체 실행은 명시적 integration/release 명령으로 분리한다.
- 각 작업은 아래에 적힌 파일만 `git add`하고, `git add -A`를 사용하지 않는다. 테스트 실패 상태에서는 커밋하지 않는다.
- 기존 Git 기록에 노출된 Gemini 키는 새 설정으로 재사용하지 않는다. 담당자가 제공자 콘솔에서 폐기·사용량 확인을 완료하기 전에는 어떤 웹 배포도 진행하지 않는다.

## Repository Target

```text
pyproject.toml
uv.lock
.python-version
.gitignore
.github/workflows/security.yml
docker/ingestion.Dockerfile
docker/indexer.Dockerfile
config/
├── models.lock.json
└── retrieval.toml
data/
├── manifests/sen_qa_sources.json
├── schemas/{document,case,chunk,law-ref,case-relation,search-result}.schema.json
└── eval/{retrieval-dev,retrieval-blind}.jsonl
src/
├── ingestion/{manifest,extract_common,extract_native,extract_ocr,normalize,privacy,quality}.py
├── ingestion/{parse_common,parse_2020,parse_2021_2022,parse_2023,parse_2024_2025}.py
├── corpus/{models,ids,relations,chunking,storage,build}.py
├── retrieval/{query,lexical,dense,fusion,service}.py
├── evaluation/{goldset,ingestion_metrics,retrieval_metrics}.py
└── cli.py
scripts/
├── build-corpus.sh
├── build-indexes.sh
├── evaluate-release.sh
├── verify-release.sh
└── backup-release.sh
tests/
├── fixtures/{native-pages,ocr-pages,parsed-cases}/
├── ingestion/
├── corpus/
├── retrieval/
└── evaluation/
docs/runbooks/{source-intake,manual-review,index-release,backup-restore}.md
```

## Task 1: 격리된 구현 작업공간과 Python 품질 게이트

**Files:**

- Create: `.python-version`
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `docker/ingestion.Dockerfile`
- Modify: `.gitignore`
- Create: `src/ingestion/__init__.py`
- Create: `src/corpus/__init__.py`
- Create: `src/retrieval/__init__.py`
- Create: `src/evaluation/__init__.py`
- Create: `src/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: 깨끗한 worktree를 만들고 기준 상태를 확인한다.**

```bash
git worktree add ../education-admin-launcher-rag -b codex/rag-foundation main
cd ../education-admin-launcher-rag
git status --short
```

Expected: 출력이 없고, 기존 작업 폴더의 미커밋 파일은 새 worktree에 나타나지 않는다.

- [ ] **Step 2: 테스트를 실행할 최소 의존성 환경만 먼저 고정한다.**

`.python-version`, `pyproject.toml`, `docker/ingestion.Dockerfile`, `.gitignore`를 만든다. `pyproject.toml`에 `typer`, `pydantic`, `pymupdf`, `sentence-transformers`, `qdrant-client`를 기본 runtime dependency로 선언한다. `paddleocr`와 CPU용 `paddlepaddle`은 Linux/amd64 ingestion image에서만 설치하는 `ocr` optional dependency로 분리한다. `pytest`, `pytest-cov`, `ruff`, `mypy`, `jsonschema`는 dev dependency로 선언한다. `docker/ingestion.Dockerfile`은 digest로 고정한 Python 3.11 slim base와 `uv sync --frozen --extra ocr`를 사용한다.

```bash
uv lock
uv sync --frozen --dev
```

Expected: exact version과 artifact hash가 포함된 `uv.lock`이 생성되고 `uv run pytest --version`이 성공한다. 아직 `src/cli.py`는 만들지 않는다.

- [ ] **Step 3: CLI가 아직 없음을 보여주는 실패 테스트를 작성한다.**

```python
# tests/test_cli.py
import subprocess
import sys

from typer.testing import CliRunner

from src.cli import app


def test_cli_reports_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "education-admin-rag 0.1.0"


def test_module_entrypoint_reports_version() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "src.cli", "version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout.strip() == "education-admin-rag 0.1.0"
```

- [ ] **Step 4: 실패를 확인한다.**

```bash
uv run pytest tests/test_cli.py -q
```

Expected: `ModuleNotFoundError` 또는 `No such command 'version'`으로 실패한다.

- [ ] **Step 5: CLI와 package scaffold를 최소 구현한다.**

```python
# src/cli.py
import typer

app = typer.Typer(no_args_is_help=True)


@app.command()
def version() -> None:
    typer.echo("education-admin-rag 0.1.0")


if __name__ == "__main__":
    app()
```

`.gitignore`에는 다음 생성물을 추가한다.

```gitignore
artifacts/
data/source/
*.sqlite3
*.sqlite3-shm
*.sqlite3-wal
.pytest_cache/
.mypy_cache/
.ruff_cache/
```

- [ ] **Step 6: 품질 게이트를 실행한다.**

```bash
uv run pytest tests/test_cli.py -q
uv run ruff check src tests
uv run mypy src
```

Expected: 테스트 2개가 통과하고 Ruff·mypy 오류가 0개다.

- [ ] **Step 7: 명시된 scaffold 파일만 커밋한다.**

```bash
git add .python-version pyproject.toml uv.lock .gitignore docker/ingestion.Dockerfile src/ingestion/__init__.py src/corpus/__init__.py src/retrieval/__init__.py src/evaluation/__init__.py src/cli.py tests/test_cli.py
git commit -m "build: scaffold corpus pipeline"
```

## Task 1A: 기존 노출 키 차단과 history-aware secret gate

**Files:**

- Modify: `교육행정_AI_Launcher.html` (키·브라우저 직접 호출만 제거)
- Create: `tests/security/test_no_client_ai_secret.py`
- Create: `config/gitleaks.toml`
- Create: `config/revoked-secrets-baseline.json`
- Create: `scripts/scan-secrets.sh`
- Create: `.github/workflows/security.yml`
- Create: `docs/security/revoked-secret-register.md`

- [ ] **Step 1: 클라이언트 secret과 직접 AI 호출을 금지하는 실패 테스트를 작성한다.**

```python
# tests/security/test_no_client_ai_secret.py
from pathlib import Path


def test_legacy_html_has_no_client_side_model_secret_or_direct_endpoint() -> None:
    html = Path("교육행정_AI_Launcher.html").read_text(encoding="utf-8")
    assert "generativelanguage.googleapis.com" not in html
    assert "AIza" not in html
```

- [ ] **Step 2: 현재 main 기준선에서 RED를 확인한다.**

```bash
uv run pytest tests/security/test_no_client_ai_secret.py -q
```

Expected: 기존 브라우저 직접 호출 또는 key-shaped 문자열 때문에 실패한다. 실제 키 값은 테스트 출력에 노출하지 않는다.

- [ ] **Step 3: 제공자 콘솔에서 기존 키를 폐기하고 사용량·과금 이상을 확인한다.**

담당자가 키 값을 기록하지 않고 provider, 최초/마지막 노출 commit, 폐기 UTC 시각, 사용량 확인 결과, 승인자만 `docs/security/revoked-secret-register.md`에 기록한다. 폐기 증적이 없으면 이 단계와 모든 배포 단계는 완료 처리하지 않는다.

- [ ] **Step 4: legacy HTML의 직접 호출을 비활성화한다.**

내장 사례와 ID는 보존하고 literal key, Google endpoint 호출, 일반지식 fallback만 제거한다. AI 버튼은 “근거 검색 기반 서비스 준비 중”이라는 비활성 상태로 바꾸며 새 secret이나 임시 프록시를 넣지 않는다.

- [ ] **Step 5: 전체 Git history의 알려진 폐기 secret과 신규 secret을 분리한다.**

digest로 고정한 gitleaks 실행 환경을 `scripts/scan-secrets.sh`에 사용한다. 최초 실행에서 과거 키 한 건의 fingerprint·commit·path만 redacted baseline에 기록하고 actual secret은 저장하지 않는다. 이후 working tree와 전체 history를 검사해 baseline에 없는 finding이 하나라도 있으면 exit 1이다.

- [ ] **Step 6: CI에서 secret gate를 실행한다.**

`.github/workflows/security.yml`은 pull request와 main push마다 `bash scripts/scan-secrets.sh`와 `uv run pytest tests/security -q`를 실행한다. action과 scanner image는 mutable tag가 아니라 commit/image digest로 고정한다.

- [ ] **Step 7: 보안 테스트를 통과시키고 별도 커밋한다.**

```bash
bash scripts/scan-secrets.sh
uv run pytest tests/security -q
git add 교육행정_AI_Launcher.html tests/security/test_no_client_ai_secret.py config/gitleaks.toml config/revoked-secrets-baseline.json scripts/scan-secrets.sh .github/workflows/security.yml docs/security/revoked-secret-register.md
git commit -m "security: disable exposed client AI integration"
```

## Task 2: 원본 6권 manifest와 변경 탐지

**Files:**

- Create: `data/manifests/sen_qa_sources.json`
- Create: `src/ingestion/manifest.py`
- Create: `tests/ingestion/test_manifest.py`
- Create: `docs/runbooks/source-intake.md`

- [ ] **Step 1: manifest 계약의 실패 테스트를 작성한다.**

```python
# tests/ingestion/test_manifest.py
from pathlib import Path

import pytest

from src.ingestion.manifest import ManifestError, load_manifest, resolve_source, verify_source


def test_manifest_contains_exactly_2020_through_2025() -> None:
    docs = load_manifest(Path("data/manifests/sen_qa_sources.json"))
    assert [doc.edition_year for doc in docs] == [2020, 2021, 2022, 2023, 2024, 2025]
    assert [doc.pdf_page_count for doc in docs] == [302, 383, 386, 168, 324, 314]


def test_source_hash_mismatch_is_fatal(tmp_path: Path) -> None:
    source = tmp_path / "book.pdf"
    source.write_bytes(b"changed")
    expected_doc = load_manifest(Path("data/manifests/sen_qa_sources.json"))[-1].model_copy(
        update={"source_filename": "book.pdf", "sha256": "0" * 64, "pdf_page_count": 1}
    )
    with pytest.raises(ManifestError, match="SHA-256 mismatch"):
        verify_source(source, expected_doc)


@pytest.mark.parametrize("source_relpath", ["../outside.pdf", "/tmp/outside.pdf"])
def test_source_path_cannot_escape_root(tmp_path: Path, source_relpath: str) -> None:
    expected_doc = load_manifest(Path("data/manifests/sen_qa_sources.json"))[0].model_copy(
        update={"source_relpath": source_relpath}
    )
    with pytest.raises(ManifestError, match="source root"):
        resolve_source(tmp_path / "source", expected_doc)


def test_source_symlink_cannot_escape_root(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (source_root / "book.pdf").symlink_to(outside)
    expected_doc = load_manifest(Path("data/manifests/sen_qa_sources.json"))[0].model_copy(
        update={"source_relpath": "book.pdf"}
    )
    with pytest.raises(ManifestError, match="source root"):
        resolve_source(source_root, expected_doc)
```

- [ ] **Step 2: 테스트 실패를 확인한다.**

```bash
uv run pytest tests/ingestion/test_manifest.py -q
```

Expected: `src.ingestion.manifest`가 없어 collection 단계에서 실패한다.

- [ ] **Step 3: 엄격한 manifest 모델과 검증기를 구현한다.**

```python
# src/ingestion/manifest.py 핵심 계약
class SourceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_id: str
    edition_year: int
    official_title: str
    publisher: str
    registration_no: str | None
    source_period_start: date | None
    source_period_end: date | None
    source_filename: str
    source_relpath: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_page_count: int = Field(gt=0)
    page_size_profiles: tuple[PageSizeProfile, ...]
    extraction_method: Literal["native", "ocr"]
    source_dpi: int | None
    render_dpi: int | None
    page_numbering: PageNumberingPolicy
    official_public_url: AnyHttpUrl | None
    official_url_status: Literal["unverified", "verified", "unavailable"]
    redistribution_status: Literal["unverified", "approved", "denied"]
    access_level: Literal["staff", "public"]
```

`PageSizeProfile`은 적용 시작·끝 PDF 페이지와 width/height point를 가진다. `PageNumberingPolicy`는 `mode`, 본문 시작·끝 PDF 페이지, 정수 offset을 가진다. 앞표지·목차처럼 본문 범위 밖인 페이지의 `page_label`은 `null`이며 음수 페이지를 만들지 않는다. `resolve_source()`는 절대경로와 `..`를 거부하고 symlink까지 resolve한 실제 경로가 resolved `SEN_QA_SOURCE_ROOT` 하위인지 `Path.relative_to()`로 확인한다. `verify_source(source_path, expected_doc)`는 `SourceDocument` 하나를 계약으로 받아 파일명, streaming SHA-256, `fitz.open(source_path).page_count`, page-size profile을 모두 확인하고 하나라도 다르면 `ManifestError`를 낸다.

- [ ] **Step 4: 6권의 고정 메타데이터와 계산된 SHA-256을 기록한다.**

`sen_qa_sources.json`은 공식 제목·발행처·등록번호·확인 가능한 수록기간·원본 파일명·source volume 상대경로, 연도 순서, 페이지 수 `[302,383,386,168,324,314]`, 실제 page-size profile, 추출 방식 `native/native/native/ocr/ocr/ocr`, source/render DPI를 기록한다. 본문 page policy는 `2020·2023=offset -6`, 나머지 `offset 0`이며 각 문서의 실제 본문 시작·끝 범위 안에서만 적용한다. SHA-256은 원본 파일에서 `shasum -a 256`으로 계산한 64자리 소문자 값만 허용한다. 공식 URL을 검증하기 전에는 `official_public_url=null`, `official_url_status=unverified`; 공개 근거를 확인하기 전 6권 모두 `redistribution_status=unverified`, `access_level=staff`로 둔다.

- [ ] **Step 5: intake 명령과 성공 기준을 문서화하고 테스트한다.**

```bash
SEN_QA_SOURCE_ROOT=/volume1/education-admin/source uv run python -m src.cli verify-sources --manifest data/manifests/sen_qa_sources.json
uv run pytest tests/ingestion/test_manifest.py -q
```

Expected: 올바른 volume에서는 `verified=6 changed=0 failed=0`; 잘못된 파일 하나라도 있으면 non-zero exit다.

- [ ] **Step 6: 커밋한다.**

```bash
git add data/manifests/sen_qa_sources.json src/ingestion/manifest.py tests/ingestion/test_manifest.py docs/runbooks/source-intake.md src/cli.py
git commit -m "feat: verify source document manifest"
```

## Task 3: 정규 데이터 모델, 상태 불변식, 안정적 ID

**Files:**

- Create: `src/corpus/models.py`
- Create: `src/corpus/ids.py`
- Create: `data/schemas/document.schema.json`
- Create: `data/schemas/case.schema.json`
- Create: `data/schemas/chunk.schema.json`
- Create: `data/schemas/law-ref.schema.json`
- Create: `data/schemas/case-relation.schema.json`
- Create: `tests/corpus/test_models.py`
- Create: `tests/corpus/test_ids.py`

- [ ] **Step 1: eligibility와 ID 불변식의 실패 테스트를 작성한다.**

```python
# tests/corpus/test_models.py
import pytest
from pydantic import ValidationError

from src.corpus.models import Case


def test_machine_extracted_case_cannot_be_searchable(case_payload: dict) -> None:
    case_payload.update(review_status="machine_extracted", search_eligible=True)
    with pytest.raises(ValidationError, match="eligibility"):
        Case.model_validate(case_payload)


def test_public_credit_never_enters_indexes(case_payload: dict) -> None:
    case_payload.update(pii_class="public_credit", review_status="approved", search_eligible=True)
    with pytest.raises(ValidationError, match="public_credit"):
        Case.model_validate(case_payload)
```

```python
# tests/corpus/test_ids.py
import pytest

from src.corpus.ids import IssuedIdRegistry, make_case_id, title_hash


def test_duplicate_number_gets_stable_page_and_title_suffix() -> None:
    assert make_case_id(2025, "계약", "계약 일반", "1", 13, "2단계 입찰", duplicate=True) == (
        "senqa-2025-contract-contract-general-1-p13-" + title_hash("2단계 입찰")
    )


def test_retired_case_id_is_never_reissued() -> None:
    registry = IssuedIdRegistry.in_memory()
    registry.issue("senqa-2025-contract-contract-general-1")
    registry.retire("senqa-2025-contract-contract-general-1")
    with pytest.raises(ValueError, match="already issued"):
        registry.issue("senqa-2025-contract-contract-general-1")
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run pytest tests/corpus/test_models.py tests/corpus/test_ids.py -q
```

Expected: 두 모듈이 없어 실패한다.

- [ ] **Step 3: `Document`, `SourceSpan`, `Case`, `Chunk`, `LawRef`, `CaseRelation`, `IngestionRun`을 구현한다.**

모든 모델은 `extra="forbid"`를 사용한다. `Case`의 model validator는 다음 진리표를 강제한다.

| review_status | search_eligible | answer_eligible |
|---|---:|---:|
| machine_extracted / needs_review / rejected | false | false |
| search_approved | true | false |
| approved + pii none/anonymized_case/quasi_identifier | true | true 또는 false |
| 모든 상태 + public_credit/restricted | false | false |

`SourceSpan.pdf_page_index`는 1부터 시작하고, `bbox`는 `[x0,y0,x1,y1]`, `text_sha256`은 64자리 소문자다.

- [ ] **Step 4: 안정적 ID와 release ID를 구현한다.**

`case_id = senqa-{year}-{domain_slug}-{part_slug}-{case_no}`로 생성하고 충돌 때만 `-p{start_page}-{title_sha256[:8]}`를 붙인다. `IssuedIdRegistry`는 active와 retired ID를 모두 조회해 과거 발급 ID 재사용을 거부한다. `release_id = corpus-{UTC YYYYMMDDHHMMSS}-{git_sha[:8]}`이며 git SHA가 8자리 미만이면 실패한다. 한글 slug는 코드에 고정된 업무분야 사전으로 영문 변환하고 미등록 값은 `sha256(value)[:10]`을 쓴다.

- [ ] **Step 5: Pydantic 모델에서 JSON Schema를 생성하고 drift를 검사한다.**

```bash
uv run python -m src.cli export-schemas --output data/schemas
uv run python -m src.cli export-schemas --output /tmp/sen-qa-schemas
diff -ru data/schemas /tmp/sen-qa-schemas
```

Expected: diff 출력이 없고 모든 schema의 `$id`가 repository-relative 고정값이다.

- [ ] **Step 6: 테스트와 타입 검사를 통과시킨다.**

```bash
uv run pytest tests/corpus/test_models.py tests/corpus/test_ids.py -q
uv run mypy src/corpus
```

- [ ] **Step 7: 커밋한다.**

```bash
git add src/corpus/models.py src/corpus/ids.py data/schemas/document.schema.json data/schemas/case.schema.json data/schemas/chunk.schema.json data/schemas/law-ref.schema.json data/schemas/case-relation.schema.json tests/corpus/test_models.py tests/corpus/test_ids.py src/cli.py
git commit -m "feat: define canonical corpus contracts"
```

## Task 4: 공통 page JSONL과 2020~2022 native 추출

**Files:**

- Create: `src/ingestion/extract_common.py`
- Create: `src/ingestion/extract_native.py`
- Create: `tests/fixtures/native-pages/2020-odd-page.json`
- Create: `tests/fixtures/native-pages/2022-continuation.json`
- Create: `tests/ingestion/test_extract_native.py`

- [ ] **Step 1: 좌표·페이지 규칙의 실패 테스트를 작성한다.**

```python
# tests/ingestion/test_extract_native.py
from src.ingestion.extract_common import printed_page_label
from src.ingestion.extract_native import remove_repeated_margin_blocks


def test_2020_printed_page_is_pdf_page_minus_six() -> None:
    assert printed_page_label(2020, pdf_page_index=13) == "7"


def test_front_matter_has_no_printed_page_label() -> None:
    assert printed_page_label(2020, pdf_page_index=3) is None


def test_vertical_navigation_is_removed_but_body_is_preserved(native_2020_page) -> None:
    cleaned = remove_repeated_margin_blocks(
        native_2020_page,
        repeated_signatures=frozenset({"right-margin:19편"}),
    )
    assert "19편" not in cleaned.normalized_text
    assert "계약방법" in cleaned.normalized_text
    assert cleaned.raw_blocks == native_2020_page.raw_blocks
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run pytest tests/ingestion/test_extract_native.py -q
```

- [ ] **Step 3: immutable page·block·line·span 계약을 구현한다.**

`RawPage`는 `doc_id`, 1-based `pdf_page_index`, `page_label`, page size, render SHA-256, `blocks[].lines[].spans[]`와 각 span의 text, bbox, font, size, confidence를 가진다. native confidence는 `1.0`으로 기록한다. 원시 block 목록은 정제 함수가 수정하지 못하도록 frozen model로 둔다.

- [ ] **Step 4: PyMuPDF native 추출과 반복 margin 제거를 구현한다.**

`page.get_text("dict", sort=True)`만 사용하며 2020 홀수면 우측 세로 내비게이션은 `(x0/page_width >= 0.90) AND 반복 빈도 >= 문서 본문 페이지의 40%`일 때만 제외한다. 2021~2022 header/footer는 y 좌표 상·하단 8%와 반복문자 조건을 동시에 만족해야 제외한다.

- [ ] **Step 5: 페이지 실패 격리와 deterministic JSONL을 검증한다.**

```bash
SEN_QA_SOURCE_ROOT=/volume1/education-admin/source uv run python -m src.cli extract-native --manifest data/manifests/sen_qa_sources.json --years 2020,2021,2022 --output artifacts/raw-pages
(cd artifacts/raw-pages && shasum -a 256 *.jsonl) > /tmp/native-run-1.sha256
SEN_QA_SOURCE_ROOT=/volume1/education-admin/source uv run python -m src.cli extract-native --manifest data/manifests/sen_qa_sources.json --years 2020,2021,2022 --output artifacts/raw-pages-rerun
(cd artifacts/raw-pages-rerun && shasum -a 256 *.jsonl) > /tmp/native-run-2.sha256
diff -u /tmp/native-run-1.sha256 /tmp/native-run-2.sha256
```

Expected: 두 실행의 파일 내용 hash가 동일하고 총 페이지 수는 `302+383+386=1071`이다.

- [ ] **Step 6: 단위 테스트를 통과시키고 커밋한다.**

```bash
uv run pytest tests/ingestion/test_extract_native.py -q
git add src/ingestion/extract_common.py src/ingestion/extract_native.py tests/fixtures/native-pages/2020-odd-page.json tests/fixtures/native-pages/2022-continuation.json tests/ingestion/test_extract_native.py src/cli.py
git commit -m "feat: extract native PDF pages with provenance"
```

## Task 5: 2023~2025 전체 페이지 PaddleOCR 추출

**Files:**

- Create: `config/models.lock.json`
- Modify: `docker/ingestion.Dockerfile`
- Create: `src/ingestion/extract_ocr.py`
- Create: `tests/fixtures/ocr-pages/2023-low-dpi.json`
- Create: `tests/fixtures/ocr-pages/2025-mixed-script.json`
- Create: `tests/ingestion/test_extract_ocr.py`

- [ ] **Step 1: 연도별 DPI와 품질 플래그 실패 테스트를 작성한다.**

```python
# tests/ingestion/test_extract_ocr.py
from src.ingestion.extract_ocr import ocr_policy


def test_ocr_render_policies_are_fixed() -> None:
    assert ocr_policy(2023).render_dpi == 300
    assert ocr_policy(2023).quality_flags == ("source_approx_96dpi",)
    assert ocr_policy(2024).render_dpi == 350
    assert ocr_policy(2024).quality_flags == ("source_150dpi",)
    assert ocr_policy(2025).render_dpi == 300
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run pytest tests/ingestion/test_extract_ocr.py -q
```

- [ ] **Step 3: 모델 revision lock 검증기를 구현한다.**

`config/models.lock.json`은 PaddleOCR/PaddlePaddle package version, OCR model revision, language `korean`, model 파일별 SHA-256을 기록한다. revision이나 64자리 hash가 없는 lock은 실행 전에 실패한다. `docker/ingestion.Dockerfile`은 build stage에서 locked 한국어 OCR model을 `/opt/models/paddleocr`에 내려받고 모든 hash를 검사한 뒤 final image에 복사한다. final image는 `/opt/venv`와 model cache가 완성된 상태여야 하며 runtime download code path를 비활성화한다. image build가 끝나면 실제 image digest를 build attestation에 기록하고, 실행 시 그 digest를 `IngestionRun`에 전달한다. lock 생성과 image build만 네트워크를 허용하고 운영 수집기는 local cache만 연다.

- [ ] **Step 4: 전체 PDF 페이지 렌더와 OCR adapter를 구현한다.**

PDF 내부 strip 이미지를 직접 넘기지 않고 `page.get_pixmap(dpi=policy.render_dpi, alpha=False)`로 완성 페이지를 렌더한다. OCR 결과를 공통 `RawPage`/line/bbox/confidence 계약으로 변환하며 line reading order는 행 중심 좌표 정렬 후 layout column 규칙을 적용한다.

- [ ] **Step 5: critical field와 low-confidence 격리 규칙을 구현한다.**

2023~2024는 제목·질문·금액·날짜·법령명·조문 중 하나라도 미검수면 사례 파서가 승인할 수 없도록 page flag를 전달한다. line confidence가 정책값 미만이거나 문서번호에 허용되지 않은 문자가 생기면 실제 값 대신 `location_id`, field type, confidence만 review queue에 기록한다.

- [ ] **Step 6: host에서는 dependency-free adapter 단위 테스트만 실행한다.**

```bash
uv run pytest tests/ingestion/test_extract_ocr.py -q
```

Expected: PaddlePaddle을 import하지 않는 fake adapter 테스트가 통과한다.

- [ ] **Step 7: Linux/amd64 builder에서 image와 1페이지 offline smoke를 검증한다.**

```bash
docker version
docker buildx version
mkdir -p artifacts/build artifacts/ocr-smoke
docker buildx build --platform linux/amd64 --load --network default -f docker/ingestion.Dockerfile -t education-admin-ingestion:corpus-v1 .
docker image inspect --format 'SEN_QA_INGESTION_IMAGE_DIGEST={{.Id}}' education-admin-ingestion:corpus-v1 > artifacts/build/ingestion.env
docker run --rm --platform linux/amd64 --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=1g --env-file artifacts/build/ingestion.env -e SEN_QA_SOURCE_ROOT=/sources -v /volume1/education-admin/source:/sources:ro -v "$PWD/artifacts/ocr-smoke:/work/artifacts/ocr-smoke:rw" education-admin-ingestion:corpus-v1 /opt/venv/bin/python -m src.cli extract-ocr --year 2025 --pages 13 --output artifacts/ocr-smoke
```

`SEN_QA_INGESTION_IMAGE_DIGEST`에는 바로 앞에서 기록한 local content digest가 env file로 전달된다. Expected: Docker server architecture가 amd64이고, network가 차단된 container에서 page JSON에 bbox/confidence, `render_dpi=300`, source/render hash, image digest가 기록된다. Docker/buildx가 없으면 host 명령으로 우회하지 않고 Linux builder 준비 작업으로 차단한다.

- [ ] **Step 8: 커밋한다.**

```bash
git add config/models.lock.json docker/ingestion.Dockerfile src/ingestion/extract_ocr.py tests/fixtures/ocr-pages/2023-low-dpi.json tests/fixtures/ocr-pages/2025-mixed-script.json tests/ingestion/test_extract_ocr.py src/cli.py
git commit -m "feat: extract OCR pages with locked models"
```

## Task 6: 정규화, 개인정보 분류, 품질 게이트

**Files:**

- Create: `src/ingestion/normalize.py`
- Create: `src/ingestion/privacy.py`
- Create: `src/ingestion/quality.py`
- Create: `src/ingestion/review.py`
- Create: `tests/ingestion/test_normalize.py`
- Create: `tests/ingestion/test_privacy.py`
- Create: `tests/ingestion/test_quality.py`
- Create: `tests/ingestion/test_review.py`
- Create: `docs/runbooks/manual-review.md`

- [ ] **Step 1: 보존 필드와 개인정보 출력 최소화 테스트를 작성한다.**

```python
# tests/ingestion/test_normalize.py
from src.ingestion.normalize import normalize_text


def test_normalization_preserves_critical_entities() -> None:
    raw = "금 1,502,000원 · 2025. 7. 1. · 제12조제3항\n질문･답변"
    normalized = normalize_text(raw)
    assert "1,502,000원" in normalized
    assert "2025. 7. 1." in normalized
    assert "제12조제3항" in normalized
    assert "질문·답변" in normalized
```

```python
# tests/ingestion/test_privacy.py
from src.ingestion.privacy import scan_text


def test_privacy_report_never_contains_detected_value() -> None:
    finding = scan_text("연락처 010-1234-5678", location_id="case-1:answer")[0]
    assert finding.kind == "phone"
    assert finding.location_id == "case-1:answer"
    assert "010" not in finding.model_dump_json()
```

```python
# tests/ingestion/test_review.py
import pytest


def test_answer_approval_requires_independent_second_reviewer(review_store, critical_case) -> None:
    review_store.mark_needs_review(critical_case.case_id, reason="critical_ocr_fields")
    review_store.verify_critical_fields(critical_case.case_id, reviewer_id="reviewer-a")
    review_store.approve_search(critical_case.case_id, reviewer_id="reviewer-a")
    with pytest.raises(ValueError, match="independent reviewer"):
        review_store.approve_answer(critical_case.case_id, reviewer_id="reviewer-a")
    review_store.approve_answer(critical_case.case_id, reviewer_id="reviewer-b")
    assert review_store.get(critical_case.case_id).review_status == "approved"


def test_critical_fields_mode_verifies_fields_and_approves_search(review_store, critical_case) -> None:
    review_store.run_mode(
        "critical-fields-all",
        cases=[critical_case],
        reviewer_id="reviewer-a",
    )
    reviewed = review_store.get(critical_case.case_id)
    assert reviewed.critical_field_review == "verified"
    assert reviewed.review_status == "search_approved"
    assert reviewed.answer_eligible is False
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run pytest tests/ingestion/test_normalize.py tests/ingestion/test_privacy.py tests/ingestion/test_quality.py tests/ingestion/test_review.py -q
```

- [ ] **Step 3: raw/normalized/corrected 계층을 구현한다.**

NFC, 줄 끝 하이픈, 반복 header/footer, 연속 공백, 구분자만 자동 정규화한다. `Correction`에는 교정자 ID, UTC 시각, 사유 코드, 이전·이후 text hash를 기록하고 원문은 변경하지 않는다.

- [ ] **Step 4: 전수 privacy detector와 정책 결정을 구현한다.**

주민등록번호, 전화, 이메일, 계좌 후보, API token, JWT/PEM, URL credential을 탐지한다. 이름+소속+직책과 감사 준식별자는 별도 분류한다. high-risk match는 `restricted`, 제작진은 `public_credit`, 마스킹된 감사사례는 최소 `anonymized_case`로 판정한다.

- [ ] **Step 5: 품질 게이트를 구현한다.**

필수 질문·답변, source span hash, 1-based 페이지, confidence, critical field review, review lifecycle, PII 정책을 한 번에 검증한다. 실패 결과는 `reason_code`, `case_id/page_id`, count만 포함한 `artifacts/review-queue/{release_id}.jsonl`로 보낸다.

- [ ] **Step 6: 검수 상태 전이와 append-only audit을 구현한다.**

허용 전이는 `machine_extracted → needs_review → search_approved → approved`와 모든 비종료 상태에서 `rejected`뿐이다. 품질 검사기는 모든 후보를 먼저 `needs_review` queue에 넣는다. critical field reviewer가 모든 요구 필드를 hash로 확인한 뒤에만 `search_approved`가 되고, 별도 reviewer가 답변·근거·privacy를 승인해야 `approved`가 된다. CLI는 단건 `review verify-fields`, `review approve-search`, `review approve-answer`, `review reject`, interactive `review run`, 검증된 segment manifest 전용 `review approve-search-batch`, 종료 gate `review assert-ready`를 제공한다. `review run --mode critical-fields-all`은 각 case에서 필드 확인을 마친 즉시 `verify-fields`와 `approve-search`를 하나의 transaction으로 실행해 `search_approved/answer_eligible=false`로 만든다. `answer-and-basis-all`은 반드시 다른 reviewer가 이 상태에서만 실행할 수 있다. batch도 case별 상태 전이 event를 남기며 DB field 직접 수정 명령은 제공하지 않는다. 각 event는 이전·이후 상태, reviewer ID, UTC 시각, reason, reviewed content hash와 batch manifest hash를 append-only `review_events`에 저장한다.

`docs/runbooks/manual-review.md`에는 source를 read-only mount하고, raw/canonical은 ingestion group read-only, review queue와 correction log는 reviewer group만 읽고 쓰도록 NAS ACL을 분리하는 명령과 확인 절차를 기록한다.

- [ ] **Step 7: 상태 우회와 권한 회귀 테스트를 통과시킨다.**

```bash
uv run pytest tests/ingestion/test_normalize.py tests/ingestion/test_privacy.py tests/ingestion/test_quality.py tests/ingestion/test_review.py -q
git add src/ingestion/normalize.py src/ingestion/privacy.py src/ingestion/quality.py src/ingestion/review.py tests/ingestion/test_normalize.py tests/ingestion/test_privacy.py tests/ingestion/test_quality.py tests/ingestion/test_review.py docs/runbooks/manual-review.md src/cli.py
git commit -m "feat: gate corpus quality and privacy"
```

## Task 7: 연도별 사례 경계 파서와 페이지 연속성

**Files:**

- Create: `src/ingestion/parse_common.py`
- Create: `src/ingestion/parse_2020.py`
- Create: `src/ingestion/parse_2021_2022.py`
- Create: `src/ingestion/parse_2023.py`
- Create: `src/ingestion/parse_2024_2025.py`
- Create: `tests/fixtures/page-golden/{2020,2021,2022,2023,2024,2025}/*.json`
- Create: `tests/fixtures/parsed-cases/{2020,2021,2022,2023,2024,2025}/*.expected.json`
- Create: `tests/ingestion/test_parse_2020.py`
- Create: `tests/ingestion/test_parse_2021_2022.py`
- Create: `tests/ingestion/test_parse_2023.py`
- Create: `tests/ingestion/test_parse_2024_2025.py`
- Create: `tests/ingestion/test_page_continuation.py`

각 연도 디렉터리에 아래 파일명을 모두 만든다. Git fixture는 실제 개인정보·저작물 본문을 넣지 않은 합성 layout 데이터이거나 재배포 승인을 받은 최소 redacted crop만 사용한다. 실제 원문 골든 페이지와 사람 전사는 `artifacts/golden-pages/{year}/`에 접근 제한 상태로 둔다.

| 연도 | page fixture와 동일 basename의 parsed expected fixture |
|---|---|
| 2020 | `cover`, `toc`, `first-case`, `continuation`, `audit`, `last-case`, `credits` |
| 2021 | `cover`, `toc`, `first-case`, `continuation`, `audit`, `last-case`, `credits` |
| 2022 | `cover`, `toc`, `first-case`, `continuation`, `audit`, `last-case`, `credits` |
| 2023 | `cover`, `toc`, `first-case`, `continuation`, `audit`, `last-case`, `credits` |
| 2024 | `cover`, `toc`, `first-case`, `continuation`, `audit`, `last-case`, `credits` |
| 2025 | `cover`, `toc`, `first-case`, `continuation`, `audit`, `last-case`, `credits` |

`cover`, `toc`, `credits`의 expected case 배열은 비어 있고 metadata transition만 검증한다. 나머지는 title/question/answer/basis/source spans와 review state를 사람이 확정한 expected JSON으로 비교한다.

- [ ] **Step 1: 공통 상태기계의 실패 테스트를 작성한다.**

```python
# tests/ingestion/test_page_continuation.py
from src.ingestion.parse_common import parse_pages


def test_answer_continues_until_next_case_marker(two_page_continuation) -> None:
    cases = parse_pages(two_page_continuation, edition_year=2022)
    assert len(cases) == 2
    assert "다음 페이지의 참고자료" in cases[0].basis_text
    assert cases[0].source_spans[-1].pdf_page_index == 42
    assert cases[1].source_spans[0].pdf_page_index == 42
```

- [ ] **Step 2: 네 파서가 없어서 실패함을 확인한다.**

```bash
uv run pytest tests/ingestion/test_parse_*.py tests/ingestion/test_page_continuation.py -q
```

- [ ] **Step 3: 공통 parser state와 ambiguity 격리를 구현한다.**

상태는 `domain`, `part`, `subtopic`, `case_no`, `section_role`, open case를 페이지 사이에 유지한다. 다음 사례 번호·편 구분이 나오거나 문장종결+테두리 종료가 동시에 확인될 때만 case를 닫는다. bleed/분할 가능성이 둘 다 있으면 자동 선택하지 않고 `ambiguous_boundary`로 격리한다.

- [ ] **Step 4: 2020 parser를 구현한다.**

19개 편, 번호 상자, 질문 제목, 글머리표 답변, 관련 근거 블록만 허용 marker로 사용한다. 세로 내비게이션과 목차는 case를 생성하지 않는다.

- [ ] **Step 5: 2021~2022 parser를 구현한다.**

대분류·편·소주제, `질문`/`질문1·2`, `답변`/`답변1·2`, 참고자료를 역할별 필드에 누적한다. 상위 목차의 겹친 숫자는 사례 번호로 해석하지 않는다.

- [ ] **Step 6: 2023 parser를 구현한다.**

교육공무직원 레이아웃의 제목·질문·대상·근거·답변·참고자료를 분리하고 모든 critical field를 `needs_review`로 시작한다.

- [ ] **Step 7: 2024~2025 parser를 구현한다.**

카드 테두리, 사례 번호, 제목·상황, 대상, 근거, 답변, 참고자료, 세로 대분류 탭을 좌표와 문자 marker를 함께 사용해 인식한다. 2024는 전 사례 critical review 전까지 격리하고, 2025는 레이아웃 구간별 표본 승인 상태를 기록한다.

- [ ] **Step 8: 골든 fixture와 경계 회귀를 통과시킨다.**

```bash
uv run pytest tests/ingestion/test_parse_*.py tests/ingestion/test_page_continuation.py -q
```

Expected: 42개 합성/redacted layout fixture에서 case boundary precision/recall/F1이 모두 1.00이고 bleed·split이 0건이며 cover/TOC/credits가 case를 생성하지 않는다.

- [ ] **Step 9: 커밋한다.**

```bash
git add src/ingestion/parse_common.py src/ingestion/parse_2020.py src/ingestion/parse_2021_2022.py src/ingestion/parse_2023.py src/ingestion/parse_2024_2025.py tests/fixtures/page-golden tests/fixtures/parsed-cases tests/ingestion/test_parse_2020.py tests/ingestion/test_parse_2021_2022.py tests/ingestion/test_parse_2023.py tests/ingestion/test_parse_2024_2025.py tests/ingestion/test_page_continuation.py
git commit -m "feat: parse yearly question answer layouts"
```

## Task 8: canonical corpus, 법령·관계·역할 기반 청킹

**Files:**

- Modify: `config/models.lock.json`
- Create: `src/corpus/relations.py`
- Create: `src/corpus/chunking.py`
- Create: `src/corpus/storage.py`
- Create: `src/corpus/build.py`
- Create: `tests/corpus/test_relations.py`
- Create: `tests/corpus/test_chunking.py`
- Create: `tests/corpus/test_storage.py`
- Create: `tests/corpus/test_reproducibility.py`

- [ ] **Step 1: 청크 경계와 재현성 실패 테스트를 작성한다.**

```python
# tests/corpus/test_chunking.py
from src.corpus.chunking import build_chunks


def test_chunks_never_cross_case_or_page_boundaries(approved_case) -> None:
    chunks = build_chunks(approved_case)
    assert {chunk.case_id for chunk in chunks} == {approved_case.case_id}
    assert all(
        len({approved_case.source_spans[index].pdf_page_index for index in chunk.source_span_indexes}) == 1
        for chunk in chunks
    )
    assert {chunk.role for chunk in chunks} >= {"question", "answer"}
```

```python
# tests/corpus/test_reproducibility.py
def test_same_inputs_produce_same_canonical_content_hash(build_twice) -> None:
    assert build_twice.first.content_sha256 == build_twice.second.content_sha256
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run pytest tests/corpus/test_relations.py tests/corpus/test_chunking.py tests/corpus/test_storage.py tests/corpus/test_reproducibility.py -q
```

- [ ] **Step 3: chunk token counter가 사용할 `bge-m3` tokenizer를 lock한다.**

network-enabled model-lock 단계에서 `BAAI/bge-m3`의 40자리 commit SHA와 tokenizer/model 파일 SHA-256을 `config/models.lock.json`에 기록한다. 이 단계에서는 image를 만들지 않고 lock과 검증된 staging cache만 생성하며, Task 10에서 같은 파일을 indexer image에 bake한다. `main`, `latest`, 빈 revision을 거부하며 unit test는 lock에 맞춘 작은 fake tokenizer를 사용한다. 실제 tokenizer cache가 lock과 다르면 corpus build 전 실패한다.

- [ ] **Step 4: 역할 기반 chunker를 구현한다.**

질문은 80~250 token, 답변·근거는 250~450 token, 긴 단락만 10~15% overlap으로 나눈다. table은 column header를 각 row에 반복한다. overlap은 같은 case와 같은 source span 안에서만 허용한다. token counter는 embedding model tokenizer revision과 함께 기록한다.

- [ ] **Step 5: 법령 참조와 관계를 구현한다.**

법령명·약칭·조항·시행일·원문 인용을 `LawRef`로 분리하되 자동 최신명 치환을 금지한다. `related`/`duplicate`는 후보 점수만 자동 생성하고, `supersedes`/`conflicts`는 reviewer 승인 정보가 없으면 canonical 관계로 승격하지 않는다.

- [ ] **Step 6: SQLite canonical schema와 deterministic JSONL export를 구현한다.**

`documents`, `cases`, `source_spans`, `chunks`, `law_refs`, `case_relations`, `corrections`, `review_events`, `issued_case_ids`, `ingestion_runs` 테이블을 만들고 foreign key, unique ID, WAL mode를 강제한다. `issued_case_ids`는 active/retired tombstone을 삭제하지 않으며 새 ID 발급 transaction이 항상 이 테이블을 조회한다. JSONL은 ID 정렬, UTF-8, key sort, LF line ending으로 export한다.

- [ ] **Step 7: build transaction과 count/hash 검증을 구현한다.**

한 문서가 격리되면 release 전체를 승인하지 않지만 진단 산출물은 별도 디렉터리에 남긴다. 같은 input SHA, parser version, config로 두 번 빌드한 canonical content hash가 다르면 exit 1이다.

- [ ] **Step 8: 테스트를 통과시키고 커밋한다.**

```bash
uv run pytest tests/corpus -q
git add config/models.lock.json src/corpus/relations.py src/corpus/chunking.py src/corpus/storage.py src/corpus/build.py tests/corpus/test_relations.py tests/corpus/test_chunking.py tests/corpus/test_storage.py tests/corpus/test_reproducibility.py src/cli.py
git commit -m "feat: build reproducible canonical corpus"
```

## Task 9: SQLite FTS5 한글 lexical 색인

**Files:**

- Create: `config/retrieval.toml`
- Create: `src/retrieval/query.py`
- Create: `src/retrieval/lexical.py`
- Create: `tests/retrieval/test_query.py`
- Create: `tests/retrieval/test_lexical.py`

- [ ] **Step 1: 한글 띄어쓰기·정확 숫자 검색 실패 테스트를 작성한다.**

```python
# tests/retrieval/test_lexical.py
def test_spacing_variant_and_exact_article_are_retrieved(lexical_index) -> None:
    hits = lexical_index.search("2단계입찰 제12조제3항 1,502,000원", limit=10)
    assert hits[0].case_id == "senqa-2025-contract-contract-general-1"
    assert "제12조제3항" in hits[0].matched_terms
    assert "1,502,000원" in hits[0].matched_terms
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run pytest tests/retrieval/test_query.py tests/retrieval/test_lexical.py -q
```

- [ ] **Step 3: 질의 정규화와 2·3글자 n-gram을 구현한다.**

NFC와 공백만 정규화하고 숫자, 단위, %, 조·항·호, 법령명, case ID 원형을 별도 exact token으로 보존한다. filter는 query 문자열에 섞지 않고 `years`, `domains`, `case_types`, `access_level` 구조로 유지한다.

- [ ] **Step 4: FTS5 테이블과 가중 BM25를 구현한다.**

`title`, `question`, `law_names`, `exact_tokens`, `char_ngrams`, `body` column을 분리한다. query 전 `search_eligible=1`, 승인 상태, access level filter를 SQL에서 먼저 적용하고 제목·질문·법령·exact token에 더 높은 weight를 둔다.

- [ ] **Step 5: 테스트와 query plan 검사를 통과시킨다.**

```bash
uv run pytest tests/retrieval/test_query.py tests/retrieval/test_lexical.py -q
uv run python -m src.cli inspect-lexical-plan --db artifacts/canonical/current.sqlite3 --query "학교회계 제12조"
```

Expected: full table scan이 아니라 FTS virtual table을 사용하며 restricted chunk가 0건이다.

- [ ] **Step 6: 커밋한다.**

```bash
git add config/retrieval.toml src/retrieval/query.py src/retrieval/lexical.py tests/retrieval/test_query.py tests/retrieval/test_lexical.py
git commit -m "feat: add Korean lexical retrieval"
```

## Task 10: 고정 `bge-m3` 임베딩과 Qdrant release collection

**Files:**

- Modify: `config/models.lock.json`
- Modify: `src/cli.py`
- Create: `src/retrieval/dense.py`
- Create: `tests/retrieval/test_dense.py`
- Create: `docker/indexer.Dockerfile`
- Create: `docker-compose.index.yml`

- [ ] **Step 1: revision과 payload filter 실패 테스트를 작성한다.**

```python
# tests/retrieval/test_dense.py
import pytest

from src.retrieval.dense import DenseEncoder, DenseIndex


def test_dense_index_requires_immutable_model_revision(model_lock_without_revision) -> None:
    with pytest.raises(ValueError, match="immutable revision"):
        DenseEncoder.from_lock(model_lock_without_revision)


def test_restricted_chunk_is_never_upserted(fake_qdrant, restricted_chunk) -> None:
    DenseIndex(fake_qdrant).upsert([restricted_chunk])
    assert fake_qdrant.points == []
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run pytest tests/retrieval/test_dense.py -q
```

- [ ] **Step 3: Task 8에서 고정한 `BAAI/bge-m3` cache를 offline 검증한다.**

`docker/indexer.Dockerfile`은 digest로 고정한 Python base에서 `config/models.lock.json`의 40자리 commit SHA만 내려받아 encoder/tokenizer를 image에 bake하고 파일별 SHA-256을 재검사한다. runtime은 `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `--network none`으로 시작하며 `main`, `latest`, 비어 있는 revision, cache miss를 모두 실패시킨다. tokenizer revision과 encoder revision이 다르면 dense build를 시작하지 않는다.

- [ ] **Step 4: batch encoder와 Qdrant payload를 구현한다.**

normalized embedding을 생성하고 `chunk_id`, `case_id`, `doc_id`, year, domain, part, case type, eligibility, review status, PII class, page/span, corpus/embedding version을 payload에 넣는다. NAS CPU에서 batch size는 설정값으로 두고 OOM 시 자동으로 결과를 숨긴 채 계속하지 말고 release build를 실패시킨다.

- [ ] **Step 5: 버전 collection과 count gate를 구현한다.**

collection 이름은 `{release_id}-bge-m3`다. upsert 완료 뒤 canonical의 eligible chunk 수와 Qdrant point 수, sampled vector hash를 비교한다. 불일치하면 `education-admin-current` alias를 바꾸지 않는다.

- [ ] **Step 6: Qdrant integration 환경 preflight를 실행한다.**

```bash
docker version
docker compose version
```

Expected: Docker server가 사용 가능하다. `docker-compose.index.yml`의 Qdrant image는 digest로 고정하고 memory hard limit을 2GB 이하로 둔다. Docker가 없으면 in-memory 대체로 release 검증을 속이지 않고 integration 작업을 차단한다.

- [ ] **Step 7: indexer image offline smoke와 Qdrant integration을 실행한다.**

```bash
uv run pytest tests/retrieval/test_dense.py -q
mkdir -p artifacts/build
docker buildx build --platform linux/amd64 --load --network default -f docker/indexer.Dockerfile -t education-admin-indexer:corpus-v1 .
docker image inspect --format 'SEN_QA_INDEXER_IMAGE_DIGEST={{.Id}}' education-admin-indexer:corpus-v1 > artifacts/build/indexer.env
docker run --rm --platform linux/amd64 --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --env-file artifacts/build/indexer.env education-admin-indexer:corpus-v1 /opt/venv/bin/python -m src.cli dense-smoke --text "학교회계 제12조"
docker compose -f docker-compose.index.yml up -d qdrant
uv run pytest -m qdrant tests/retrieval/test_dense.py -q
docker compose -f docker-compose.index.yml stop qdrant
```

Expected: network가 없는 read-only indexer에서 고정 모델로 normalized vector가 생성되고 image digest가 기록된다. Qdrant point count는 eligible chunk count와 같고 filter 없는 search method가 코드에 존재하지 않는다.

- [ ] **Step 8: 커밋한다.**

```bash
git add config/models.lock.json src/cli.py src/retrieval/dense.py tests/retrieval/test_dense.py docker/indexer.Dockerfile docker-compose.index.yml
git commit -m "feat: build versioned dense index"
```

## Task 11: Hybrid RRF 검색 계약과 근거 span 반환

**Files:**

- Create: `src/retrieval/fusion.py`
- Create: `src/retrieval/service.py`
- Create: `data/schemas/search-result.schema.json`
- Create: `tests/retrieval/test_fusion.py`
- Create: `tests/retrieval/test_service.py`

- [ ] **Step 1: RRF와 사전 접근통제의 실패 테스트를 작성한다.**

```python
# tests/retrieval/test_fusion.py
import pytest

from src.retrieval.fusion import reciprocal_rank_fusion


def test_rrf_uses_k_60_and_groups_by_parent(lexical_hits, dense_hits) -> None:
    fused = reciprocal_rank_fusion(lexical_hits, dense_hits, k=60, limit=8)
    assert len({hit.case_id for hit in fused}) == len(fused)
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
```

```python
# tests/retrieval/test_service.py
def test_search_returns_evidence_spans_and_never_answer_text(search_service) -> None:
    response = search_service.search("2단계 입찰", access_level="staff")
    assert response.results[0].matched_spans
    assert response.results[0].pdf_page_index >= 1
    assert not hasattr(response, "generated_answer")


def test_policy_filters_are_passed_to_both_backends_before_search(search_service) -> None:
    search_service.search("감사 사례", access_level="staff")
    assert search_service.lexical.last_filter.search_eligible is True
    assert search_service.dense.last_filter.search_eligible is True
    assert search_service.lexical.last_filter.review_statuses == {"search_approved", "approved"}
    assert search_service.dense.last_filter.review_statuses == {"search_approved", "approved"}


def test_answer_context_selector_requires_answer_eligibility(search_service) -> None:
    response = search_service.search("감사 사례", access_level="staff")
    context = search_service.select_answer_context(response, limit=5)
    assert all(item.answer_eligible is True for item in context)
    assert all(item.review_status == "approved" for item in context)
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run pytest tests/retrieval/test_fusion.py tests/retrieval/test_service.py -q
```

- [ ] **Step 3: RRF, parent grouping, exact boost를 구현한다.**

lexical 25, dense 25를 병렬 조회하고 `score += 1/(60+rank)`로 결합한다. 같은 `case_id`는 최고 자식으로 묶으며 정확한 case ID·법령명·조문·금액만 config에 고정된 additive boost를 준다. 최신 연도 자체에는 boost를 주지 않는다.

- [ ] **Step 4: 관계와 무응답 판정을 구현한다.**

승인된 `supersedes`가 있으면 대체 사례를 표시하고 `conflicts`면 양쪽을 함께 반환한다. 무응답은 결합 점수, answer/basis span 존재, 질문 유형별 calibration, review metadata를 reason code로 남긴다. 근거 span이 없으면 `answer_context_eligible=false`다.

- [ ] **Step 5: `SearchResponse` Pydantic 모델과 JSON Schema를 구현한다.**

정규화 질의, filters, corpus/lexical/embedding version, latency breakdown, no-answer 후보와 reason code, 상위 8개 case, matched span, PDF/인쇄 페이지, quality warning, related/supersedes/conflicts ID를 반환한다. 생성 문장과 모델 호출 필드는 넣지 않는다.

- [ ] **Step 6: 계약·필터·결정성 테스트를 통과시킨다.**

```bash
uv run pytest tests/retrieval -q
uv run python -m src.cli export-schemas --output data/schemas
```

- [ ] **Step 7: 커밋한다.**

```bash
git add src/retrieval/fusion.py src/retrieval/service.py data/schemas/search-result.schema.json tests/retrieval/test_fusion.py tests/retrieval/test_service.py src/cli.py
git commit -m "feat: expose grounded hybrid search contract"
```

## Task 12: 200문항 골드셋과 수집·검색 평가

**Files:**

- Create: `data/eval/retrieval-dev.jsonl`
- Create: `data/eval/retrieval-blind.jsonl` (질문·층화 태그만 포함)
- Generate, Git-excluded: `artifacts/eval-private/retrieval-blind-labels.jsonl`
- Create: `src/evaluation/goldset.py`
- Create: `src/evaluation/ingestion_metrics.py`
- Create: `src/evaluation/retrieval_metrics.py`
- Create: `tests/evaluation/test_goldset.py`
- Create: `tests/evaluation/test_ingestion_metrics.py`
- Create: `tests/evaluation/test_retrieval_metrics.py`

- [ ] **Step 1: 층화 수와 blind 누출 방지 실패 테스트를 작성한다.**

```python
# tests/evaluation/test_goldset.py
def test_public_goldset_has_required_size_and_nonsecret_strata(load_public_goldsets) -> None:
    dev, blind = load_public_goldsets()
    all_items = dev + blind
    assert (len(dev), len(blind), len(all_items)) == (140, 60, 200)
    assert all(sum(item.edition_year == year for item in all_items) >= 25 for year in range(2020, 2026))
    assert sum(item.focus in {"law", "article", "amount", "date"} for item in all_items) >= 30
    assert sum(item.low_resolution_ocr for item in all_items) >= 30
    assert sum(item.spacing_or_typo_variant for item in all_items) >= 20
```

- [ ] **Step 2: 빈 골드셋이 실패함을 확인한다.**

```bash
uv run pytest tests/evaluation/test_goldset.py -q
```

- [ ] **Step 3: gold item 계약과 reviewer workflow를 구현한다.**

공개 개발 문항은 질문, 허용 case ID, 필수 page/span, 연도·분야·유형, no-answer, OCR 품질군, focus, 변형 태그, 작성자와 독립 검수자 ID를 가진다. 같은 사람이 작성·승인할 수 없게 validator로 막는다. `retrieval-blind.jsonl`에는 질문 ID·질문·층화 태그만 커밋하고, 허용 case ID·필수 page/span·no-answer 정답은 접근 제한된 `artifacts/eval-private/retrieval-blind-labels.jsonl`에 분리한다. release 평가 명령만 두 파일을 ID로 결합하며 tuning report에는 item-level blind 정답을 출력하지 않는다.

- [ ] **Step 4: 업무전문가가 검증한 140/60 문항을 채운다.**

각 연도 25개 이상, 질의응답·감사, 무응답 30개 이상, 법령/조문/금액/날짜 30개 이상, 연도 간 관련·대체·상충 20개 이상, 2023~2024 OCR 30개 이상, 오타·띄어쓰기 20개 이상 조건을 동시에 충족시킨다. 공개 테스트는 정답을 노출하지 않는 층화를 검사하고, protected-label validator는 개발+blind label을 결합해 무응답 30개 이상과 positive case/page foreign key를 release 환경에서 검사한다.

- [ ] **Step 5: ingestion metric을 구현한다.**

case boundary precision/recall/F1, bleed/split, 필수필드 누락, page anchor, 핵심 entity 오류, 1,502자 잘림, provenance 누락을 계산한다. 출시 통과값은 F1 `1.00`, bleed/split `0`, 누락 `0`, 전체와 blind page anchor `100%`, 핵심 entity 오류 `0`, 1,502자 잘림 `0`, provenance 누락 `0`이다.

- [ ] **Step 6: retrieval metric을 구현한다.**

Recall@10, year별 Recall@10, MRR@10, nDCG@10, evidence span 포함률, no-answer recall, stage별 latency를 기존 substring/lexical/dense/hybrid 각각 계산한다. 2023·2024·2025 OCR 품질군도 별도 slice로 출력한다. hybrid gate는 전체 Recall `>=0.95`, 모든 연도 `>=0.90`, MRR `>=0.75`, nDCG `>=0.80`, span `>=0.98`, no-answer recall `>=0.95`다. 서비스 cold-start 시간은 warm 검색 p95와 분리해 기록한다.

- [ ] **Step 7: evaluator 자체 테스트를 통과시킨다.**

```bash
uv run pytest tests/evaluation -q
```

- [ ] **Step 8: 커밋한다.**

```bash
git add data/eval/retrieval-dev.jsonl data/eval/retrieval-blind.jsonl src/evaluation/goldset.py src/evaluation/ingestion_metrics.py src/evaluation/retrieval_metrics.py tests/evaluation/test_goldset.py tests/evaluation/test_ingestion_metrics.py tests/evaluation/test_retrieval_metrics.py
git commit -m "test: add corpus and retrieval release gates"
```

## Task 13: 레거시 매핑, release 전환, 백업·복구 자동화

**Files:**

- Create: `src/release.py`
- Create: `src/corpus/legacy.py`
- Create: `config/backup-recipients.txt` (public encryption recipients only)
- Create: `config/backup-tools.lock.json`
- Create: `config/storage-policy.toml`
- Create: `docker/backup.Dockerfile`
- Create: `scripts/build-corpus.sh`
- Create: `scripts/build-indexes.sh`
- Create: `scripts/evaluate-release.sh`
- Create: `scripts/verify-release.sh`
- Create: `scripts/backup-release.sh`
- Create: `scripts/restore-release.sh`
- Create: `scripts/promote-release.sh`
- Create: `scripts/verify-storage-permissions.sh`
- Create: `tests/corpus/test_legacy.py`
- Create: `tests/test_release_scripts.py`
- Create: `tests/test_storage_policy.py`
- Create: `docs/runbooks/index-release.md`
- Create: `docs/runbooks/backup-restore.md`

- [ ] **Step 1: 레거시 역매핑과 alias 실패 유지 테스트를 작성한다.**

```python
# tests/corpus/test_legacy.py
def test_every_mapped_legacy_id_has_one_canonical_target(mapping) -> None:
    assert len(mapping) == len({item.legacy_id for item in mapping})
    assert all(item.case_id for item in mapping)
```

```python
# tests/test_release_scripts.py
def test_failed_evaluation_keeps_current_alias(fake_release) -> None:
    previous = fake_release.current_alias
    assert fake_release.promote(
        release_id=fake_release.candidate_release_id,
        metrics={"recall_at_10": 0.94},
    ) is False
    assert fake_release.current_alias == previous


def test_manifest_failure_after_alias_change_rolls_alias_back(fake_release) -> None:
    previous = fake_release.current_alias
    fake_release.fail_next_manifest_replace = True
    assert fake_release.promote(
        release_id=fake_release.candidate_release_id,
        metrics={"all_release_gates": True},
    ) is False
    assert fake_release.current_alias == previous


def test_promotion_requires_matching_verify_and_restore_attestations(fake_release) -> None:
    fake_release.write_verification_attestation(release_id="corpus-a")
    fake_release.write_restore_attestation(release_id="corpus-b")
    assert fake_release.promote(release_id="corpus-a") is False
```

```python
# tests/test_storage_policy.py
def test_service_identities_are_distinct_non_root(storage_policy) -> None:
    assert storage_policy.ingestion_uid > 0
    assert storage_policy.search_uid > 0
    assert storage_policy.evaluator_uid > 0
    assert storage_policy.reviewer_gid > 0
    assert len({storage_policy.ingestion_uid, storage_policy.search_uid, storage_policy.evaluator_uid}) == 3
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run pytest tests/corpus/test_legacy.py tests/test_release_scripts.py -q
```

- [ ] **Step 3: 기존 HTML에서 legacy ID·제목만 읽는 비교기를 구현한다.**

HTML의 내장 body를 canonical source로 저장하지 않는다. `legacy_id`, 제목, 연도, 새 `case_id`, mapping confidence, 검수 상태만 `artifacts/reports/{release_id}/legacy-map.jsonl`에 기록한다. 2,418건 중 무매핑·다중매핑 수와 잘린 108건 복구 상태, 비어 있던 laws 1,565건의 새 파싱 상태를 요약한다.

- [ ] **Step 4: build/evaluate/verify script를 구현한다.**

모든 script는 `set -euo pipefail`을 사용하며 공통 `SEN_QA_RELEASE_ID` 외에는 책임에 맞는 최소 환경만 요구한다. `build-corpus`는 source+artifact root, `build-indexes`는 artifact root, `evaluate-release`는 artifact+private-eval root, `verify-release`는 source+artifact+private-eval root, `backup-release`는 artifact+private-eval root, `restore-release`는 artifact+private-eval root와 backup path를 요구한다. `src.release start-release`는 규칙에 맞는 release ID와 source/artifact/private-eval root만 들어 있는 Git-excluded `active-release.env`를 생성한다. `build-corpus.sh`는 고정 ingestion image를 `--network none`, source volume `:ro`, release artifact subdirectory만 `:rw`로 실행하고 source verify → secret scan → extract → parse → privacy/quality → candidate canonical과 review queue 생성에서 반드시 멈춘다. 사람 검수와 `review assert-ready` checkpoint 전에는 lexical/dense 색인을 만들지 않는다. checkpoint 뒤 `build-indexes.sh`가 approved/search-approved corpus로 lexical+dense를 만들고, `evaluate-release.sh`가 200문항을 평가하며, `verify-release.sh`가 gate attestation을 만든다. backup/restore attestation까지 확인한 `promote-release.sh`만 current를 전환한다. 자동화는 이 명령 집합과 명시적 사람 승인 checkpoint를 포함하며, 어느 단계든 실패하면 promotion에 도달하지 않는다. script는 executable bit에 의존하지 않고 항상 `bash scripts/<name>.sh`로 호출한다.

- [ ] **Step 5: 원자적 index promotion을 구현한다.**

`verify-release.sh`는 canonical·index count/hash, privacy, ingestion, retrieval gate를 검사해 signed verification attestation만 만들고 promotion을 수행하지 않는다. backup과 격리 restore가 성공해 restore attestation까지 생성된 뒤에만 `scripts/promote-release.sh`를 실행한다. promote는 새 filesystem manifest를 `pending-{release_id}.json`으로 fsync한 다음 Qdrant alias를 예상 old collection과 비교하는 CAS 방식으로 원자 교체하고 `os.replace()`로 pending manifest를 `current.json`에 바꾼다. 두 저장소를 하나의 분산 transaction이라고 표현하지 않는다. manifest 교체가 실패하면 alias를 old collection으로 rollback하고, startup reconciliation이 alias와 manifest release ID 불일치를 감지해 검색 서비스를 readiness 실패 상태로 둔다. verification 또는 restore attestation이 없거나 다른 release ID면 promote가 실패한다.

- [ ] **Step 6: backup·restore runbook과 검증을 구현한다.**

SQLite online backup, Qdrant snapshot, source manifest, model lock, evaluation report를 release bundle로 묶고 SHA-256 manifest를 생성한다. `config/backup-tools.lock.json`은 `age` version과 Linux/amd64 binary SHA-256을 고정하고, `docker/backup.Dockerfile`은 digest 고정 base에 검증된 binary만 넣는다. backup/restore script는 이 image를 `--network none --read-only`로 실행하며 NAS host의 임의 `age` binary에 의존하지 않는다. private blind label은 `config/backup-recipients.txt`의 public age recipient로 별도 암호화해 bundle에 넣고 평문을 복사하지 않는다. 복호화 identity는 Git·NAS·bundle 밖의 관리자 보관소에 둔다. 같은 NAS 경로는 백업으로 인정하지 않고 외장 디스크·다른 NAS·원격 저장소 중 하나의 target을 필수 인자로 받는다. `scripts/restore-release.sh`는 bundle hash를 검증하고 `SEN_QA_BACKUP_IDENTITY_FILE`로 blind label을 별도 `SEN_QA_PRIVATE_EVAL_ROOT/restore/{release_id}`에 복원하며 permission `0700`을 확인한다. 그 뒤 별도 SQLite path와 Qdrant collection namespace에서 200문항 평가를 실행하고 restore attestation을 생성한다. snapshot restore에는 source volume을 요구하지 않고, 원본부터 재생성하는 검증만 별도 `build-corpus`에서 source를 요구한다. 복원본은 명시적 promotion 전에는 current alias를 건드리지 않는다.

`config/storage-policy.toml`에는 NAS에서 실제 확인한 non-root ingestion/search/evaluator UID와 reviewer GID를 기록한다. `scripts/verify-storage-permissions.sh`는 해당 identity로 최소 container probe를 실행해 ingestion은 source 읽기 가능·쓰기 불가, search는 approved canonical 읽기 가능·쓰기 불가·review queue와 private-eval 읽기 불가, reviewer는 review queue 읽기/쓰기 가능·private-eval 읽기 불가, evaluator만 active private-eval label 읽기 가능·쓰기 불가인지 검사한다. 예상 밖 성공이나 실패가 하나라도 있으면 release를 차단한다.

- [ ] **Step 7: script 단위 테스트와 shell syntax를 통과시킨다.**

```bash
uv run pytest tests/corpus/test_legacy.py tests/test_release_scripts.py tests/test_storage_policy.py -q
docker buildx build --platform linux/amd64 --load --network default -f docker/backup.Dockerfile -t education-admin-backup:corpus-v1 .
docker run --rm --platform linux/amd64 --network none --read-only education-admin-backup:corpus-v1 age --version
bash -n scripts/build-corpus.sh scripts/build-indexes.sh scripts/evaluate-release.sh scripts/verify-release.sh scripts/backup-release.sh scripts/restore-release.sh scripts/promote-release.sh scripts/verify-storage-permissions.sh
```

- [ ] **Step 8: 커밋한다.**

```bash
git add src/release.py src/corpus/legacy.py config/backup-recipients.txt config/backup-tools.lock.json config/storage-policy.toml docker/backup.Dockerfile scripts/build-corpus.sh scripts/build-indexes.sh scripts/evaluate-release.sh scripts/verify-release.sh scripts/backup-release.sh scripts/restore-release.sh scripts/promote-release.sh scripts/verify-storage-permissions.sh tests/corpus/test_legacy.py tests/test_release_scripts.py tests/test_storage_policy.py docs/runbooks/index-release.md docs/runbooks/backup-restore.md src/cli.py
git commit -m "feat: automate corpus release and recovery"
```

## Task 14: PDF 6권 전체 수집, 사람 검수, NAS 성능 출시 게이트

**Files:**

- Generate, Git-excluded: `artifacts/raw-pages/`, `artifacts/parsed-cases/`, `artifacts/canonical/`, `artifacts/review-queue/`, `artifacts/indexes/`
- Create: `docs/reports/corpus-v1-summary.md`
- Create: `docs/reports/retrieval-v1-summary.md`
- Create: `docs/reports/privacy-v1-summary.md`
- Create: `docs/reports/release-v1-checklist.md`

- [ ] **Step 1: 전체 6권을 같은 release ID로 추출·파싱한다.**

```bash
uv run python -m src.cli start-release --source-root /volume1/education-admin/source --artifact-root /volume1/education-admin/artifacts --private-eval-root /volume1/education-admin/private-eval --env-file /volume1/education-admin/artifacts/active-release.env
set -a
. /volume1/education-admin/artifacts/active-release.env
set +a
bash scripts/verify-storage-permissions.sh
bash scripts/build-corpus.sh
```

Expected: source read-only, canonical search read-only, review queue reviewer-only probe가 먼저 통과한다. 그 뒤 manifest 6권·총 1,877페이지가 처리되고, 실패·격리 수가 문서별로 명시된다. 격리가 0이 아니면 release 상태는 `review_required`이지 성공이 아니다.

- [ ] **Step 2: 연도·품질군별 승인 정책으로 review queue를 닫는다.**

```bash
set -a
. /volume1/education-admin/artifacts/active-release.env
set +a
uv run python -m src.cli review list --release-id "$SEN_QA_RELEASE_ID" --status needs_review
uv run python -m src.cli review run --release-id "$SEN_QA_RELEASE_ID" --years 2020,2021,2022 --mode native-layout-sample --sample-rate 0.10 --minimum-per-layout 5 --reviewer-id "$(id -un)"
uv run python -m src.cli review approve-search-batch --release-id "$SEN_QA_RELEASE_ID" --selector native-layout-sample-passed --reason-code native_golden_and_sample_pass --reviewer-id "$(id -un)"
uv run python -m src.cli review run --release-id "$SEN_QA_RELEASE_ID" --years 2023,2024 --mode critical-fields-all --reviewer-id "$(id -un)"
uv run python -m src.cli review run --release-id "$SEN_QA_RELEASE_ID" --years 2025 --mode layout-sample --sample-rate 0.10 --minimum-per-layout 5 --reviewer-id "$(id -un)"
uv run python -m src.cli review approve-search-batch --release-id "$SEN_QA_RELEASE_ID" --selector 2025-zero-error-layouts --reason-code layout_sample_zero_error --reviewer-id "$(id -un)"
```

2020~2022는 42개 golden fixture 통과 후 각 layout segment의 `max(5, 10%)`를 첫 검수자가 확인하고 segment manifest hash 단위로 검색만 승인한다. 2023~2024는 제목·질문·금액·날짜·법령명·조문 전 건을 첫 검수자가 확인한다. 2025는 각 segment의 `max(5, 10%)`가 무오류일 때 검색만 batch 승인하고, 오류 한 건이면 같은 segment 전 건을 `review run --mode critical-fields-all`로 전환한다. `public_credit`, `restricted`, 실패 사례는 승인 대상에서 제외하고 명시적으로 rejected/restricted 상태를 유지한다.

독립된 둘째 NAS 계정이 새 shell에서 active env를 다시 읽고, 답변 근거로 사용할 2023~2024 전 건과 별도 지정 사례를 다음 명령으로 승인한다.

```bash
set -a
. /volume1/education-admin/artifacts/active-release.env
set +a
uv run python -m src.cli review run --release-id "$SEN_QA_RELEASE_ID" --years 2023,2024 --mode answer-and-basis-all --reviewer-id "$(id -un)"
uv run python -m src.cli review assert-ready --release-id "$SEN_QA_RELEASE_ID" --require-search-decision-for-all-quality-passed --require-approved-or-rejected-for-critical --max-unresolved-required 0
```

`review run`은 매 case에서 source bbox, candidate content hash, reason code를 보여주고 확인 전에는 다음으로 진행하지 않는다. 첫째와 둘째 reviewer ID가 같으면 명령이 실패한다. 모든 교정과 상태 전이는 append-only audit trail을 남기며, 2020~2022와 오류 없는 2025 미표본은 `search_approved/answer_eligible=false`, 두 사람 검수를 마친 사례만 `approved/answer_eligible=true`가 된다.

- [ ] **Step 3: 개인정보·재배포 gate를 승인한다.**

high-risk detector match를 모두 해소하고, 레거시 말뭉치에서 확인된 79개 사례의 제작진 크레딧군을 회귀 표본으로 포함해 모든 `public_credit`이 일반 색인에 0건인지 확인한다. source별 재배포 근거가 없으면 `staff/unverified`를 유지한다. 요약 보고서에는 값이 아니라 kind/count/location ID만 넣는다.

- [ ] **Step 4: canonical과 두 색인을 빌드한다.**

```bash
set -a
. /volume1/education-admin/artifacts/active-release.env
set +a
bash scripts/build-indexes.sh
```

Expected: canonical eligible chunk 수와 FTS/Qdrant count가 일치하고, model/corpus/index release ID가 같다.

- [ ] **Step 5: lexical·dense·hybrid 200문항 평가를 실행한다.**

```bash
set -a
. /volume1/education-admin/artifacts/active-release.env
set +a
bash scripts/evaluate-release.sh
```

Expected: 기존 substring 검색, lexical, dense, hybrid 네 방식을 같은 200문항으로 비교하고 hybrid가 Task 12의 전체·연도별·근거 span·무응답 기준을 모두 통과한다. 보고서는 2023, 2024, 2025 OCR 품질군을 따로 보여준다. 하나라도 미달하면 alias를 바꾸지 않는다.

- [ ] **Step 6: DS925+ warm 성능을 24GB RAM 환경에서 측정한다.**

service 시작부터 model/index 준비 완료까지 cold-start 시간을 별도 기록한다. 그 뒤 질의 200개를 warm-up 후 반복해 normalization, lexical, query embedding, dense, fusion, parent expansion을 각각 측정한다. 검색 전체 p95가 3초 이하여야 한다. 인덱싱은 야간 일회성 job으로 실행하고 온라인 검색과 동시에 돌리지 않는다. Qdrant memory limit은 2GB 이하에서 시작해 실제 RSS로 조정한다.

- [ ] **Step 7: release 검증과 외부 백업 복원 연습을 수행한다.**

```bash
set -a
. /volume1/education-admin/artifacts/active-release.env
set +a
bash scripts/verify-release.sh
bash scripts/backup-release.sh /volumeUSB1/usbshare/education-admin-backup
SEN_QA_BACKUP_IDENTITY_FILE=/secure/offline/education-admin-backup.agekey bash scripts/restore-release.sh /volumeUSB1/usbshare/education-admin-backup
bash scripts/promote-release.sh
```

Expected: blind 60문항의 page anchor가 100%이고 provenance 누락이 0건이며 설계의 12개 완료 기준이 모두 pass한다. verify는 verification attestation만 만들고 current를 바꾸지 않는다. 별도 restore namespace에서 hash·count·200문항 평가가 재현돼 restore attestation이 생긴 뒤에만 promote가 current alias를 바꾼다.

- [ ] **Step 8: 실제 값만 담은 네 요약 보고서를 작성한다.**

`corpus-v1-summary.md`에는 문서/사례/청크/격리 수와 provenance 지표, `retrieval-v1-summary.md`에는 기존 substring·lexical·dense·hybrid 네 방식과 연도별 지표·p95, `privacy-v1-summary.md`에는 비식별 count와 정책, `release-v1-checklist.md`에는 12개 gate와 승인자·시각·release ID를 기록한다. 원문 개인정보, secret, NAS credential은 기록하지 않는다.

- [ ] **Step 9: 보고서와 코드 전체 검증 후 커밋한다.**

```bash
uv run pytest -q
uv run ruff check src tests
uv run mypy src
git add docs/reports/corpus-v1-summary.md docs/reports/retrieval-v1-summary.md docs/reports/privacy-v1-summary.md docs/reports/release-v1-checklist.md
git commit -m "docs: record corpus v1 release evidence"
```

## Final Acceptance Checklist

설계서 22.2의 완료 기준과 아래 12개 항목은 순서대로 1:1 대응한다.

- [ ] 원본 6권의 파일명·SHA-256·페이지 수·공개 상태가 manifest에 고정됐다.
- [ ] 모든 canonical case가 문서, 1-based PDF 페이지, 인쇄 페이지, bbox, text hash로 역추적된다.
- [ ] 골드 페이지 사례 경계 F1 1.00, bleed/split 0건, 질문·답변 누락 0건이다.
- [ ] 골드셋의 금액·날짜·법령명·조문 오류가 0건이다.
- [ ] 새 canonical 말뭉치의 1,502자 인위적 잘림이 0건이다.
- [ ] 개인정보·크레딧·준식별정보 정책이 적용되고 restricted/public_credit의 일반 색인 유입이 0건이다.
- [ ] 같은 200문항에서 기존 substring·lexical·dense·hybrid를 비교했고 hybrid가 모든 검색 출시 기준을 통과했다.
- [ ] hybrid Recall@10 전체 95% 이상, 각 연도 90% 이상이다.
- [ ] blind 60문항의 PDF/인쇄 페이지와 bbox anchor 정확도가 100%다.
- [ ] 실패 release가 current alias를 바꾸지 않고 이전 release로 복구 가능하다.
- [ ] 문서화된 자동 release 명령 집합으로 원본 검증부터 canonical·lexical·dense 색인과 평가까지 재생성된다.
- [ ] 외부 공개 후보 문서는 `redistribution_status=approved`와 `access_level=public`을 모두 만족하며, 그 외 자료는 직원 제한 상태다.

추가 안전·운영 게이트:

- [ ] 1,877개 모든 PDF 페이지의 raw page 결과 또는 명시적 격리 사유가 있다.
- [ ] 개발 140문항과 접근 제한 blind label 60문항의 층화 조건과 독립 검수를 통과했다.
- [ ] MRR@10 0.75, nDCG@10 0.80, evidence span 98%, no-answer recall 95% 이상이고 provenance 누락이 0건이다.
- [ ] DS925+ 24GB RAM의 warm 검색 p95가 3초 이하이며 cold-start와 ingestion 자원 분리가 기록됐다.
- [ ] SQLite backup, Qdrant snapshot, manifest, model lock, 평가 보고서가 다른 물리 장애 도메인에 복제되고 restore 검증됐다.
- [ ] 기존 Gemini 키 폐기·사용량 확인 증적 전에는 공개 웹·AI 호출을 배포하지 않는다.

## 다음 하위 프로젝트

이 계획을 모두 통과한 뒤에만 다음 설계를 시작한다: 직원 검색 UI/FastAPI → 직원 SSO/RBAC/감사로그 → 사용자가 명시적으로 요청하는 근거 기반 Platform API 답변 → 기존 계산기 모듈화 → NAS 운영 배포. 개인 ChatGPT OAuth는 정식 직원 서비스의 기본 인증수단으로 사용하지 않는다.
