# 공공 GitLab·GitHub 미러·공개 안전 CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 공공 GitLab을 공개 원본 저장소로 만들고 GitHub 자동 미러, 공개 안전 CI, Runner 준비 계약과 NAS pull-only 운영 경계를 배포 직전 상태까지 구축한다.

**Architecture:** GitLab은 코드·협업·검증 메타데이터의 공개 원본이며 GitHub는 push mirror다. CI는 `public-safe` 태그 Runner에서 코드·합성 fixture만 처리하고 아티팩트를 만들지 않으며, 원본 PDF·OCR·canonical DB·Qdrant·검토 데이터는 NAS에 남는다. Runner가 없을 때 파이프라인은 안전하게 pending 상태를 유지하고 NAS를 fallback Runner로 사용하지 않는다.

**Tech Stack:** Git, GitLab CE, GitHub, Bash 3.2+, Python 3.11, uv, pytest, Ruff, strict mypy, Gitleaks 8.30.1, Notion MCP

## Global Constraints

- 공개 GitLab 경로는 `https://gitlab.aigov.go.kr/h19h19/education-admin-rag`다.
- GitHub 미러는 `https://github.com/weplebong/education-admin-launcher`이며 공개로 전환한다.
- GitLab은 공개 원본이고 GitHub는 자동 push mirror다.
- 원본 PDF, OCR JSONL, canonical SQLite, ReviewStore DB, Qdrant snapshot, private evaluation, backup, key는 GitLab·GitHub·CI 로그·CI artifact에 넣지 않는다.
- Auto DevOps를 끄고 명시적 `.gitlab-ci.yml`만 사용한다.
- 공개 CI는 `public-safe` 태그가 있는 보호 Runner에서만 실행한다.
- full-history secret scan을 위해 `GIT_DEPTH: "0"`을 사용한다.
- 삭제·폐기 확인된 과거 Google API key fingerprint는 기존 승인 baseline으로만 허용하고 새 secret은 허용하지 않는다.
- GitLab이 NAS에 쓰거나 배포하지 않는다. NAS가 검증된 release를 수동 승인 후 pull한다.
- CI 로그와 오류는 본문, 비밀값, 내부 경로를 출력하지 않는다.
- 외부 설정 변경 전 대상 프로젝트·브랜치·미러 URL을 읽기 전용으로 재확인한다.
- 각 로컬 변경은 TDD, `git diff --check`, scoped Ruff/mypy와 관련 pytest를 통과한 뒤 커밋한다.

---

## File Structure

- `.gitlab-ci.yml`: Auto DevOps를 대체하는 공개 안전 pipeline 정의
- `scripts/verify-public-repo.sh`: tracked path가 공개 허용 경계를 넘지 않는지 값 없이 검사
- `scripts/ci-public-gates.sh`: GitLab Runner가 실행할 policy/quality/security/docs 진입점
- `tests/test_public_repository_policy.py`: 공개 금지 파일·경로와 sanitized failure 계약
- `tests/test_gitlab_public_ci.py`: pipeline 변수·Runner tag·금지 동작·shell syntax 계약
- `docs/runbooks/public-gitlab.md`: 프로젝트, mirror, Runner, NAS pull-only 운영 절차
- `docs/reports/public-gitlab-bootstrap.md`: project ID, visibility, HEAD, mirror, Runner 상태의 비식별 증적
- `docs/superpowers/specs/2026-08-09-public-gitlab-hybrid-design.md`: 승인된 설계 원본
- Notion `교육행정 RAG 프로젝트 현황·배포 로드맵`: 사람용 상태 대시보드

---

### Task 1: 공개 저장소 경계 검사

**Files:**
- Create: `scripts/verify-public-repo.sh`
- Create: `tests/test_public_repository_policy.py`

**Interfaces:**
- Consumes: Git index from `git ls-files -z`
- Produces: `./scripts/verify-public-repo.sh` with success output `public_repo_policy=pass tracked_files=<count>` and fixed failure `public_repo_policy=blocked class=<class>`

- [ ] **Step 1: Write the failing tests**

