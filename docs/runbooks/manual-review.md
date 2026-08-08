# NAS 수동 검수 운영 경계

이 runbook은 원본·raw·canonical corpus와 검수 상태를 서로 다른 권한 경계에
둔다. 검수 DB에는 본문이 아니라 opaque case ID, SHA-256, 상태, provenance와
감사 event만 저장한다. 아래 명령은 실제 자격 증명을 포함하지 않으며 NAS root
운영자가 값만 치환해 실행한다.

> **PRODUCTION RELEASE BLOCKED**
>
> root 소유의 제한형 review broker와 `SO_PEERCRED` 검증, 고정 이미지·mount
> allowlist, 두 실제 NAS 계정 동시성 시험이 구현·통과되기 전에는 검수 CLI를
> NAS 운영 DB에 직접 연결하거나 `assert-ready` 결과로 release하면 안 된다.
> 현재 SQLite trigger는 애플리케이션 실수를 막는 defense-in-depth일 뿐,
> DB 파일 writer에 대한 변조 방지 경계가 아니다.

## 1. 계정, 그룹, 경로

검수자는 review service 계정, service group, Docker/Container Manager 관리자
그룹에 속하지 않아야 하며 임의 컨테이너 실행 sudo 규칙도 없어야 한다.

```sh
set -eu
umask 0007

export SEN_QA_ROOT='/volume1/education-admin'
export SEN_QA_SOURCE_DIR="$SEN_QA_ROOT/source"
export SEN_QA_RAW_DIR="$SEN_QA_ROOT/raw"
export SEN_QA_CANONICAL_DIR="$SEN_QA_ROOT/canonical"
export SEN_QA_REGISTRY_FILE="$SEN_QA_CANONICAL_DIR/review-registry.json"
export SEN_QA_REVIEW_STATE_DIR="$SEN_QA_ROOT/review-state"
export SEN_QA_REVIEW_DB="$SEN_QA_REVIEW_STATE_DIR/review.sqlite3"
export SEN_QA_QUEUE_DIR="$SEN_QA_ROOT/review-queue"
export SEN_QA_CORRECTION_DIR="$SEN_QA_ROOT/corrections"
export SEN_QA_BROKER_DIR="$SEN_QA_ROOT/review-broker"
export SEN_QA_BROKER_SOCKET="$SEN_QA_BROKER_DIR/review.sock"

export SEN_QA_INGESTION_GROUP='senqa-ingestion'
export SEN_QA_REVIEW_GROUP='senqa-reviewer'
export SEN_QA_SERVICE_GROUP='senqa-review-service'
export SEN_QA_SERVICE_USER='<review-service-account>'
export SEN_QA_REVIEWER_PROBE_USER='<reviewer-account>'
export SEN_QA_BROKER_CONTAINER='<root-managed-review-broker-container>'
export SEN_QA_DOCKER='/var/packages/ContainerManager/target/usr/bin/docker'

test "$(id -u)" -eq 0
getent group "$SEN_QA_INGESTION_GROUP" >/dev/null
getent group "$SEN_QA_REVIEW_GROUP" >/dev/null
getent group "$SEN_QA_SERVICE_GROUP" >/dev/null
id "$SEN_QA_SERVICE_USER" >/dev/null
id "$SEN_QA_REVIEWER_PROBE_USER" >/dev/null
command -v setfacl >/dev/null
command -v getfacl >/dev/null
command -v sudo >/dev/null
test -x "$SEN_QA_DOCKER"
```

## 2. 동결 corpus: host ACL과 container `:ro`

source, raw, canonical은 root/ingestion 소유의 `0550` 디렉터리와 `0440`
파일로 동결한다. reviewer와 비-root broker service에는 세 경로 모두 읽기·탐색
ACL만 주며 새 항목도 같은 ACL을 상속한다. 최상위 경로는 목록을 볼 수
없게 잠그고 ingestion, reviewer, service group에게 탐색(`--x`)만 허용한다.

