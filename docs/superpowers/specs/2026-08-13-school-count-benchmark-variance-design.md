# 서울 교육기관 학교 수 기준 모집단 차이 설계

## 목적

2026-08-13 운영 원천 수집은 NEIS 서울(B10) 1,415행과 유치원알리미 공시차수 `20261` 706행을 정상 수집했지만, 서울시교육청의 2026-03-10 잠정 학교 수 표와 모집단 정의·기준일이 달라 후보 생성 전에 차단되었다. 이 설계는 그 차이를 임의 허용 오차로 완화하지 않고, 검토된 원천 범주·건수·기준일·공식 통계 차이를 하나의 hash-pinned 관리자 정책으로 고정한다.

정책이 정확히 일치할 때만 사람 검토용 후보를 생성한다. 후보 생성은 승인이나 `current.json` 변경을 뜻하지 않으며, 기존 review digest 기반 사람 승인 절차를 그대로 거친다.

## 근거와 현재 차단 상태

서울시교육청의 2026-03-10 잠정 표는 다음 수를 제시한다.

| 공식 통계 범주 | 잠정 수 |
| --- | ---: |
| 유치원 | 724 |
| 초등학교 | 609 |
| 중학교 | 390 |
| 고등학교 | 319 |
| 특수학교 | 32 |
| 기타(각종학교 17 + 고등기술학교 1) | 18 |

