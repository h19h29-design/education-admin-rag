# GitLab Webhook × Hermes 교육행정 QA 웹 설계

## 목표

공개 질문 웹페이지에서 질문을 접수하고, GitLab Note Hook이 맥미니의
저장소 밖에서 설정된 로컬 실행 프로필로 답변하며,
결과를 다시 웹페이지에 보여준다. GitLab은 질문 큐와 Webhook, 감사
이력의 권위 원천이다. Cloudflare는 공개 웹/API와 이미 구성된
GitLab-to-Mac Tunnel만 담당한다.

## 사용자 흐름

1. 사용자가 공개 웹 페이지에서 최대 1,000자의 한국어 질문을 입력한다.
2. Cloudflare Worker가 Turnstile, 요청 크기, 속도 제한을 확인한 뒤 공개
   코드 저장소의 **confidential GitLab issue**를 만든다.
3. Worker가 그 issue에 `/hermes ask` 명령 note와 질문을 추가한다.
4. GitLab Note Hook이 기존 `gitlab-agent.h19h19.com` Cloudflare Tunnel의
   새 `/webhooks/gitlab-qa` 경로를 통해 맥미니의 로컬 QA 브리지에 도착한다.
5. 전용 필터가 프로젝트, bot actor, confidential issue, 명령, 질문 크기를
   재검증하고 고정된 로컬 RAG 검색기를 직접 실행한다.
6. 브리지는 설정된 로컬 실행 프로필을 도구 0개인 `context_engine` allowlist와
   `--ignore-rules`로 1회 호출한다. 실행기는 봉인된 검색 근거만 받아 답변한다.
7. 브리지의 독립 GitLab delivery 경계가 답변을 동일 confidential issue의
   machine-marked note로 게시한다.
8. Worker가 임의의 폴링 토큰을 검증하고 해당 note를 정규화해 웹에
   pending, complete, failed 상태로 보여준다.

## 보안 경계

- 웹페이지와 코드는 공개하되 질문·답변 issue는 confidential로 유지한다.
- Worker 및 맥미니용 GitLab 토큰은 서로 분리한 project access token이며
  Cloudflare Secret과 macOS Keychain에만 저장한다.
- Worker는 GitLab 토큰, Webhook secret, 맥미니 경로, RAG 데이터를 반환하지
  않는다.
- 폴링 토큰은 256-bit random이고 KV에는 해시만 저장한다.
- 필터는 정확한 project ID/path, 사전 승인된 bot username, confidential flag,
  issue IID, command prefix, request ID를 검증한다.
- 공개 질문이 Hermes의 terminal, web, browser, MCP를 열지 않는다. 검색은
  필터가 인증된 read-only CLI로 선행하고 Hermes에는 결과 JSON만 주입한다.
- 결과에는 항상 `unreviewed_incomplete_preview`,
  `production_eligible=false`, case ID, 연도, PDF 페이지가 포함된다.
- 개인정보 입력 금지 안내를 표시하고 질문과 답변은 공개 GitLab
  화면에서 노출되지 않는다.

## 웹 UI

시각 기준은
`docs/design-assets/gitlab-hermes-qa-concept.png`(
SHA-256 `7c3af740ff963fbb3fadba06c58d3c9d5e4b3b3a4ee969240b3620f7a4983601`)이다.

- 참신한 흰색 배경, 딥 네이비 타이포, 저채도 블루 포커스, 앨버 경고
- 왼쪽 최근 질문 rail, 중앙 질문 composer, pending 상태, 답변·근거 표
- desktop에서는 320px rail, mobile에서는 rail을 접고 단일 column
- 키보드 focus, screen-reader status, reduced motion, 1,000자 계수기
- 과도한 card grid, gradient, hero 일러스트, 추상적 dashboard 요소는 사용하지 않는다.

## 배포 경계

첫 배포는 새 호스트에서 실시한다. 기존
`gitlab-agent.h19h19.com/webhooks/gitlab-agent`와 status/review subscription은 변경하지
않고 터널에 `/webhooks/gitlab-qa` → `127.0.0.1:8645` 경로만 추가한다. 운영 alias, canonical RAG alias, NAS,
원본 자료는 변경하지 않는다.
