# GitLab Webhook × Hermes QA 웹 구현 계획

1. GitLab QA Note Hook의 exact filter, RAG 선행 검색, 값 비노출 계약을
   RED→GREEN으로 구현한다.
2. GitLab QA 전용 로컬 브리지에서 Hermes `hermes2`를 무도구 1회 호출하고,
   결과를 confidential GitLab issue note로 보내는 외부 SHA 고정 설치기를 구현한다.
3. Cloudflare Worker에 confidential issue 생성, `/hermes ask` note 생성, KV 폴링
   권한, Turnstile/rate-limit 경계를 구현한다.
4. 승인된 이미지 개념을 따라 responsive 질문·pending·answer 웹 UI를
   구현한다.
5. 합성 GitLab/Cloudflare/Hermes E2E, 모바일·데스크톱 브라우저, 전체
   pytest/Ruff/mypy/public-repo gate를 검증한다.
6. 별도 project token과 Worker secret이 준비된 뒤 Cloudflare Worker와
   맥미니 브리지, 터널의 새 path route만 배포하고 질문 1건을 실제 왕복 검증한다.
