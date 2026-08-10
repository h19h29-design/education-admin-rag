# Hermes GitLab Webhook·Cloudflare Tunnel 구현 계획

> 승인된 설계:
> `docs/superpowers/specs/2026-08-10-hermes-gitlab-webhook-design.md`

## Task 1: 값 최소화 GitLab payload filter

**Files**

- Create: `scripts/hermes-gitlab-filter.py`
- Create: `tests/test_hermes_gitlab_webhook.py`

- [x] 프로젝트·사용자·명령·대상 IID allowlist 실패 테스트를 먼저 만든다.
- [x] production 파일 부재 RED를 확인한다.
- [x] 64 KiB bounded JSON reader와 exact-type filter를 구현한다.
- [x] 유효 출력이 고정 metadata만 포함하고 hostile text를 반사하지 않음을 검증한다.

## Task 2: 운영 runbook과 local Hermes route

**Files**

- Create: `docs/runbooks/hermes-gitlab-webhook.md`

- [x] OpenAI Codex OAuth가 로그인 상태인지 값 없이 확인한다.
- [x] 실제 gateway profile의 Webhook toolset을 `web,no_mcp`로 제한하고
  resolver 결과가 정확히 `web`인지 확인한다.
- [x] Webhook listener를 `127.0.0.1:8644`, 64 KiB, 분당 6회로 설정한다.
- [x] route secret을 Keychain과 mode 0600 Hermes subscription에만 저장한다.
- [x] 정상·잘못된 secret·잘못된 프로젝트·중복 delivery를 local/external에서 검증한다.

## Task 3: Cloudflare와 GitLab external wiring

- [x] `gitlab-agent.h19h19.com` 전용 named tunnel을 만든다.
- [x] ingress는 Hermes Webhook route와 catch-all 404만 허용한다.
- [x] GitLab Note events Webhook에 같은 secret을 설정한다.
- [x] GitLab Note delivery가 HTTPS 202이고 Telegram delivery가 오류 없이 끝남을 확인한다.
- [x] Cloudflare, GitLab, 로컬 증적에 secret·본문·내부 경로를 포함하지 않는다.
- [x] GitLab `webhook-id`를 exact route에서 `X-Request-ID`로 변환하고 재전송이
  `200 duplicate`인지 확인한다.

## Task 4: 증적·공개 저장소·미러

- [x] Notion 프로젝트 페이지에 RAG code-ready와 release 미완료를 구분해 기록한다.
- [ ] 전체 pytest, Ruff, strict mypy, public policy와 current-tree secret scan을 실행한다.
- [ ] 명시 파일만 커밋하고 GitLab main에 push한다.
- [ ] GitHub mirror main이 같은 SHA인지 확인한다.

## 완료 조건

- GitLab에서 허용 사용자가 exact 명령을 남길 때만 Hermes가 한 번 실행된다.
- Webhook 세션은 host terminal/file/Computer Use에 접근할 수 없다.
- public route는 Webhook 외에 404이며 ingress port를 직접 개방하지 않는다.
- RAG 운영 release 완료로 오해할 표현을 사용하지 않는다.
