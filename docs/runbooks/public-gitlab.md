# Public GitLab operations

이 문서는 공공 GitLab을 공개 원본 저장소로 운영하고 GitHub를 미러로 유지하는 절차다. 공개 저장소와 CI에는 코드, 합성 fixture, 테스트 결과와 해시만 허용한다.

## Project contract

- project: `h19h19/education-admin-rag`
- visibility: public
- Auto DevOps: disabled
- default branch: `main`, protected
- Runner tag: `public-safe`
- clone: `GIT_DEPTH=0`
- deployment: disabled
- GitHub mirror: `h19h29-design/education-admin-rag`, public
- legacy GitHub remote: `weplebong/education-admin-launcher`, private and retained read-only

## Public boundary

원본 PDF, OCR output, canonical DB, review DB, Qdrant snapshot, private labels, backup, key, internal host/path are prohibited.

Push 전 다음을 실행한다.

```bash
./scripts/verify-public-repo.sh
./scripts/scan-secrets.sh
git status --porcelain=v1
```

첫 두 명령은 통과해야 하고 worktree 출력은 비어 있어야 한다. 삭제·폐기된 과거 Google API key fingerprint는 승인된 baseline과 정확히 일치할 때만 허용한다.

## Project bootstrap

빈 프로젝트를 생성하며 README, license, `.gitignore`를 GitLab에서 자동 생성하지 않는다. 첫 push 후 Auto DevOps를 끄고 `main`을 보호한다. Maintainer만 push·merge할 수 있고 force push는 금지한다.

## Remotes

기존 GitHub remote를 보존하면서 GitLab을 원본 remote로 만든다.

```bash
git remote rename origin github
git remote add origin https://gitlab.aigov.go.kr/h19h19/education-admin-rag.git
git push --set-upstream origin HEAD:main
git push origin --tags
```

push 후 exact SHA를 비교한다.

```bash
test "$(git rev-parse HEAD)" = \
  "$(git ls-remote origin refs/heads/main | awk '{print $1}')"
```

## GitHub push mirror

GitLab push mirror를 우선 사용한다. GitHub credential은 GitLab secret store에만 입력하고 token을 Git URL, Git config, 저장소 파일 또는 CI log에 넣지 않는다.

미러 기능이 제공되지 않으면 credential 없는 dual-push URL을 사용한다.

```bash
git remote set-url --push origin \
  https://gitlab.aigov.go.kr/h19h19/education-admin-rag.git
git remote set-url --add --push origin \
  https://github.com/h19h29-design/education-admin-rag.git
git remote get-url --all --push origin
```

두 remote의 `main`은 항상 같은 SHA여야 한다.

기존 `weplebong/education-admin-launcher` 저장소는 관리자 권한이 없어 공개 전환하지 않는다. 공개 미러는 관리 권한이 있는 `h19h29-design/education-admin-rag`를 사용하며, 기존 저장소는 `github-legacy` remote로 보존한다.

## Public-safe CI

`.gitlab-ci.yml`은 `public-policy`, `quality`, `security`, `docs` 네 작업만 정의한다. 모든 작업은 `public-safe` tag를 요구한다. artifact, cache, deploy, Docker push와 NAS 환경변수는 정의하지 않는다.

full-history secret 검사를 위해 `GIT_DEPTH`는 `0`이다. 공개 로그에 원문, source filename, 내부 경로 또는 예외 입력값을 출력하지 않는다.

## Runner 없음

승인된 Runner가 없으면 pipeline은 pending 상태로 둔다. NAS를 fallback Runner로 등록하지 않는다. AI정부실험실 VM이 배정된 뒤 OS, CPU, RAM, disk, network, Docker executor와 log visibility를 검증하고 `public-safe` 또는 `public-safe-docker` tag를 부여한다.

Runner가 첫 pipeline을 성공하기 전에는 NAS 연산이 이전됐다고 기록하지 않는다.

### GitHub-hosted public-safe fallback

GitLab Runner가 준비될 때까지 공개 GitHub 미러의 `.github/workflows/public-safe.yml`이 동일한 `ci-public-gates.sh` 계약을 실행한다. GitLab은 계속 원본 저장소이고 GitHub Actions는 공개 코드·합성 fixture의 테스트 계산만 대신한다.

- 권한은 `contents: read`만 허용한다.
- checkout은 full history이며 공식 action도 immutable commit SHA로 고정한다.
- secret, artifact, cache, deploy, Docker push와 NAS 환경변수를 사용하지 않는다.
- 원본 PDF, OCR output, canonical/review DB, Qdrant snapshot은 업로드하지 않는다.
- hosted workflow 성공은 공개 테스트 부하가 NAS에서 분리됐다는 증거일 뿐, 민감 운영 계산이나 Docker/SBOM 배포가 이전됐다는 뜻은 아니다.

## NAS pull-only

NAS는 검토된 tag를 pull하고 commit과 release checksum을 검증한다. GitLab에는 NAS write credential을 제공하지 않는다. GitLab webhook도 NAS 쓰기 권한을 갖지 않는다.

서명된 release metadata를 GitLab Release에 올릴 때는 본문, private label, DB, snapshot, 내부 경로를 제외한다.

## Read-back checklist

```text
visibility=public
github_visibility=public
auto_devops=disabled
main_protected=1
force_push_allowed=0
private_data_uploaded=0
nas_registered_as_runner=0
```

완료 주장은 GitLab 설정 read-back과 두 remote의 SHA 비교가 끝난 뒤에만 bootstrap report와 Notion에 반영한다.

## Rollback

- mirror divergence가 생기면 mirror를 중지하고 GitLab `main`을 원본으로 유지한다.
- 공개 정보 노출이 발견되면 pipeline을 취소하고 artifact를 만료한 뒤 원인을 수정한다.
- 신뢰할 수 없는 Runner는 즉시 unregister한다.
- NAS는 마지막 검증 release로 복구한다.
- Rollback 중에도 GitLab에는 NAS write credential을 제공하지 않는다.
