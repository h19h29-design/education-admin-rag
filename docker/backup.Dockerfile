FROM --platform=linux/amd64 python:3.11-slim@sha256:78b39ef14d8e2b4d71f8dc304f1328c37df95fe0ef99477c2ae6bd3d03784553 AS builder

WORKDIR /build

COPY config/backup-tools.lock.json /build/config/backup-tools.lock.json
COPY docker/prepare_backup_tools.py /build/prepare_backup_tools.py

RUN python /build/prepare_backup_tools.py \
      --lock /build/config/backup-tools.lock.json \
      --output /opt/backup-tools

FROM --platform=linux/amd64 python:3.11-slim@sha256:78b39ef14d8e2b4d71f8dc304f1328c37df95fe0ef99477c2ae6bd3d03784553 AS runtime

COPY --from=builder /opt/backup-tools/age /usr/local/bin/age
COPY --from=builder /opt/backup-tools/minisign /usr/local/bin/minisign

USER 65532:65532

CMD ["age", "--version"]