```python
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "scripts" / "verify-public-repo.sh"


def _run(tmp_path: Path, tracked_path: str) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy2(POLICY, repo / "verify-public-repo.sh")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    target = repo / tracked_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("synthetic\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    return subprocess.run(
        ["bash", "verify-public-repo.sh"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def test_public_repository_policy_accepts_source(tmp_path: Path) -> None:
    result = _run(tmp_path, "src/example.py")
    assert result.returncode == 0
    assert "public_repo_policy=pass" in result.stdout


def test_public_repository_policy_rejects_pdf_without_echo(tmp_path: Path) -> None:
    result = _run(tmp_path, "source-name.pdf")
    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert "class=document" in combined
    assert "source-name" not in combined


def test_public_repository_policy_rejects_canonical_db_without_echo(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "artifacts/private-name.sqlite3")
    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert "class=private-artifact" in combined
    assert "private-name" not in combined
```

- [ ] **Step 2: Run tests to verify RED**

Run: `uv run pytest tests/test_public_repository_policy.py -q`

Expected: FAIL because `scripts/verify-public-repo.sh` does not exist.

- [ ] **Step 3: Add the minimal policy script**

```bash
#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  printf '%s\n' 'public_repo_policy=blocked class=repository' >&2
  exit 2
}
cd "$repo_root"

tracked_count=0
blocked_class=""
while IFS= read -r -d '' tracked_path; do
  tracked_count=$((tracked_count + 1))
  case "$tracked_path" in
    artifacts/*|private/*|data/raw/*|data/ocr/*|*/raw-pages/*)
      blocked_class="private-artifact"
      break
      ;;
    *.pdf|*.sqlite|*.sqlite3|*.db|*.key|*.pem|*.p12|*.pfx)
      blocked_class="document"
      break
      ;;
  esac
done < <(git ls-files -z)

if [[ -n "$blocked_class" ]]; then
  printf 'public_repo_policy=blocked class=%s\n' "$blocked_class" >&2
  exit 2
fi

printf 'public_repo_policy=pass tracked_files=%s\n' "$tracked_count"
```

- [ ] **Step 4: Run focused verification**

Run:

```bash
chmod +x scripts/verify-public-repo.sh
uv run pytest tests/test_public_repository_policy.py -q
bash -n scripts/verify-public-repo.sh
./scripts/verify-public-repo.sh
uv run ruff check tests/test_public_repository_policy.py
uv run ruff format --check tests/test_public_repository_policy.py
git diff --check
```

Expected: all commands PASS and repository policy prints `public_repo_policy=pass`.

- [ ] **Step 5: Commit**

```bash
git add scripts/verify-public-repo.sh tests/test_public_repository_policy.py
git commit -m "feat: enforce public repository boundary"
```

---

### Task 2: 공개 안전 GitLab CI

**Files:**
- Create: `.gitlab-ci.yml`
- Create: `scripts/ci-public-gates.sh`
- Create: `tests/test_gitlab_public_ci.py`

**Interfaces:**
- Consumes: Task 1 `./scripts/verify-public-repo.sh`, existing `scripts/scan-secrets.sh`, `uv.lock`
- Produces: four `public-safe` jobs named `public-policy`, `quality`, `security`, `docs`

- [ ] **Step 1: Write the failing CI contract tests**

```python
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".gitlab-ci.yml"
ENTRYPOINT = ROOT / "scripts" / "ci-public-gates.sh"


def test_gitlab_pipeline_is_full_history_public_safe() -> None:
    text = CI.read_text(encoding="utf-8")
    assert 'GIT_DEPTH: "0"' in text
    assert 'AUTO_DEVOPS_DISABLED: "1"' in text
    assert "public-safe" in text
    assert "public-policy:" in text
    assert "quality:" in text
    assert "security:" in text
    assert "docs:" in text


def test_gitlab_pipeline_cannot_deploy_or_publish_artifacts() -> None:
    text = CI.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "artifacts:" not in lowered
    assert "docker push" not in lowered
    assert "deploy" not in lowered
    assert "sen_qa_" not in lowered


def test_public_gate_entrypoint_is_valid_shell() -> None:
    subprocess.run(["bash", "-n", str(ENTRYPOINT)], check=True)


def test_public_gate_rejects_unknown_mode_without_input_echo() -> None:
    marker = "PRIVATE_MODE_SENTINEL"
    result = subprocess.run(
        ["bash", str(ENTRYPOINT), marker],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 2
    assert combined == "public_ci_gate=invalid\n"
    assert marker not in combined
```

