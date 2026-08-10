# Hermes GitLab Webhook·Cloudflare Tunnel 설계

## 목표

공개 GitLab 프로젝트의 명시적 댓글 명령만 맥미니 Hermes Agent로 전달한다.
Cloudflare Tunnel은 외부 공개 포트를 열지 않고 Hermes의 Webhook 전용
loopback listener로만 연결한다. Hermes는 기존 ChatGPT 계정의 OpenAI Codex
OAuth를 사용한다.

## 범위

- 대상 프로젝트는 `h19h19/education-admin-rag`와 GitLab project ID `428`로
  고정한다.
- 1단계 명령은 `/hermes status`와 MR 댓글의 `/hermes review`뿐이다.
- Webhook 세션에는 `web`만 제공하고 `no_mcp` sentinel로 모든 MCP 상속을
  차단한다.
- MR·이슈 제목, 설명, 댓글의 임의 문자열은 agent prompt로 전달하지 않는다.
- 결과는 기존 Hermes Telegram home channel로 전달한다.
- GitLab, Cloudflare, Hermes에는 NAS credential, 원본 PDF, OCR JSONL,
  canonical/review DB, Qdrant snapshot을 제공하지 않는다.

## 구조

```text
GitLab Note Hook
  -> HTTPS gitlab-agent.h19h19.com
  -> webhook-id를 X-Request-ID로 변환
  -> Cloudflare named tunnel
  -> 127.0.0.1:8644/webhooks/gitlab-agent
  -> GitLab secret-token 검증
  -> project/user/command allowlist filter
  -> Hermes read-only webhook session
  -> OpenAI Codex OAuth
  -> Telegram 결과 알림
```

Cloudflare는 DNS와 Tunnel transport만 담당한다. Hermes dashboard, API server,
SSH, NAS, RAG artifact path는 Tunnel ingress에 포함하지 않는다. 알 수 없는
hostname과 route는 각각 Cloudflare `http_status:404`와 Hermes 404로 종료한다.

## 신뢰 경계

1. TLS는 Cloudflare edge에서 종료되고 Tunnel은 맥미니에서 outbound 연결만
   생성한다.
2. GitLab은 `X-Gitlab-Token`을 보내고 Hermes는 timing-safe exact match로
   검사한다. 현재 GitLab 인스턴스와 Hermes가 Standard Webhooks signing
   token을 함께 지원하게 되면 HMAC 방식으로 이전한다.
3. Cloudflare는 정확한 webhook hostname/path에서만 GitLab의 재시도 간 동일한
   `webhook-id`를 Hermes가 지원하는 `X-Request-ID`로 복사한다. Hermes의
   idempotency cache는 같은 delivery를 한 번만 실행한다.
4. `scripts/hermes-gitlab-filter.py`는 project ID/path, 사용자, note type,
   exact 명령, 대상 IID를 재검사한다.
5. filter 출력은 고정 project metadata와 정수 IID만 포함한다. 다른 입력값은
   모두 `[SILENT]`로 무시한다.
6. Webhook platform toolset은 read-only로 고정한다. 인증된 payload도 신뢰할
   수 없는 입력으로 취급한다.

## 코드 쓰기 자동화의 별도 gate

`/hermes fix`는 1단계에 포함하지 않는다. 다음 조건을 모두 충족한 뒤 별도
설계와 검토를 거쳐 활성화한다.

- Docker/VM/별도 OS account 중 하나로 host filesystem을 격리한다.
- 공개 repository 전용 credential만 제공한다.
- 매 실행은 새 `codex/agent-*` worktree와 branch에서 시작한다.
- main 직접 push, force push, merge, deploy, secret/NAS 접근을 금지한다.
- 테스트 성공 뒤 Merge Request만 만들고 사람만 merge한다.

## 비용

현재 트래픽 규모에서는 Cloudflare Zero Trust Free named tunnel을 사용한다.
Worker는 사용하지 않고 Free request-header transform rule만 사용한다. 도메인
연간 갱신료와 기존 ChatGPT 구독 외의
월 고정비를 추가하지 않는다.

## Rollback

1. GitLab Webhook을 disable한다.
2. Cloudflare hostname route를 제거한다.
3. Cloudflare request-header transform rule을 제거한다.
4. Hermes dynamic subscription을 제거한다.
5. Hermes Webhook platform을 disable하고 gateway를 재시작한다.
6. 기존 Telegram, GitLab origin, GitHub mirror, NAS release는 변경하지 않는다.
