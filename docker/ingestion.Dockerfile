FROM --platform=linux/amd64 python:3.11-slim@sha256:78b39ef14d8e2b4d71f8dc304f1328c37df95fe0ef99477c2ae6bd3d03784553 AS builder

WORKDIR /work

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONPATH=/work

RUN python -m pip install --no-cache-dir --require-hashes \
    "uv @ https://files.pythonhosted.org/packages/0e/51/b75808766f895248553c6370968509cd4f726e6943e310a8f7a171036ad0/uv-0.11.17-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl#sha256=9da839e5a491c9a701d7d327a199cafc76ac27a03ac84fd2a8d4bf32c3af2448"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra ocr --no-dev

COPY src ./src
COPY config/models.lock.json ./config/models.lock.json
COPY data/manifests/sen_qa_sources.json ./data/manifests/sen_qa_sources.json
COPY docker/prepare_ocr_models.py /build/prepare_ocr_models.py

RUN /opt/venv/bin/python /build/prepare_ocr_models.py \
      --lock config/models.lock.json \
      --output /opt/models/paddleocr \
    && /opt/venv/bin/python -m src.cli validate-ocr-models \
      --lock config/models.lock.json \
      --model-root /opt/models/paddleocr

FROM --platform=linux/amd64 python:3.11-slim@sha256:78b39ef14d8e2b4d71f8dc304f1328c37df95fe0ef99477c2ae6bd3d03784553 AS runtime

WORKDIR /work

RUN set -eux; \
    . /etc/os-release; \
    test "$VERSION_ID" = "13"; \
    test "$VERSION_CODENAME" = "trixie"; \
    DEBIAN_SNAPSHOT=20260803T000000Z; \
    grep -Fqx "# http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
      /etc/apt/sources.list.d/debian.sources; \
    grep -Fqx "# http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
      /etc/apt/sources.list.d/debian.sources; \
    sed -ri \
      "s|^URIs: http://deb.debian.org/debian$|URIs: https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/|; s|^URIs: http://deb.debian.org/debian-security$|URIs: https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/|" \
      /etc/apt/sources.list.d/debian.sources; \
    grep -Fqx "URIs: https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/" \
      /etc/apt/sources.list.d/debian.sources; \
    grep -Fqx "URIs: https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/" \
      /etc/apt/sources.list.d/debian.sources; \
    ! grep -Fq "URIs: http://deb.debian.org/" \
      /etc/apt/sources.list.d/debian.sources; \
    apt-get -o Acquire::Check-Valid-Until=false update; \
    apt-get -o Acquire::Check-Valid-Until=false install -y --no-install-recommends \
      libgl1 \
      libglib2.0-0t64 \
      libgomp1; \
    rm -rf /var/lib/apt/lists/*

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/work \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/ocr-runtime \
    PADDLE_HOME=/tmp/paddle-home \
    PADDLE_PDX_CACHE_HOME=/tmp/paddlex-cache \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1 \
    SEN_QA_OCR_MODEL_ROOT=/opt/models/paddleocr

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models/paddleocr /opt/models/paddleocr
COPY --from=builder /work/src /work/src
COPY --from=builder /work/config /work/config
COPY --from=builder /work/data /work/data

USER 65532:65532

ENTRYPOINT ["/opt/venv/bin/python", "-m", "src.cli"]
CMD ["--help"]
