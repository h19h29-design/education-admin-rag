# Ingestion image v1 build attestation

검증 시각: `2026-08-08T12:30:01Z`

검증 코드: `5a71934068fbaaa52ed3827090b6c5ec25d8a439`

검증 환경: Synology NAS, Linux/x86_64, Docker 24.0.2, OS-visible memory 19.49 GiB

이 문서는 Task 6의 Linux/amd64 OCR 이미지와 1페이지 offline smoke 결과만 요약한다. OCR 원문과 개인정보 값은 기록하지 않는다. 전체 2023~2025 문서 수집·사람 검수·출시 승인을 대신하지 않는다.

## Image gate

| 항목 | 결과 |
| --- | --- |
| 후보 태그 | `education-admin-ingestion:corpus-v2-candidate` |
| local content digest | `sha256:36ee9e94ee8f404e34954227048e628f71a126f5fd42fd8e370369a73550fdd9` |
| 플랫폼 | `linux/amd64` |
| 최종 크기 | `1,796,610,763` bytes |
| 크기 한도 | `2,500,000,000` bytes 이하: PASS |
| 이전 기준 이미지 | `7,005,662,571` bytes |
| 절감 | `5,209,051,808` bytes (`74.355%`) |
| PaddlePaddle | 정확히 `3.1.1`: PASS |
| runtime distribution | 71개 |
| locked OCR models | 2개, build/runtime 전후 hash 검증: PASS |
| 제외 모듈 | `torch`, `sentence_transformers`, `transformers`, `qdrant_client`, `triton`: 모두 없음 |
| 제외 distribution | `nvidia-*`, `cuda-*`: 0개 |
| 원본 manifest | 6권 모두 일치 (`verified=6 changed=0 failed=0`) |

이미지는 digest 고정 Python 3.11 slim base에서 빌드했다. 모델 다운로드는 build 단계에서만 허용했고, 아래 runtime 검증은 `--network none`, read-only root filesystem, non-root UID/GID, 임시 `/tmp` 조건에서 실행했다. 이 digest는 NAS의 local content ID이며 아직 registry 서명이나 외부 배포 digest는 아니다.

## Offline OCR smoke

입력은 승인 manifest의 2025년 사례집 PDF 13쪽 한 페이지이며 `render_dpi=300`, 기본 MKLDNN 경로, 전체 2480×3509 raster를 사용했다.

| 실행 | 경과 시간 | extracted | quarantined | failed | JSONL SHA-256 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1 | 91초 | 1 | 0 | 0 | `9e9bb29865078b4853e0112fb9764b6447199306165ffe9d2a4c97f08104305e` |
| 2 | 90초 | 1 | 0 | 0 | `9e9bb29865078b4853e0112fb9764b6447199306165ffe9d2a4c97f08104305e` |

두 결과는 바이트 단위로 동일했다. 출력 메타데이터 검증 결과는 다음과 같다.

- `review_status=machine_extracted`, `search_eligible=false`, `answer_eligible=false`
- OCR line/span 각 54개, PDF-point bbox 전부 페이지 경계 안
- confidence 범위 0.0824217945~0.9999828935
- review queue 2건, critical field marker 6건
- source SHA-256, render SHA-256, image digest, 페이지 번호와 DPI 일치

실행 중 읽기 전용 표본 측정은 초기화 구간 432.6 MiB, 추론 구간 2.025 GiB였고 CPU는 각각 약 8.7/7.6 logical cores를 사용했다. 이는 peak profiler 측정이 아니라 `docker stats --no-stream` 두 시점의 표본이다.

## Disposition

Task 6 Linux image gate는 PASS다. 후보 이미지는 전체 수집 전용 기반으로 사용할 수 있다. 다만 OCR 결과는 계속 fail-closed이며, Task 7 이후의 개인정보 분류·품질 게이트·독립 사람 검수와 전체 연도 통합 평가가 끝나기 전에는 검색이나 답변에 노출하지 않는다. 기존 외부 API 키 폐기·사용량 검토·승인자 증적도 공개 배포 전 별도 필수 게이트다.
