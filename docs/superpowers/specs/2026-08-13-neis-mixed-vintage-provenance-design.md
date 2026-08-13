# NEIS 혼합 기준일 Provenance 설계

## 목적

서울시교육청(B10) 학교 원천이 같은 수집 시점에 서로 다른 `LOAD_DTM` 기준일을 반환해도, 기관을 누락하거나 날짜를 임의로 통일하지 않고 사람 검토를 거친 승인 후보를 만들 수 있게 한다.

2026-08-13 실측 응답은 총 1,415행이며 기준일 분포는 다음과 같다.

| `LOAD_DTM` | 행 수 |
| --- | ---: |
| 2026-04-23 | 1,413 |
| 2026-05-17 | 1 |
| 2026-06-07 | 1 |

## 범위와 비범위

- 범위: NEIS `schoolInfo`의 행별 관측 기준일 보존, 날짜별 집계 provenance, 후보 검토·승인·스냅샷 검증의 무결성 결합, 운영 문서화와 회귀 테스트.
- 비범위: 특정 기준일 행의 삭제, 원천 API의 날짜 필터 추정, 기관명·주소를 검토 패킷에 추가, 자동 승인·자동 공개, 유치원·SEN 원천의 기준일 정책 변경.

## 데이터 계약

`NEIS` 원천의 각 `SourceInstitutionRecord.source_as_of`는 해당 원시 행의 `LOAD_DTM`을 ISO 날짜로 변환한 값이다. 수집기는 페이지 안과 페이지 사이의 복수 기준일을 허용하되, 모든 원시 행이 유효한 `LOAD_DTM`을 가져야 한다.

`SourceProvenance`에는 `sourceObservationDateCounts`를 추가한다. 이 값은 다음 조건을 모두 만족해야 한다.

1. 키는 ISO `YYYY-MM-DD` 날짜이며 사전순으로 정렬된다.
2. 값은 양의 정수다.
3. 값의 합은 `fetchedRowCount`와 정확히 같다.
4. NEIS 정규화 레코드의 `source_as_of` 분포와 정확히 일치한다.
5. 원시 페이지 연결 SHA-256, 정규화 레코드 SHA-256, 날짜별 건수는 모두 후보 transaction과 manifest에 결합된다.

기존 단일 `source_as_of` 필드는 NEIS의 경우 최댓값·최솟값 등으로 대체하지 않는다. 여러 관측일이 있는 원천은 다중 기준일을 명시적으로 표현하는 새 histogram으로만 승인된다. 단일 기준일 원천도 크기 1 histogram을 사용해 같은 검증 경로를 따른다.

## 후보와 승인 흐름

```mermaid
flowchart LR
  A[NEIS 원시 페이지] --> B[행별 LOAD_DTM 검증]
  B --> C[정규화 레코드 + 날짜별 histogram]
  C --> D[후보 manifest와 signed transaction]
  D --> E[검토 패킷]
  E -->|사람 검토·digest 승인| F[잠금 하 재검증]
  F --> G[approved current 포인터]
```

후보 검토 패킷은 `NEIS`의 날짜별 건수와 SHA-256만 표시한다. 기관명, 주소, 좌표, API 키, 원시 응답 바이트는 포함하지 않는다. 생성 시점과 승인 시점에 다음을 재검증한다.

- transaction의 HMAC receipt와 단계
- 원시·정규화 SHA-256 및 행 수
- 날짜 histogram의 정렬·형식·합계·정규화 레코드 일치
- 기존 품질 gate, 서울 경계, source/enrichment provenance
- 검토 digest의 constant-time 일치

날짜 histogram을 변조하거나, 키 순서를 바꾸거나, 한 행의 관측일을 바꾸거나, 값의 합을 바꾸면 검토 패킷 생성과 승인 모두 포인터 쓰기 전에 실패해야 한다.

## 오류 처리

- 누락·비정상 `LOAD_DTM`, 중복 JSON 키, 날짜 형식 오류, 빈 histogram, 정렬되지 않은 키, 합계 불일치는 `SourceDataError` 또는 snapshot validation 오류로 fail-closed 한다.
- 수집 중 원천 목록 총계가 바뀌거나 페이지가 반복되면 후보를 만들지 않는다.
- 기존 `current.json`은 후보 생성·검토·거부·실패에서 바이트 단위로 변하지 않는다.
- 승인 성공 후에만 이미 검증된 후보가 공개 포인터가 된다. 재시도는 같은 검토 digest의 idempotent 경로만 허용한다.

## 검증 전략

TDD 회귀 테스트로 다음을 고정한다.

1. 한 페이지와 여러 페이지에 섞인 유효 날짜를 보존하고 정렬된 histogram을 만든다.
2. 잘못된 날짜, histogram의 정렬 위반·합계 위반·행별 불일치, transaction/manifest 변조가 검토와 승인 전에 실패한다.
3. 실제처럼 `1413/1/1`인 mixed-vintage fixture가 후보를 만들되 자동 승격하지 않는다.
4. 검토 패킷은 날짜별 건수와 해시만 보이고 PII·원시 데이터·키를 포함하지 않는다.
5. 승인은 재검증된 histogram과 review digest가 같을 때만 포인터를 쓴다.
6. 단일 기준일 NEIS fixture와 기존 유치원/SEN 스냅샷 검증은 회귀 없이 유지된다.

## 운영 절차

관리자는 `sync-institutions.py --env-file ...`로 후보만 생성한다. `review-institution-snapshot.py`가 출력한 날짜별 건수·원천/품질 집계를 검토하고, 의도한 후보라면 digest를 `approve-institution-snapshot.py`에 전달한다. 후보가 승인되기 전에는 release gate가 계속 차단되어야 한다.