```sh
install -d -o root -g "$SEN_QA_INGESTION_GROUP" -m 0710 \
  "$SEN_QA_ROOT"
setfacl -m \
  "u::rwx,g::--x,g:$SEN_QA_REVIEW_GROUP:--x,g:$SEN_QA_SERVICE_GROUP:--x,m::--x,o::---" \
  "$SEN_QA_ROOT"

install -d -o root -g "$SEN_QA_INGESTION_GROUP" -m 0550 \
  "$SEN_QA_SOURCE_DIR" "$SEN_QA_RAW_DIR" "$SEN_QA_CANONICAL_DIR"

find "$SEN_QA_SOURCE_DIR" "$SEN_QA_RAW_DIR" "$SEN_QA_CANONICAL_DIR" \
  -type d -exec chown root:"$SEN_QA_INGESTION_GROUP" {} + \
  -exec chmod 0550 {} +
find "$SEN_QA_SOURCE_DIR" "$SEN_QA_RAW_DIR" "$SEN_QA_CANONICAL_DIR" \
  -type f -exec chown root:"$SEN_QA_INGESTION_GROUP" {} + \
  -exec chmod 0440 {} +

setfacl -R -m "g:$SEN_QA_REVIEW_GROUP:r-X,g:$SEN_QA_SERVICE_GROUP:r-X" \
  "$SEN_QA_SOURCE_DIR" "$SEN_QA_RAW_DIR" "$SEN_QA_CANONICAL_DIR"
setfacl -R -d -m "g:$SEN_QA_REVIEW_GROUP:r-X,g:$SEN_QA_SERVICE_GROUP:r-X" \
  "$SEN_QA_SOURCE_DIR" "$SEN_QA_RAW_DIR" "$SEN_QA_CANONICAL_DIR"
```

root가 관리하는 broker container는 다음 mount 계약을 고정해야 한다. source,
raw, canonical, registry는 반드시 `:ro`; review-state만 service writer에게 `:rw`다.
queue/corrections는 별도 mount이고 임의 host path, 이미지, command를 client가
선택할 수 없다. read-only root filesystem과 `--network none`, 고정 UID/GID,
`/tmp`의 `mode=1777,nosuid,nodev,noexec`도 broker 배포 설정에 고정한다.

```text
/volume1/education-admin/source          -> /data/source:ro
/volume1/education-admin/raw             -> /data/raw:ro
/volume1/education-admin/canonical       -> /data/canonical:ro
review-registry.json + expected SHA-256  -> /input/review-registry.json:ro
/volume1/education-admin/review-state    -> /data/review-state:rw
/volume1/education-admin/review-queue    -> /data/review-queue:rw
/volume1/education-admin/corrections     -> /data/corrections:rw
```

## 3. 쓰기 경계: service DB와 reviewer 작업 경로

service account만 DB/WAL/SHM을 만들고 수정한다. reviewer는 review-state 디렉터리
자체를 읽거나 쓸 수 없다. SQLite DB, WAL, SHM은 같은 로컬 NAS volume과 같은
디렉터리에 두며 service 중지 시 함께 snapshot하거나 SQLite online backup API를
사용한다.

queue와 correction 경로만 reviewer group이 쓰며 setgid `2770`과 default ACL로
두 계정 사이의 group 소유권을 안정적으로 상속한다. 일반 파일은 `0660` 이하로
제한한다.

```sh
install -d -o "$SEN_QA_SERVICE_USER" -g "$SEN_QA_SERVICE_GROUP" -m 0700 \
  "$SEN_QA_REVIEW_STATE_DIR"
install -d -o "$SEN_QA_SERVICE_USER" -g "$SEN_QA_REVIEW_GROUP" -m 2770 \
  "$SEN_QA_QUEUE_DIR" "$SEN_QA_CORRECTION_DIR"
install -d -o "$SEN_QA_SERVICE_USER" -g "$SEN_QA_REVIEW_GROUP" -m 0750 \
  "$SEN_QA_BROKER_DIR"

setfacl -m "u::rwx,g::rwx,o::---,m::rwx" \
  "$SEN_QA_QUEUE_DIR" "$SEN_QA_CORRECTION_DIR"
setfacl -d -m "u::rwx,g::rwx,o::---,m::rwx" \
  "$SEN_QA_QUEUE_DIR" "$SEN_QA_CORRECTION_DIR"

if test -e "$SEN_QA_REVIEW_DB"; then
  chown "$SEN_QA_SERVICE_USER:$SEN_QA_SERVICE_GROUP" "$SEN_QA_REVIEW_DB"
  chmod 0600 "$SEN_QA_REVIEW_DB"
fi
find "$SEN_QA_REVIEW_STATE_DIR" -maxdepth 1 \
  \( -name 'review.sqlite3' -o -name 'review.sqlite3-wal' \
     -o -name 'review.sqlite3-shm' \) \
  -exec chown "$SEN_QA_SERVICE_USER:$SEN_QA_SERVICE_GROUP" {} + \
  -exec chmod 0600 {} +
find "$SEN_QA_QUEUE_DIR" "$SEN_QA_CORRECTION_DIR" -type f \
  -exec chmod 0660 {} +
```

