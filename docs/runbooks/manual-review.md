# NAS 수동 검수 운영 경계

이 runbook은 원본·raw·canonical corpus와 검수 상태를 서로 다른 권한 경계에
둔다. 검수 DB에는 본문이 아니라 opaque case ID, SHA-256, 상태, provenance와
감사 event만 저장한다. 아래 명령은 실제 자격 증명을 포함하지 않으며 NAS root
운영자가 값만 치환해 실행한다.

> **PRODUCTION RELEASE BLOCKED**
>
> root 소유의 제한형 review broker는 코드에 포함되어 있다. `SO_PEERCRED` 검증,
> 고정 이미지·mount allowlist, 두 실제 NAS 계정 동시성 시험이 운영 NAS에서
> 통과되기 전에는 검수 CLI를
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
export SEN_QA_QUARANTINE_SIDECAR="$SEN_QA_REVIEW_STATE_DIR/parser-quarantine-resolutions.json"
export SEN_QA_QUEUE_DIR="$SEN_QA_ROOT/review-queue"
export SEN_QA_CORRECTION_DIR="$SEN_QA_ROOT/corrections"
export SEN_QA_ANNOTATION_DIR="$SEN_QA_ROOT/approved-quarantine-annotations"
export SEN_QA_BROKER_DIR="$SEN_QA_ROOT/review-broker"
export SEN_QA_BROKER_SOCKET="$SEN_QA_BROKER_DIR/review.sock"

export SEN_QA_INGESTION_GROUP='senqa-ingestion'
export SEN_QA_REVIEW_GROUP='senqa-reviewer'
export SEN_QA_SERVICE_GROUP='senqa-review-service'
export SEN_QA_SERVICE_USER='<review-service-account>'
export SEN_QA_REVIEWER_PROBE_USER='<reviewer-account>'
export SEN_QA_SECOND_REVIEWER_PROBE_USER='<second-reviewer-account>'
export SEN_QA_BROKER_CONTAINER='<root-managed-review-broker-container>'
export SEN_QA_BROKER_IMAGE='<public-image>@sha256:<64-lowercase-hex>'
export SEN_QA_DOCKER='/var/packages/ContainerManager/target/usr/bin/docker'

test "$(id -u)" -eq 0
getent group "$SEN_QA_INGESTION_GROUP" >/dev/null
getent group "$SEN_QA_REVIEW_GROUP" >/dev/null
getent group "$SEN_QA_SERVICE_GROUP" >/dev/null
id "$SEN_QA_SERVICE_USER" >/dev/null
id "$SEN_QA_REVIEWER_PROBE_USER" >/dev/null
id "$SEN_QA_SECOND_REVIEWER_PROBE_USER" >/dev/null
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
/etc/passwd                              -> /etc/passwd:ro
/volume1/education-admin/review-state    -> /data/review-state:rw
/volume1/education-admin/review-queue    -> /data/review-queue:rw
/volume1/education-admin/corrections     -> /data/corrections:rw
/volume1/education-admin/approved-quarantine-annotations
                                             -> /data/approved-quarantine-annotations:ro
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
install -d -o root -g "$SEN_QA_SERVICE_GROUP" -m 0550 \
  "$SEN_QA_ANNOTATION_DIR"
install -d -o "$SEN_QA_SERVICE_USER" -g "$SEN_QA_REVIEW_GROUP" -m 2750 \
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
find "$SEN_QA_ANNOTATION_DIR" -type f \
  -exec chown root:"$SEN_QA_SERVICE_GROUP" {} + \
  -exec chmod 0440 {} +

test -f "$SEN_QA_QUARANTINE_SIDECAR"
chown "$SEN_QA_SERVICE_USER:$SEN_QA_SERVICE_GROUP" \
  "$SEN_QA_QUARANTINE_SIDECAR"
chmod 0600 "$SEN_QA_QUARANTINE_SIDECAR"
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

다음은 root가 실행하는 고정 launch 계약이다. registry SHA-256은 배포 증적에서
복사하며 reviewer 요청에서 받지 않는다. broker 디렉터리의 setgid 때문에 socket은
reviewer group을 상속하고 broker가 `0660`으로 제한한다. service process에는 Docker
socket을 mount하지 않는다.