- [ ] **Step 2: Run tests to verify RED**

Run: `uv run pytest tests/test_gitlab_public_ci.py -q`

Expected: FAIL because `.gitlab-ci.yml` and `scripts/ci-public-gates.sh` do not exist.

- [ ] **Step 3: Add the fixed CI entrypoint**

```bash
#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root"

case "${1:-}" in
  policy)
    ./scripts/verify-public-repo.sh
    ;;
  quality)
    uv sync --locked --group dev
    uv run pytest -q
    uv run ruff check src tests
    uv run ruff format --check src
    uv run mypy --strict --explicit-package-bases src
    uv lock --check --offline
    ;;
  security)
    ./scripts/verify-public-repo.sh
    ./scripts/scan-secrets.sh
    ;;
  docs)
    uv sync --locked --group dev
    uv run pytest tests/test_release_shell_scripts.py tests/test_gitlab_public_ci.py -q
    ;;
  *)
    printf '%s\n' 'public_ci_gate=invalid' >&2
    exit 2
    ;;
esac
```

- [ ] **Step 4: Add the explicit GitLab pipeline**

```yaml
workflow:
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
    - if: '$CI_COMMIT_BRANCH'
    - if: '$CI_COMMIT_TAG'

stages:
  - verify

default:
  tags:
    - public-safe
  interruptible: true

variables:
  GIT_DEPTH: "0"
  AUTO_DEVOPS_DISABLED: "1"
  UV_FROZEN: "1"
  PIP_DISABLE_PIP_VERSION_CHECK: "1"

public-policy:
  stage: verify
  script:
    - ./scripts/ci-public-gates.sh policy

quality:
  stage: verify
  script:
    - ./scripts/ci-public-gates.sh quality

security:
  stage: verify
  script:
    - ./scripts/ci-public-gates.sh security

docs:
  stage: verify
  script:
    - ./scripts/ci-public-gates.sh docs
```

- [ ] **Step 5: Run focused and static verification**

Run:

```bash
chmod +x scripts/ci-public-gates.sh
uv run pytest tests/test_gitlab_public_ci.py tests/test_public_repository_policy.py -q
bash -n scripts/ci-public-gates.sh
./scripts/ci-public-gates.sh policy
uv run ruff check tests/test_gitlab_public_ci.py tests/test_public_repository_policy.py
uv run ruff format --check tests/test_gitlab_public_ci.py tests/test_public_repository_policy.py
git diff --check
```

Expected: all commands PASS. No job defines artifacts, deployment, NAS variables, or an untagged Runner path.

- [ ] **Step 6: Commit**

```bash
git add .gitlab-ci.yml scripts/ci-public-gates.sh tests/test_gitlab_public_ci.py
git commit -m "ci: add public-safe GitLab gates"
```

---

### Task 3: GitLab 운영 runbook과 bootstrap evidence

**Files:**
- Create: `docs/runbooks/public-gitlab.md`
- Create: `docs/reports/public-gitlab-bootstrap.md`
- Modify: `tests/test_gitlab_public_ci.py`

**Interfaces:**
- Consumes: approved design and Task 2 CI job names
- Produces: operator checklist and value-free bootstrap attestation

- [ ] **Step 1: Add failing documentation contract tests**

Append:

```python
RUNBOOK = ROOT / "docs" / "runbooks" / "public-gitlab.md"
REPORT = ROOT / "docs" / "reports" / "public-gitlab-bootstrap.md"


def test_public_gitlab_runbook_preserves_private_boundary() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for phrase in (
        "Auto DevOps",
        "public-safe",
        "GIT_DEPTH",
        "push mirror",
        "NAS pull-only",
        "원본 PDF",
        "Runner 없음",
        "Rollback",
    ):
        assert phrase in text


def test_bootstrap_report_is_metadata_only() -> None:
    text = REPORT.read_text(encoding="utf-8")
    for key in (
        "project_created=",
        "project_visibility=",
        "runner_available=",
        "container_registry_enabled=",
        "private_data_uploaded=",
    ):
        assert key in text
    assert "/volume" not in text
    assert "PRIVATE" not in text
```

- [ ] **Step 2: Run tests to verify RED**

Run: `uv run pytest tests/test_gitlab_public_ci.py -q`

Expected: FAIL because the runbook and report do not exist.

- [ ] **Step 3: Write the runbook**

