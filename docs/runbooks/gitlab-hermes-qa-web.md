# GitLab Webhook × Hermes QA 웹 운영

## 고정 구조

공개 웹의 질문은 Cloudflare Worker가 project `428`의 confidential issue와
`/hermes ask senqa-...` note로 기록한다. GitLab Note Hook은
`https://gitlab-agent.h19h19.com/webhooks/gitlab-qa`로 전송한다. 터널은 이 경로만
맥미니 `127.0.0.1:8645`의 QA 브리지로 보낸다. 기존
`/webhooks/gitlab-agent`와 Hermes status/review 경로는 변경하지 않는다.

브리지는 bot actor, project ID/path, confidential flag, request ID를 확인하고
고정된 read-only 프리뷰 검색기를 먼저 실행한다. Hermes `hermes2`는
`-t context_engine --ignore-rules`로 실행되므로 terminal, web, browser, MCP를 사용할 수
없다. 답변은 별도 response token으로 같은 confidential issue에만 게시된다.

## 비밀 분리

- Worker GitLab token: issue/note 생성과 note 조회 전용, Cloudflare Secret
- Mac response token: issue note 생성 전용, macOS Keychain/launch wrapper
- GitLab Webhook secret: QA hook 검증 전용, GitLab과 Mac Keychain/launch wrapper
- Turnstile secret: Cloudflare Secret

토큰을 저장소, Worker vars, launchd plist, 로그, 질문 issue 본문에 넣지 않는다.
Worker token과 Mac response token을 같은 값으로 사용하지 않는다.

## 배포 전 게이트

```bash
uv run pytest -q \
  tests/test_gitlab_qa_bridge.py \
  tests/test_hermes_gitlab_qa_webhook.py \
  tests/test_hermes_gitlab_qa_delivery.py
npm --prefix web/qa-worker test
node --check web/qa-worker/src/worker.js
node --check web/qa-worker/public/app.js
```

Cloudflare Worker에는 `QUESTIONS` KV, static assets, exact public origin을 바인딩한다.
`GITLAB_TOKEN`과 `TURNSTILE_SECRET`은 `wrangler secret put`으로만 입력한다.
배포된 Worker에서 `/api/config`가 site key만 반환하고 비밀을 반환하지 않는지
확인한다.

## 맥미니 실행

브리지의 배포 사본과 filter/search/config는 소유자 전용 regular file이어야 한다.
launch wrapper가 Keychain에서 두 비밀을 읽고 다음 환경만 넘긴 뒤 실행한다.

```text
SENQA_GITLAB_WEBHOOK_SECRET
SENQA_GITLAB_RESPONSE_TOKEN
SENQA_GITLAB_BOT_USERNAME
SENQA_GITLAB_QA_FILTER
SENQA_HERMES_COMMAND
SENQA_PREVIEW_SEARCH_COMMAND
SENQA_PREVIEW_SEARCH_CONFIG
```

실행 파일은 `scripts/hermes-gitlab-qa-bridge.py`이며 bind 주소/포트는
`127.0.0.1:8645`로 고정이다. 외부 인터페이스나 LAN 주소에 직접 bind하지 않는다.

## 실제 왕복 검증

1. 별도 test 질문을 공개 웹에서 한 번 제출한다.
2. GitLab에 confidential issue와 정확한 ask note가 생성됐는지 확인한다.
3. Webhook delivery가 2xx이고 같은 request ID의 answer note가 한 개뿐인지 확인한다.
4. 웹 poll이 완료되면 AI 답변, 관련 사례 질의·답변, 사례 ID·연도·PDF 쪽만
   표시하는지 확인한다. GitLab, Webhook, Hermes, RAG, project, bot, model이나
   내부 상태 코드·필드명은 화면과 답변에 표시하면 안 된다.
5. 근거 사례가 없을 때는 AI 호출 없이 다음 문구만 표시하고 관련 사례와 근거 영역을
   숨기는지 확인한다.

   ```text
   등록된 사례집에서 이 질문과 관련된 내용을 찾지 못했습니다. 다른 표현이나 핵심어로 다시 검색해 주세요.
   ```

6. 근거 사례는 있지만 AI 답변을 안전하게 검증하지 못했을 때는 사례 목록을 보존하고
   다음 문구를 표시하는지 확인한다.

   ```text
   답변을 정리하지 못했습니다. 관련 사례는 아래 목록에서 직접 확인해 주세요.
   ```

7. GitLab UI에서 issue가 public으로 보이지 않는지 비로그인 세션으로 확인한다.

실패 시 기존 status/review route나 운영 alias를 변경하지 않는다. 새 QA path route와
Worker route만 비활성화하고 confidential test issue를 보존해 감사한다.