```sh
SEN_QA_BROKER_DIGEST="${SEN_QA_BROKER_IMAGE##*@sha256:}"
test "$SEN_QA_BROKER_IMAGE" != "$SEN_QA_BROKER_DIGEST"
printf '%s\n' "$SEN_QA_BROKER_DIGEST" | grep -Eq '^[0-9a-f]{64}$' || {
  echo 'broker_image_digest_invalid' >&2; exit 2;
}
test -n "${SEN_QA_REVIEW_REGISTRY_SHA256:?}"
printf '%s\n' "$SEN_QA_REVIEW_REGISTRY_SHA256" | grep -Eq '^[0-9a-f]{64}$' || {
  echo 'registry_digest_invalid' >&2; exit 2;
}

export SEN_QA_SERVICE_UID="$(id -u "$SEN_QA_SERVICE_USER")"
export SEN_QA_SERVICE_GID="$(getent group "$SEN_QA_SERVICE_GROUP" | cut -d: -f3)"
export SEN_QA_REVIEW_GID="$(getent group "$SEN_QA_REVIEW_GROUP" | cut -d: -f3)"
export SEN_QA_REVIEWER_UID="$(id -u "$SEN_QA_REVIEWER_PROBE_USER")"
export SEN_QA_SECOND_REVIEWER_UID="$(id -u "$SEN_QA_SECOND_REVIEWER_PROBE_USER")"

"$SEN_QA_DOCKER" run -d --name "$SEN_QA_BROKER_CONTAINER" \
  --restart unless-stopped --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --user "$SEN_QA_SERVICE_UID:$SEN_QA_SERVICE_GID" \
  --group-add "$SEN_QA_REVIEW_GID" \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  -v "$SEN_QA_SOURCE_DIR:/data/source:ro" \
  -v "$SEN_QA_RAW_DIR:/data/raw:ro" \
  -v "$SEN_QA_CANONICAL_DIR:/data/canonical:ro" \
  -v "$SEN_QA_REVIEW_STATE_DIR:/data/review-state:rw" \
  -v "$SEN_QA_ANNOTATION_DIR:/data/approved-quarantine-annotations:ro" \
  -v "$SEN_QA_BROKER_DIR:/run/sen-qa:rw" \
  -v "/etc/passwd:/etc/passwd:ro" \
  --entrypoint /opt/venv/bin/python \
  "$SEN_QA_BROKER_IMAGE" -m src.ingestion.review_broker \
  --socket /run/sen-qa/review.sock \
  --database /data/review-state/review.sqlite3 \
  --registry /data/canonical/review-registry.json \
  --registry-sha256 "$SEN_QA_REVIEW_REGISTRY_SHA256" \
  --manifest-root /data/canonical/manifests \
  --quarantine-sidecar /data/review-state/parser-quarantine-resolutions.json \
  --annotation-manifest-root /data/approved-quarantine-annotations \
  --quarantine-reviewer-uid "$SEN_QA_REVIEWER_UID" \
  --quarantine-reviewer-uid "$SEN_QA_SECOND_REVIEWER_UID" \
  --annotation-owner-uid 0
```

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

남은 production blocker는 root 소유 broker의 실제 NAS 계정 통합 시험이다. 이를
닫힌 뒤에만 broker를 통해 `assert-ready`를 실행하고 release 증적에 registry hash,
이미지 digest, blocker count, 두 OS actor UID를 값 최소화 형태로 남긴다.

## 7. parser quarantine 해소 sidecar

`parser-quarantines.jsonl`은 case review queue와 별개의 선행 검수 대상이다.
`src.ingestion.quarantine_review`의 draft는 원본 package를 변경하지 않고 release,
registry, manifest, raw authority, parser authority, quarantine file SHA-256과 count를
묶는다. 같은 JSONL 행이 반복돼도 각 occurrence는 서로 다른 ordinal과 opaque ID를
가진다. sidecar에는 본문을 넣지 않고 기존 page, bbox, text SHA-256만 보존한다.

허용 disposition은 다음뿐이다.

- `unresolved`: 아직 판단하지 않았거나 upstream OCR 재추출이 필요한 상태
- `confirmed_noncase`: 해당 occurrence 전체가 사례가 아님을 사람이 확인한 상태
- `corrected`: 원래 occurrence의 모든 span을 정확히 한 번씩 hierarchy, case 또는
  fragment role에 배정한 상태

upstream `page-extraction-failed`, `page-render-failed`, `ocr-adapter-failed`,
`ocr-provenance-invalid`는 사람이 닫을 수 없다. 원본을 같은 OCR authority 계약으로
재추출하고 새 parser/quarantine authority를 발급해야 한다. `corrected`는 새 문자열을
받지 않으며, role 값은 exact source span의 normalized projection에서만 파생한다.
span 일부만 배정하거나 중복 배정하면 실패한다.

운영 순서는 반드시 분리한다.