The runbook must contain these exact sections and commands:

```markdown
# Public GitLab operations

## Project contract
- project: h19h19/education-admin-rag
- visibility: public
- Auto DevOps: disabled
- default branch: main, protected
- Runner tag: public-safe
- clone: GIT_DEPTH=0

## Public boundary
원본 PDF, OCR output, canonical DB, review DB, Qdrant snapshot, private labels,
backup, key, internal host/path are prohibited.

## Remotes
git remote rename origin github
git remote add origin https://gitlab.aigov.go.kr/h19h19/education-admin-rag.git
git push --set-upstream origin HEAD:main
git push origin --tags

## Mirror
Use GitLab push mirror to GitHub. Keep credentials in the GitLab secret store.
Never put a token in a URL committed to Git or in a CI log.

## Runner 없음
Pipelines remain pending until an approved public-safe Runner exists. NAS is not
registered as a fallback Runner.

## NAS pull-only
NAS pulls a reviewed tag and verifies the commit and release checksums. GitLab
does not receive NAS write credentials.

## Rollback
Disable the mirror on divergence. Cancel pipelines and expire public artifacts
on disclosure. Unregister an untrusted Runner. Return NAS to the last verified
release; GitLab still has no NAS write credential.
```

- [ ] **Step 4: Write the initial metadata-only report**

```markdown
# Public GitLab bootstrap evidence

project_path=h19h19/education-admin-rag
project_created=0
project_visibility=unverified
auto_devops_enabled=unverified
default_branch=unverified
pipeline_definition=.gitlab-ci.yml
runner_available=0
container_registry_enabled=unverified
github_mirror_target=weplebong/education-admin-launcher
private_data_uploaded=0
```

- [ ] **Step 5: Verify and commit**

Run:

```bash
uv run pytest tests/test_gitlab_public_ci.py -q
uv run ruff check tests/test_gitlab_public_ci.py
uv run ruff format --check tests/test_gitlab_public_ci.py
git diff --check
git add docs/runbooks/public-gitlab.md docs/reports/public-gitlab-bootstrap.md tests/test_gitlab_public_ci.py
git commit -m "docs: define public GitLab operations"
```

Expected: focused tests and static checks PASS.

---

### Task 4: 공개 GitLab 프로젝트와 원본 push

**Files:**
- Modify external GitLab project settings
- Modify local Git remotes
- Modify: `docs/reports/public-gitlab-bootstrap.md`

**Interfaces:**
- Consumes: current clean branch, Task 1 public policy, Task 2 pipeline
- Produces: public GitLab `main` pointing to the exact local HEAD

- [ ] **Step 1: Resolve and verify exact external targets**

Run:

```bash
git status --porcelain=v1
git rev-parse HEAD
git merge-base --is-ancestor main HEAD
./scripts/verify-public-repo.sh
./scripts/scan-secrets.sh
git remote -v
```

Expected: clean worktree, current branch is a descendant of `main`, public policy PASS, secret gate PASS, and only the existing GitHub remote is present.

- [ ] **Step 2: Create the project in the logged-in GitLab UI**

Create a blank project with these exact settings:

```text
Project name: education-admin-rag
Project slug: education-admin-rag
Namespace: h19h19
Visibility: Public
Initialize repository with README: Off
```

Expected: project URL is exactly `https://gitlab.aigov.go.kr/h19h19/education-admin-rag`.

- [ ] **Step 3: Rename remotes and push exact history**

Run:

```bash
git remote rename origin github
git remote add origin https://gitlab.aigov.go.kr/h19h19/education-admin-rag.git
git push --set-upstream origin HEAD:main
git push origin --tags
```

Expected: pushes succeed without force and GitLab `refs/heads/main` equals local `HEAD`.

- [ ] **Step 4: Disable implicit deployment and protect the branch**

In project settings:

```text
Auto DevOps: Disabled
Default branch after first push: main
Allowed to merge main: Maintainers
Allowed to push main: Maintainers
Force push: Disabled
```

Expected: no automatic deployment configuration exists.

- [ ] **Step 5: Read back the remote state**

Run:

```bash
test "$(git rev-parse HEAD)" = "$(git ls-remote origin refs/heads/main | awk '{print $1}')"
git ls-remote --tags origin >/dev/null
```

Expected: commit equality check exits 0.