## 4. 제한형 broker 계약

reviewer가 DB나 Docker에 직접 쓰지 않는다. root 소유 broker만 service UID로
SQLite를 열며 Unix socket을 `0660`, group `senqa-reviewer`로 제공한다. broker는
연결마다 kernel의 `SO_PEERCRED` UID를 읽고 `pwd`에서 actor를 만들며 요청 body의
`reviewer_id`를 신뢰하지 않는다.

broker가 허용할 operation은 아래로 한정한다.

- `verify-fields`, `approve-search`, `approve-answer`, `reject`
- 건별 확인을 요구하는 `run critical-fields-all`, `run answer-and-basis-all`
- 해시 고정 manifest 전용 `approve-search-batch`
- 값이 아닌 count와 blocker code만 반환하는 `assert-ready`

case 등록·상태 전이 전에는 read-only canonical registry의 fingerprint를 확인하고,
`case_id + content_sha256 + page_id + bbox + reason_code + count` binding을 DB에
저장된 registry fingerprint와 대조한다. `review run` client는 각 case의 opaque ID,
content hash, page/bbox, reason code, count를 표시하고 사람이 확인하기 전에는
다음 case transaction을 요청하지 않는다. 본문·질문·답변·개인정보 값은 로그나
실패 응답에 출력하지 않는다.

root 전용 broker는 image digest, command, mount source/destination, network mode,
service UID/GID를 하드코딩한다. reviewer에게 Docker socket, Docker group,
Container Manager 관리자 권한 또는 포괄적 sudo wrapper를 주면 root 권한과 같은
우회가 가능하므로 금지한다.

## 5. 운영 전 검증 명령

아래 검증은 broker가 구현된 뒤 root 운영자가 실행한다. `getfacl`의 effective
권한, 실제 두 번째 계정의 동시 요청, WAL/SHM 생성 중 소유권을 모두 확인한다.