1. ingestion service가 `create_resolution_draft`로 canonical bytes를 만든다.
2. root 소유 broker가 `SO_PEERCRED` actor를 `uid:<uid>:<account>`로 만들고 고정된
   reviewer UID allowlist를 확인한 뒤 건별 `append_resolution_event`를 호출한다.
   reviewer는 2,257개 span이나 reviewer ID를 socket body에 넣지 않는다. operator가
   root 소유 annotation root에 `0440` canonical manifest를 동결하고 SHA-256을
   전달한 뒤, client는 아래 네 필드만 보낸다.

   ```json
   {"operation":"resolve-quarantine","expected_head_sha256":"<current-sidecar-sha256>","annotation_manifest_id":"decision-0001.json","annotation_manifest_sha256":"<approved-manifest-sha256>"}
   ```

   broker는 absolute root부터 모든 directory component를 `O_DIRECTORY|O_NOFOLLOW`로
   descriptor-walk하고, 고정된 manifest와 sidecar parent descriptor를 operation이
   끝날 때까지 유지한다. manifest는 size-bounded/stable read하며 sidecar 전용 `0600`
   lock 아래 expected head를 compare-and-swap한다. 성공 시 hash-chain event 하나를
   추가한 canonical sidecar를 같은 held parent의 `O_EXCL` `0600` 임시 파일에 기록하고
   file `fsync`, dirfd 기반 atomic replace, directory `fsync` 순서로 내구화한다.
   stale head와 replay는 값 없는 code로 실패하며 기존 sidecar를 부분 갱신하지 않는다.
3. broker를 중지하고 sidecar를 `0600` 일반 파일로 동결한 뒤, 별도 operator가
   SHA-256을 release 증적에 기록한다. 검증 명령이 같은 실행에서 expected SHA를
   계산해 넘기면 외부 seal로 인정하지 않는다.
4. 다음 실행에서 `load_resolution_authority(path, expected_sha256=<증적값>)`로만
   sidecar를 연다. symlink, FIFO, mode drift, noncanonical JSON, duplicate key,
   broken event chain, authority drift는 값 없이 실패한다.
5. 모든 occurrence가 terminal일 때만 `reparse_with_resolution`을 실행한다. 함수는
   2020~2025 닫힌 dispatch, exact page/source binding, 전체 occurrence 재대조를 하고
   parser quarantine이 하나라도 다시 나오면 실패한다.
6. 성공한 parse 결과는 기존 package를 덮어쓰지 않고 새 빈 release root에
   재-stage한다. 모든 새 case는 다시 `needs_review`, search/answer ineligible로
   시작하며 사람 case review를 별도로 완료해야 한다.

재-stage는 `prepare_resolved_review_corpus`만 사용한다. 이 경계는 기존
`VerifiedParseRun`과 quarantine 포함 `PreparedReviewBatch`를 재생성해 서로
대조하고, 전달된 결과를 내부 `reparse_with_resolution` 결과와 canonical byte
단위로 비교한다. 새 parser authority v2는 이전 parser authority, 이전 quarantine
SHA-256, 이전 registry, manifest/raw authority, 외부 resolution SHA-256과 문서별
input/resolved-parse SHA-256을 함께 묶는다. 기존 review database, decision snapshot,
attestation, candidate 또는 sampling authority는 복사하지 않는다.

resolved package는 `sen-qa-ingestion-evidence/v4`,
`sen-qa-review-package/v4`, `sen-qa-review-ready-attestation/v3`을 사용하고
`parser-quarantine-resolutions.json`을 `0600`으로 포함한다. evidence와 ready
attestation은 그 파일의 외부 SHA-256을 함께 봉인한다. 새 release root는 완전히
비어 있어야 하며, sidecar 누락·변조, unresolved occurrence, 과거 authority drift,
재parse quarantine 재발 중 하나라도 있으면 staging/export/finalizer가 모두
실패한다.

broker operation과 value-free event envelope는 코드와 로컬 동시성/crash 회귀시험에
연결돼 있다. 남은 production blocker는 이 launch 계약을 NAS에 설치한 뒤 두 실제 NAS reviewer UID로 같은 expected head에 동시에 요청해 정확히 한 요청만 성공하고,
다른 요청은 `resolution_head_stale`이며 sidecar event가 정확히 하나인지 확인하는
계정/ACL 통합 시험이다. 이 시험 전의 sidecar는 개발 증적일 뿐 human-ready나
release-ready로 표시하면 안 된다. 기존 quarantine 포함 v3 package와 운영 alias는
그대로 fail closed 상태를 유지한다.