- [ ] **Step 6: Validate the pipeline definition in GitLab**

Use **Build → Pipeline editor → Validate** for the exact `main` version of `.gitlab-ci.yml`.

Expected: GitLab reports valid configuration with `public-policy`, `quality`, `security`, and `docs`. Creating a pipeline may leave all jobs pending because no matching Runner exists; it must not start Auto DevOps or a deployment job.

- [ ] **Step 7: Record the actual metadata and commit**

Update `docs/reports/public-gitlab-bootstrap.md` only with project ID, public URL, exact HEAD SHA, Auto DevOps state, branch protection state, Runner count, Container Registry state, and `private_data_uploaded=0`. Do not add credentials, internal paths, user email, or source filenames.

Run:

```bash
git diff --check
git add docs/reports/public-gitlab-bootstrap.md
git commit -m "docs: attest public GitLab bootstrap"
git push origin HEAD:main
```

Expected: the evidence commit is visible on GitLab `main`.

---

### Task 5: GitHub automatic push mirror

**Files:**
- Modify external GitLab repository mirror settings
- Modify: `docs/reports/public-gitlab-bootstrap.md`

**Interfaces:**
- Consumes: GitLab `main`, authenticated GitHub CLI credential held outside Git
- Produces: GitHub `main` and tags matching GitLab without a repository credential in code or logs

- [ ] **Step 1: Verify GitHub ownership and fast-forward safety**

Run:

```bash
gh repo view weplebong/education-admin-launcher --json nameWithOwner,visibility,defaultBranchRef,viewerPermission
git fetch github main
git merge-base --is-ancestor github/main HEAD
```

Expected: repository is reachable, `viewerPermission` is `WRITE` or stronger, and GitHub `main` is an ancestor of current `HEAD`.

- [ ] **Step 2: Make the GitHub mirror public**

Run:

```bash
gh repo edit weplebong/education-admin-launcher \
  --visibility public --accept-visibility-change-consequences
gh repo view weplebong/education-admin-launcher --json visibility
```

Expected: the read-back visibility is `PUBLIC`. GitLab will publish the same reviewed tree, so this does not expand the approved content set.

- [ ] **Step 3: Configure GitLab push mirror without printing the token**

Copy the existing authenticated GitHub token directly to the system clipboard without terminal output:

```bash
gh auth token | pbcopy
```

In GitLab **Settings → Repository → Mirroring repositories**, configure:

```text
Git repository URL: https://github.com/weplebong/education-admin-launcher.git
Mirror direction: Push
Authentication: Password
Username: authenticated GitHub account
Password: paste from clipboard
Mirror only protected branches: Disabled
Keep divergent refs: Disabled
```

Clear the clipboard immediately:

```bash
printf '' | pbcopy
```

Expected: mirror state becomes successfully updated and no token appears in terminal, Git remote URLs, browser URL, or repository files.

If the GitLab edition does not expose push mirroring, configure an explicit dual-push fallback:

```bash
git remote set-url --push origin https://gitlab.aigov.go.kr/h19h19/education-admin-rag.git
git remote set-url --add --push origin https://github.com/weplebong/education-admin-launcher.git
git remote get-url --all --push origin
```

Expected fallback output contains exactly the GitLab and GitHub repository URLs and no credential. Record `mirror_mode=dual-push` instead of `mirror_mode=gitlab-push` in the evidence report.

- [ ] **Step 4: Trigger and verify the mirror**

Push a metadata-only commit or use GitLab's update mirror control, then run:

```bash
git fetch origin main
git fetch github main
test "$(git rev-parse origin/main)" = "$(git rev-parse github/main)"
```

Expected: both remote-tracking branches have the same SHA.

- [ ] **Step 5: Record mirror evidence and commit**

Append only:

```text
github_push_mirror_enabled=1
github_main_matches_gitlab=1
github_visibility=public
mirror_credentials_committed=0
```

Run:

```bash
git diff --check
git add docs/reports/public-gitlab-bootstrap.md
git commit -m "docs: attest GitHub push mirror"
git push origin HEAD:main
```

Expected: GitLab push mirror advances GitHub to the new evidence commit.

---

### Task 6: Runner readiness, project work items, and Notion status