```sh
test "$(stat -c '%a' "$SEN_QA_ROOT")" = 710
test "$(stat -c '%a' "$SEN_QA_SOURCE_DIR")" = 550
test "$(stat -c '%a' "$SEN_QA_RAW_DIR")" = 550
test "$(stat -c '%a' "$SEN_QA_CANONICAL_DIR")" = 550
test "$(stat -c '%a' "$SEN_QA_REVIEW_STATE_DIR")" = 700
test "$(stat -c '%a' "$SEN_QA_QUEUE_DIR")" = 2770
test "$(stat -c '%a' "$SEN_QA_CORRECTION_DIR")" = 2770

test -z "$(find "$SEN_QA_SOURCE_DIR" "$SEN_QA_RAW_DIR" \
  "$SEN_QA_CANONICAL_DIR" -type f ! -perm 0440 -print -quit)"
test -z "$(find "$SEN_QA_REVIEW_STATE_DIR" -maxdepth 1 \
  \( -name 'review.sqlite3' -o -name 'review.sqlite3-wal' \
     -o -name 'review.sqlite3-shm' \) \
  \( ! -user "$SEN_QA_SERVICE_USER" -o ! -group "$SEN_QA_SERVICE_GROUP" \) \
  -print -quit)"
test -z "$(find "$SEN_QA_REVIEW_STATE_DIR" -maxdepth 1 \
  \( -name 'review.sqlite3' -o -name 'review.sqlite3-wal' \
     -o -name 'review.sqlite3-shm' \) ! -perm 0600 -print -quit)"

sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test -x "$SEN_QA_ROOT"
sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test ! -r "$SEN_QA_ROOT"
sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test -r "$SEN_QA_SOURCE_DIR"
sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test -r "$SEN_QA_RAW_DIR"
sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test -r "$SEN_QA_CANONICAL_DIR"
sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test ! -w "$SEN_QA_SOURCE_DIR"
sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test ! -w "$SEN_QA_CANONICAL_DIR"
sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test ! -r "$SEN_QA_REVIEW_STATE_DIR"
sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test -w "$SEN_QA_QUEUE_DIR"
sudo -u "$SEN_QA_REVIEWER_PROBE_USER" test -w "$SEN_QA_CORRECTION_DIR"

sudo -u "$SEN_QA_SERVICE_USER" test -x "$SEN_QA_ROOT"
sudo -u "$SEN_QA_SERVICE_USER" test ! -r "$SEN_QA_ROOT"
sudo -u "$SEN_QA_SERVICE_USER" test -r "$SEN_QA_SOURCE_DIR"
sudo -u "$SEN_QA_SERVICE_USER" test -r "$SEN_QA_RAW_DIR"
sudo -u "$SEN_QA_SERVICE_USER" test -r "$SEN_QA_CANONICAL_DIR"
sudo -u "$SEN_QA_SERVICE_USER" test -r "$SEN_QA_REGISTRY_FILE"
sudo -u "$SEN_QA_SERVICE_USER" test ! -w "$SEN_QA_SOURCE_DIR"
sudo -u "$SEN_QA_SERVICE_USER" test ! -w "$SEN_QA_CANONICAL_DIR"
sudo -u "$SEN_QA_SERVICE_USER" test ! -w "$SEN_QA_REGISTRY_FILE"

id -nG "$SEN_QA_REVIEWER_PROBE_USER" | tr ' ' '\n' \
  | grep -Fx "$SEN_QA_SERVICE_GROUP" && exit 1 || true
getfacl -p "$SEN_QA_ROOT" "$SEN_QA_SOURCE_DIR" "$SEN_QA_RAW_DIR" \
  "$SEN_QA_CANONICAL_DIR" "$SEN_QA_REVIEW_STATE_DIR" \
  "$SEN_QA_QUEUE_DIR" "$SEN_QA_CORRECTION_DIR"
```

broker container가 존재할 때 root 운영자는 실제 mount가 read-only인지도 확인한다.

```sh
test -S "$SEN_QA_BROKER_SOCKET"
test "$(stat -c '%a' "$SEN_QA_BROKER_SOCKET")" = 660
test "$(stat -c '%G' "$SEN_QA_BROKER_SOCKET")" = "$SEN_QA_REVIEW_GROUP"

"$SEN_QA_DOCKER" inspect "$SEN_QA_BROKER_CONTAINER" \
  --format '{{range .Mounts}}{{println .Destination .RW}}{{end}}'
```

inspect 결과에서 `/data/source`, `/data/raw`, `/data/canonical`, registry는
`false`, `/data/review-state`만 service container에 `true`여야 한다. 서로 다른
reviewer 두 계정으로 동시에 건별 확인을 수행해 actor UID가 각각 기록되고,
DB/WAL/SHM이 모두 service UID/GID `0600`을 유지하는지도 확인한다.

## 6. release blocker와 증적

canonical 발급기와 review registry는 중앙 `src/corpus/ids.py` validator를 공유한다.
전화·주민번호·계좌 후보처럼 긴 숫자와 알려진 provider token 형태의 raw `case_no`는
안정적인 `opaque-<hash>` component로 바뀌며 거부 오류에도 입력값을 노출하지 않는다.

남은 production blocker는 root 소유 broker와 실제 NAS 계정 통합 시험이다. 이를
닫힌 뒤에만 broker를 통해 `assert-ready`를 실행하고 release 증적에 registry hash,
이미지 digest, blocker count, 두 OS actor UID를 값 최소화 형태로 남긴다.
