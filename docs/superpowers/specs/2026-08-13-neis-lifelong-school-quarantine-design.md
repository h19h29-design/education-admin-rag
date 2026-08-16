# NEIS 평생학교 검토 격리 설계

## 목적

서울특별시교육청(B10) NEIS `schoolInfo`가 반환하는 평생학교 18행을 손실 없이 감사 대상으로 보존하되, 공식 학교 분류와 최신 통계가 확정되기 전에는 공개 검색·출발지·경로 계산에 사용되지 않게 한다.

2026-08-13 실측 원천에서 발견한 검토 대상은 다음과 같다.

| NEIS `SCHUL_KND_SC_NM` | 행 수 |
| --- | ---: |
| `평생학교(초)-3년6학기` | 2 |
| `평생학교(중)-2년6학기` | 5 |
| `평생학교(고)-2년6학기` | 7 |
| `평생학교(고)-3년6학기` | 4 |
| **합계** | **18** |

## 범위와 비범위

- 범위: 평생학교 원천 유형 허용 계약, `UNCLASSIFIED_SCHOOL` 격리 유형, 관리자 감사·검토 집계, snapshot provenance·review digest 결합, 공개 비노출, fail-closed 테스트.
- 비범위: 평생학교를 `ELEMENTARY_SCHOOL`, `MIDDLE_SCHOOL`, `HIGH_SCHOOL`, `MISC_SCHOOL`로 임의 매핑, 공식 통계의 재정의, 자동 승인, 이용자 문서에 격리 내부 상세 노출.

## 분류 및 격리 계약

NEIS 정규화 단계는 위 네 개 원본 유형만 `UNCLASSIFIED_SCHOOL`로 변환한다. `SourceInstitutionRecord`는 정규화 유형과 별도로 `source_kind_label`을 갖는다. 이 필드는 NEIS 레코드의 동기화·후보 생성 안에서만 사용하고 snapshot 기관 JSONL에는 저장하지 않는다. 대신 정렬된 유형별 집계와 검수 리소스 해시를 source provenance·signed transaction·review digest에 결합한다. 원천 분류명은 관리자 감사 및 provenance 무결성 용도일 뿐, 공개 API나 이용자 UI에 노출하지 않는다.

새로운 미분류 유형, 기대 건수와 다른 건수, 허용 리소스의 스키마·해시 변경은 모두 원천 단계에서 실패한다. 조용히 새 유형을 더 많이 격리하거나 기존 학교 범주로 편입할 수 없다.

`UNCLASSIFIED_SCHOOL` 기관과 그 모든 사이트는 좌표·서울 경계 판정이 정상이어도 무조건 `REVIEW_REQUIRED`다. `statusSource`는 격리 원인을 구별할 수 있는 고정 코드를 사용한다. 스냅샷 재검증과 승인은 기관과 사이트의 격리 상태를 다시 계산하여, 임의로 `ACTIVE`로 변조된 후보를 포인터 쓰기 전에 거부한다.

## 검수 리소스

네 개 원본 유형과 기대 건수를 `resources/institution-sources/neis-unclassified-school-kinds.csv`에 버전 관리한다. 코드는 이 리소스의 canonical SHA-256를 고정하고, manifest에 `unclassifiedSchoolPolicySha256`를 기록한다. 리소스는 다음을 포함한다.

- 원천 `SCHUL_KND_SC_NM`
- 기대 행 수
- 격리 이유 코드 `OFFICIAL_CLASSIFICATION_PENDING`
- 검수 일자와 관리자 권한
- NEIS B10 원천 범위·URL과 검수된 유형 집계의 canonical SHA-256

프로그램은 리소스의 정확한 스키마, 중복 없는 정렬된 유형명, 양의 정수 건수, 고정 이유 코드를 검증한다. 실제 NEIS 집계는 리소스와 키·값 모두 정확히 일치해야 한다. 불일치는 승인대기 후보가 아니라 신규 정책 검토가 필요한 동기화 실패다.

## 통계 대조와 감사 출력