**Files:**
- Modify external GitLab Work Items/Milestone
- Modify Notion page `교육행정 RAG 프로젝트 현황·배포 로드맵`
- Modify: `docs/reports/public-gitlab-bootstrap.md`

**Interfaces:**
- Consumes: verified absence of instance/group/project Runner
- Produces: explicit Runner acquisition work item and an accurate public status; no claim that compute has moved

- [ ] **Step 1: Create a public milestone and work items**

Create milestone `v0.1-public-release` and these work items:

```text
공개 안전 GitLab Runner 신청 및 등록
첫 public-safe pipeline 실행과 로그 검토
Docker build·SBOM용 public-safe-docker Runner 검증
GitLab Release와 NAS pull-only 검증
```

Each work item must state that original PDFs, OCR output, canonical DB, Qdrant snapshots, review data, and credentials are prohibited from CI.

- [ ] **Step 2: Verify Runner state without registering NAS**

In **Settings → CI/CD → Runners**, record:

```text
project_runner_count=0
group_runner_count=0
instance_runner_count=0
required_runner_tag=public-safe
nas_registered_as_runner=0
```

Expected: pipeline jobs may remain pending and the report makes no compute-offload claim.

- [ ] **Step 3: Update the Notion dashboard**

Update the existing page to:

- mark public project, Auto DevOps, branch protection, full-history push, and GitHub mirror complete;
- leave Runner and first CI execution incomplete;
- add GitLab project and milestone links;
- keep `NAS 부하 절감` in blocked status until a Runner job succeeds;
- append a dated changelog with GitLab/GitHub matching HEAD only.

- [ ] **Step 4: Commit the updated metadata report**

Run:

```bash
git diff --check
git add docs/reports/public-gitlab-bootstrap.md
git commit -m "docs: record Runner readiness"
git push origin HEAD:main
```

Expected: GitLab and GitHub mirror converge to the commit.

---

### Task 7: Final pre-deployment verification

**Files:**
- Verify all modified files and external settings
- Modify Notion changelog only if every completed claim has fresh evidence

**Interfaces:**
- Consumes: Tasks 1–6
- Produces: pre-deployment handoff with explicit Runner blocker and verified public/private boundary

- [ ] **Step 1: Run the full local gate**

Run:

```bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src
uv run mypy --strict --explicit-package-bases src
uv lock --check --offline
./scripts/verify-public-repo.sh
./scripts/scan-secrets.sh
git diff --check
```

Expected: all gates PASS. The secret gate may report only fingerprints already present in the approved revoked-secret baseline.

- [ ] **Step 2: Verify exact remote convergence**

Run:

```bash
git fetch origin main
git fetch github main
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
test "$(git rev-parse HEAD)" = "$(git rev-parse github/main)"
git status --porcelain=v1
```

Expected: local, GitLab, and GitHub SHAs match and the worktree is clean.

- [ ] **Step 3: Verify public project controls**

Read back and record:

```text
visibility=public
github_visibility=public
auto_devops=disabled
main_protected=1
force_push_allowed=0
push_mirror=healthy
runner_available=0
container_registry_enabled=0
```

Expected: no implicit deployment path and no unverified Runner claim.

- [ ] **Step 4: Final Notion update**

Mark Tasks 1–2 and GitLab/GitHub setup complete. Keep `Runner 확보`, `첫 원격 pipeline`, and `실제 NAS 연산 이전` unchecked. Add the fresh test count and exact converged commit SHA.

- [ ] **Step 5: Handoff**

Report:

```text
Public GitLab: ready
GitHub mirror: healthy
Public-safe CI definition: ready
Runner execution: blocked until approved Runner exists
NAS compute offload: not started
Sensitive data uploaded: no
Production deployment: not performed
```

Do not claim deployment or NAS load reduction before an approved Runner completes the first `public-safe` pipeline.

---

## Self-Review Checklist

- [ ] Every public/private boundary in the design is enforced by Task 1 or Task 2.
- [ ] Auto DevOps disablement, branch protection, project visibility, mirror and Runner state have read-back steps.
- [ ] No step prints a GitHub or GitLab token.
- [ ] The plan does not register NAS as a Runner or give GitLab NAS credentials.
- [ ] GitLab/GitHub convergence is checked by exact commit SHA.
- [ ] Runner absence is represented as a blocker, not as completed compute offload.
- [ ] Notion claims are updated only after corresponding evidence exists.