근거는 [서울교육소식 기사](https://enews.sen.go.kr/news/view.do?bbsSn=191455&step1=3&step2=1)와 그 첨부 원문 `20260608075519432.png`다. 검토된 첨부 SHA-256은 `6279b1bc08a593c96b119220ecbfc6cc4884d7e64125a1705db508afeee15e70`이다. 이 표는 4월 1일 기준 공식 통계가 8월 말 확정되기 전의 잠정치다.

같은 날 운영 수집에서 확인한 NEIS 원본 학교종류 분포는 다음과 같다.

| NEIS `SCHUL_KND_SC_NM` | 행 수 | 역할 |
| --- | ---: | --- |
| `초등학교` | 610 | 공식 통계 대조 |
| `중학교` | 390 | 공식 통계 대조 |
| `고등학교` | 319 | 공식 통계 대조 |
| `특수학교` | 32 | 공식 통계 대조 |
| `각종학교(초)` | 1 | 공식 기타 대조 |
| `각종학교(중)` | 7 | 공식 기타 대조 |
| `각종학교(고)` | 13 | 공식 기타 대조 |
| `고등기술학교` | 1 | 공식 기타 대조 |
| `외국인학교` | 17 | 보충 모집단 |
| `방송통신중학교` | 1 | 보충 모집단 |
| `방송통신고등학교` | 5 | 보충 모집단 |
| `평생학교(초)-3년6학기` | 2 | 기존 검토 격리 |
| `평생학교(중)-2년6학기` | 5 | 기존 검토 격리 |
| `평생학교(고)-2년6학기` | 7 | 기존 검토 격리 |
| `평생학교(고)-3년6학기` | 4 | 기존 검토 격리 |
| `공동실습소` | 1 | 비선택 원천 행 |
| **합계** | **1,415** | |

`공동실습소` 1행을 제외한 NEIS 정규화 행은 1,414개다. 평생학교 18행은 기존 정책대로 `UNCLASSIFIED_SCHOOL`/`REVIEW_REQUIRED`로 보존한다. 유치원알리미는 공시차수 `20261`, 원천 기준일 2026-04-01, 706행이다.

## 범위와 비범위

- 범위: NEIS 원본 학교종류 전체 분포, 유치원 공시차수·총량, 공식 통계 모집단과 보충 모집단 분리, 검토된 차이, provenance·manifest·signed transaction·review digest 결합, 관리자 감사 출력, fail-closed 갱신 절차.
- 비범위: 학교 이름·주소·좌표를 정책에 저장, 평생학교 격리 완화, 보충 모집단을 삭제, 통계 차이를 백분율 허용 오차로 일반화, 후보 자동 승인, 이용자 안내에 내부 통계 정책 노출.

## 모집단 프로필 리소스

`resources/institution-sources/school-count-population-profile.csv`를 새 검수 리소스로 버전 관리한다. 리소스는 메타데이터와 정렬된 행으로 구성하고, 코드는 canonical SHA-256을 고정한다.

행 스키마는 정확히 다음 여섯 필드다.

```text
source,source_category,observed_count,normalized_type,reconciliation_role,benchmark_type
```

허용 `reconciliation_role`은 다음 네 값뿐이다.

- `BENCHMARK`: 공식 통계와 같은 모집단에 포함한다.
- `SUPPLEMENTARY`: 서비스 데이터에는 포함하지만 공식 통계 분자에서는 제외한다.
- `QUARANTINED`: 원천 completeness를 위해 보존하되 기존 평생학교 정책으로 공개 비노출한다.
- `NONSELECTABLE`: 원천 총량에는 포함하지만 기관 snapshot에는 넣지 않는다.

메타데이터는 스키마 버전, 상태 `TEMPORARY_PRELIMINARY_VARIANCE`, 검토일 `2026-08-13`, 검토 역할 `data-steward`, NEIS 범위 `B10`, NEIS 원천 1,415·정규화 1,414, 유치원 공시차수 `20261`·기준일 2026-04-01·총량 706, 공식 통계 URL·기준일·raw SHA-256, 기존 평생학교 정책 SHA-256을 포함한다.

프로필의 네 `QUARANTINED` 행은 기존 `neis-unclassified-school-kinds.csv`의 정확한 라벨·건수와 다시 대조한다. 새 리소스가 기존 격리 정책의 대체 원천이 되지 않으며, 두 정책이 다르면 수집을 중단한다.

각 행의 정규화·대조 매핑은 다음과 같이 고정한다. `benchmark_type`이 빈 행은 공식 통계 분자에 포함하지 않는다.

| source/category | normalized_type | role | benchmark_type |
| --- | --- | --- | --- |
| NEIS `초등학교` | `ELEMENTARY_SCHOOL` | `BENCHMARK` | `ELEMENTARY_SCHOOL` |
| NEIS `중학교` | `MIDDLE_SCHOOL` | `BENCHMARK` | `MIDDLE_SCHOOL` |
| NEIS `고등학교` | `HIGH_SCHOOL` | `BENCHMARK` | `HIGH_SCHOOL` |
| NEIS `특수학교` | `SPECIAL_SCHOOL` | `BENCHMARK` | `SPECIAL_SCHOOL` |
| NEIS `각종학교(초/중/고)` | `MISC_SCHOOL` | `BENCHMARK` | `MISC_SCHOOL` |
| NEIS `고등기술학교` | `MISC_SCHOOL` | `BENCHMARK` | `MISC_SCHOOL` |
| NEIS `외국인학교` | `MISC_SCHOOL` | `SUPPLEMENTARY` | 빈 값 |
| NEIS `방송통신중학교` | `MIDDLE_SCHOOL` | `SUPPLEMENTARY` | 빈 값 |
| NEIS `방송통신고등학교` | `HIGH_SCHOOL` | `SUPPLEMENTARY` | 빈 값 |
| NEIS 네 평생학교 라벨 | `UNCLASSIFIED_SCHOOL` | `QUARANTINED` | 빈 값 |
| NEIS `공동실습소` | 빈 값 | `NONSELECTABLE` | 빈 값 |
| KINDERGARTEN_INFO `KINDERGARTEN_TOTAL` | `KINDERGARTEN` | `BENCHMARK` | `KINDERGARTEN` |

`각종학교(초/중/고)`와 `네 평생학교 라벨`은 문서 표기의 묶음일 뿐 CSV에는 위에서 열거한 exact 원본 라벨이 각각 한 행씩 들어간다. 빈 값은 공백 문자열이 아니라 CSV의 빈 필드로만 표현한다.

NEIS 역할별 합계는 `BENCHMARK=1,373`, `SUPPLEMENTARY=23`, `QUARANTINED=18`, `NONSELECTABLE=1`이며 총 1,415다. 정규화 출력은 앞의 세 역할 1,414개다. KINDERGARTEN_INFO는 `BENCHMARK=706` 하나다. 이 합계 관계도 별도 gate로 검증한다.

리소스 로더는 정확한 필드 순서, exact built-in 문자열·정수 타입, NFC 문자열, 앞뒤 공백 없음, 중복 없는 정렬 순서, 양의 건수, 허용 역할·유형, 고정 메타데이터, canonical SHA-256을 검증한다. 심볼릭 링크, 16KiB 초과, UTF-8 오류, 빈 행, 추가 열·메타데이터도 거부한다.

## 공식 대조 모집단

공식 통계 분자는 정규화된 최종 유형 전체를 단순 집계하지 않고, 검수 프로필의 `BENCHMARK` 원본 범주만 합산한다. 이에 따라 방송통신학교와 외국인학교가 중·고·기타 분자에 중복 편입되지 않는다.

| 대조 유형 | 공식 기대 | 프로필 실제 | 승인된 차이 | 판정 |
| --- | ---: | ---: | ---: | --- |
| `KINDERGARTEN` | 724 | 706 | -18 | `REVIEWED_VARIANCE` |
| `ELEMENTARY_SCHOOL` | 609 | 610 | +1 | `REVIEWED_VARIANCE` |
| `MIDDLE_SCHOOL` | 390 | 390 | 0 | `MATCHED` |
| `HIGH_SCHOOL` | 319 | 319 | 0 | `MATCHED` |
| `SPECIAL_SCHOOL` | 32 | 32 | 0 | `MATCHED` |
| `MISC_SCHOOL` | 18 | 22 | +4 | `REVIEWED_VARIANCE` |

`MISC_SCHOOL`의 프로필 실제 22는 각종학교 21과 고등기술학교 1이다. 외국인학교 17은 `SUPPLEMENTARY`라 공식 기타 분자에서 제외한다. 방송통신중학교 1과 방송통신고등학교 5도 각각 서비스의 중·고 유형으로 정규화하지만 공식 분자에서는 제외한다.

기존 1% tolerance는 이 정책의 승인 근거로 사용하지 않는다. gate는 위 기대·실제·부호 있는 차이가 모두 정확히 일치할 때만 통과한다. 하나라도 달라지면 `REVIEWED_VARIANCE`를 자동 확대하지 않고 정책 재검토가 필요한 실패로 처리한다.

## 수집 및 검증 흐름

1. 검수 프로필과 기존 평생학교 정책을 네트워크 호출 전에 로드·검증한다.
2. NEIS 수집은 filtering 이전의 모든 원본 `SCHUL_KND_SC_NM`을 exact 문자열로 집계한다. 앞뒤 공백·NFC 변형·새 라벨·건수 변경은 실패한다.
3. NEIS 원본 총량 1,415, `NONSELECTABLE` 1, 정규화 1,414, 네 역할별 집계가 프로필과 정확히 일치하는지 확인한다.
4. 유치원 수집은 요청 공시차수 `20261`, 실제 기준일 2026-04-01, 총량 706을 프로필과 정확히 대조한다.
5. 기존 source date histogram, pagination, raw/normalized hash, 서울 범위, 좌표 품질, SEN·평생학교 검증을 모두 통과시킨다.
6. `BENCHMARK` 원본 범주만 공식 기대치와 대조하고, 정확한 승인 차이와 상태를 계산한다.
7. 모든 gate가 통과하면 후보만 생성한다. 동기화 CLI는 `current.json`을 쓰거나 승인을 수행하지 않는다.
8. 관리자는 감사 JSON과 검토 패킷을 확인한 뒤 별도 승인 CLI에 review digest를 전달한다.

## provenance와 승인 결합

NEIS source provenance에는 정렬된 전체 원본 학교종류 건수, 프로필 SHA-256, 역할별 총량, 기존 평생학교 정책 SHA-256을 저장한다. 유치원 provenance에는 공시차수, 기준일, 총량, 프로필 SHA-256을 저장한다.

후보 manifest에는 strict `schoolCountReconciliation` 블록을 추가한다. 블록은 다음만 포함한다.

- `profileStatus`, `profileSha256`, `benchmarkSha256`
- source별 원본 총량·정규화 총량과 역할별 총량
- 유형별 `expectedCount`, `actualCount`, signed `deltaCount`, `status`
- `passed=true`

manifest SHA-256가 signed build transaction에 포함되므로 위 블록도 transaction에 결합된다. 사람 검토 패킷에는 `schoolCountReconciliationSha256`과 동일한 aggregate 블록을 포함하고, review digest 계산 대상에 넣는다. 승인 함수는 잠금 안에서 provenance·manifest·후보 행으로 대조 결과를 다시 계산하고 exact digest를 비교한 뒤에만 기존 원자적 승격을 수행한다.

후보 파일이나 profile/benchmark hash, source category histogram, timing, 역할별 총량, 승인 차이, transaction receipt를 바꾸면 검토 또는 승인 단계에서 포인터 쓰기 전에 실패한다.

## 관리자 감사와 개인정보 경계

`PRE_PROMOTION_RECONCILIATION`, 후보 manifest, review packet은 다음 집계만 표시한다.

- 고정 allowlist의 원본 범주명과 건수
- `BENCHMARK`·`SUPPLEMENTARY`·`QUARANTINED`·`NONSELECTABLE` 역할별 건수
- 공식 기대·프로필 실제·부호 있는 차이·판정
- 공시차수·기준일·검수 리소스 해시

학교명, 주소, 좌표, 연락처, 원시 행, API 키, 인증 query, HMAC 키는 포함하지 않는다. 원본 범주명도 검수 리소스의 고정 allowlist에 있는 값만 출력하므로 임의 원천 문자열을 감사 JSON 키로 반사하지 않는다. 상세 절차는 README 관리자 운영 절에만 기록하고 일반 이용자 안내에는 넣지 않는다.

## 공개 서비스 동작

- 초·중·고·특수·각종·고등기술·외국인·방송통신 학교는 기존 유형 매핑, 좌표 품질, 서울 범위, status gate를 통과하면 공개 검색·출발지에 사용할 수 있다.
- 평생학교 18행은 기존 정책대로 기관과 모든 사이트가 `REVIEW_REQUIRED`이며 공개 검색·`require_site`·출발지·경로 계산에서 제외한다.
- `공동실습소`는 원천 completeness 감사에는 포함하지만 기관 snapshot에는 포함하지 않는다.
- 이 정책은 기관의 공개 이름·유형·상태를 새로 추론하거나 바꾸지 않는다.

## 실패 및 갱신 절차

다음 경우 기존 `current.json`을 바이트 단위로 변경하지 않고 실패한다.

1. NEIS 또는 유치원 원천 범주·총량·기준일 변경
2. 새 학교종류, 공백·Unicode 변형, category role 변경
3. 검수 프로필·기존 평생학교 정책·공식 통계 리소스의 스키마 또는 hash 변경
4. 기대·실제·승인 차이 중 하나라도 불일치
5. provenance, manifest, candidate JSONL, transaction, review digest 변조
6. 기존 좌표·서울 범위·quality·source date gate 실패

서울시교육청이 2026년 4월 1일 기준 확정 통계를 게시하거나 원천 분류가 바뀌면 이 임시 정책을 조용히 갱신하지 않는다. 관리자는 새 공식 자료의 URL·기준일·raw SHA-256, 전체 원천 분포, 차이 원인을 다시 검토하고 별도 설계·테스트·사람 승인을 거친다. 확정 통계와 원천 모집단이 일치하면 `TEMPORARY_PRELIMINARY_VARIANCE` 정책을 제거한다.

## 검증 전략

TDD 회귀 테스트로 다음을 고정한다.

1. 운영형 NEIS 1,415행의 전체 raw histogram과 역할 합계가 정확히 통과한다.
2. 방송통신 6·외국인 17은 서비스 레코드에 남지만 공식 분자에서 제외된다.
3. 평생학교 18은 기존 격리 정책과 exact cross-check되고 계속 공개 비노출된다.
4. 공동실습소 1은 raw total에는 포함되고 normalized output에는 없다.
5. 유치원 `20261`/2026-04-01/706과 여섯 공식 대조 유형의 exact signed variance만 통과한다.
6. 새 라벨, 건수·기준일·역할·부호 변경, profile/benchmark hash 변조는 후보 생성 전에 실패한다.
7. manifest/provenance/transaction/review packet의 동일 aggregate 변조는 승인 전에 실패하고 포인터를 변경하지 않는다.
8. 감사 출력은 고정 allowlist 집계만 포함하고 이름·주소·좌표·키·원시 행을 포함하지 않는다.
9. candidate-only 동기화, 사람 review digest 승인, 승인 재시도, release fail-closed를 유지한다.
10. 전체 Python warning-strict, Ruff, mypy, Playwright 회귀 검증을 통과한다.

## 승인 기준

이 설계의 구현은 운영 원천을 현재 수치에 억지로 맞추는 보정이 아니다. 원천 모집단과 잠정 공식 통계의 차이를 명시적으로 분리·고정하고, 정확한 동일 상태에서만 후보 생성을 허용하는 임시 fail-closed 정책이다. 구현 후에도 승인된 운영 snapshot이 생기기 전까지 MVP release gate는 계속 차단되어야 한다.