공식 학교 수 대조는 기존 `KINDERGARTEN`, `ELEMENTARY_SCHOOL`, `MIDDLE_SCHOOL`, `HIGH_SCHOOL`, `SPECIAL_SCHOOL`, `MISC_SCHOOL` 범주만 사용한다. `UNCLASSIFIED_SCHOOL` 18건은 이 분모와 분자에 합산하지 않는다. 대신 검수 리소스와 정확히 일치하는 경우에만 별도 `unclassifiedSchoolKindCounts` 정책 gate를 통과한다.

다음 관리자 출력에 정렬된 `unclassifiedSchoolKindCounts`를 포함한다.

- 승격 전 `PRE_PROMOTION_RECONCILIATION` 감사 JSON
- 후보 manifest의 NEIS source provenance
- `review-institution-snapshot.py`의 사람 검토 패킷

해당 집계는 유형명과 건수만 노출한다. 기관명, 주소, 좌표, API 키, 원시 행, endpoint query는 포함하지 않는다. 후보 manifest와 signed transaction은 NEIS 정규화 해시 및 이 집계를 결합한다. 승인 시점에 감사 패킷을 다시 생성하여 review digest와 constant-time 비교한다.

## 공개 비노출

`InstitutionStore`는 기관과 사이트가 모두 `ACTIVE`인 레코드만 인덱싱하는 기존 계약을 유지한다. 따라서 `UNCLASSIFIED_SCHOOL` 기관은:

- 기관 검색 API 결과에 나오지 않는다.
- `require_site` 및 출발지 확정을 거부한다.
- 경로·여비 미리보기 입력으로 사용할 수 없다.
- 관리자 snapshot JSONL·manifest·검토 패킷에서는 감사 대상으로 계속 보존된다.

이 공개 경계들을 store 단위 테스트와 API 통합 테스트로 각각 고정한다.

## 오류 처리와 운영 절차

다음 경우는 기존 `current.json`을 바이트 단위로 변경하지 않고 fail-closed 한다.

1. 허용되지 않은 NEIS 학교 유형 발견
2. 네 개 격리 유형의 건수 변경
3. 검수 리소스 스키마·해시·정렬 위반
4. `UNCLASSIFIED_SCHOOL` 기관 또는 사이트의 `ACTIVE` 변조
5. manifest 집계, 정규화 해시, transaction receipt, review digest 불일치

관리자는 후보 생성 후 `unclassifiedSchoolKindCounts`, 전체 source/type/status 건수, 격리 ID, 좌표 품질, 기존 snapshot과의 diff를 검토한다. 이 후에만 기존 사람 승인 CLI에 review digest를 전달한다. 이 정책은 README의 **관리자 운영** 절에만 추가하고 일반 이용자 안내에는 포함하지 않는다.

## 검증 전략

TDD 회귀 테스트로 다음을 고정한다.

1. 실측 네 유형 `2/5/7/4`가 정확히 18건의 `UNCLASSIFIED_SCHOOL`로 정규화된다.
2. 새 유형, 건수 변경, 중복·비정렬·손상된 검수 리소스는 후보 생성 전에 실패한다.
3. 기존 공식 학교 수 대조는 평생학교를 합산하지 않고 독립적으로 통과한다.
4. 정규화·좌표 보완 후에도 18건의 기관·사이트가 모두 `REVIEW_REQUIRED`다.
5. 검토 패킷은 정렬된 유형별 건수를 포함하되 학교명·주소·좌표·키·원시 행을 포함하지 않는다.
6. 집계·provenance·후보 파일·승인 transaction의 변조는 포인터 쓰기 전에 실패한다.
7. 승인된 snapshot을 로드한 뒤에도 18건은 store 검색·`require_site`·출발지 API에서 배제된다.
8. 기존 혼합 `LOAD_DTM` histogram, 후보 인간 승인, 유치원·SEN, release fail-closed 회귀 검증을 모두 유지한다.

## 후속 정책 변경

서울시교육청 최신 공식 통계나 NEIS 분류 지침이 이 18건의 학교 범주를 확정하면 별도 설계·검토로 매핑과 통계 gate를 갱신한다. 그 전까지는 이 격리 정책을 자동 완화하지 않는다.
