FROM --platform=linux/amd64 python:3.11-slim@sha256:78b39ef14d8e2b4d71f8dc304f1328c37df95fe0ef99477c2ae6bd3d03784553 AS builder

WORKDIR /work

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONPATH=/work

RUN python -m pip install --no-cache-dir --require-hashes \
    "uv @ https://files.pythonhosted.org/packages/0e/51/b75808766f895248553c6370968509cd4f726e6943e310a8f7a171036ad0/uv-0.11.17-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl#sha256=9da839e5a491c9a701d7d327a199cafc76ac27a03ac84fd2a8d4bf32c3af2448"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra index --no-dev

COPY src ./src
COPY config/models.lock.json ./config/models.lock.json
COPY docker/prepare_embedding_model.py /build/prepare_embedding_model.py

ARG BGE_M3_REVISION=5617a9f61b028005a4858fdac845db406aefb181
ARG BGE_M3_LOCK_SHA256=db9c89ff6e48b94ec8d6013f003f42e1ee824a602542e2a0d373b8a5ea0a12da
RUN install -d -m 0755 /opt/models \
    && test "$BGE_M3_REVISION" = "5617a9f61b028005a4858fdac845db406aefb181" \
    && test "$BGE_M3_LOCK_SHA256" = "db9c89ff6e48b94ec8d6013f003f42e1ee824a602542e2a0d373b8a5ea0a12da" \
    && /opt/venv/bin/python /build/prepare_embedding_model.py \
         --lock config/models.lock.json \
         --output /opt/models/bge-m3 \
         --expected-lock-sha256 "$BGE_M3_LOCK_SHA256" \
    && /opt/venv/bin/python -m src.cli verify-embedding-models \
         --lock config/models.lock.json \
         --model-root /opt/models/bge-m3 \
         --expected-lock-sha256 "$BGE_M3_LOCK_SHA256" \
    && find /opt/models/bge-m3 -type d -exec chmod 0555 {} + \
    && find /opt/models/bge-m3 -type f -exec chmod 0444 {} +

FROM --platform=linux/amd64 python:3.11-slim@sha256:78b39ef14d8e2b4d71f8dc304f1328c37df95fe0ef99477c2ae6bd3d03784553 AS runtime

WORKDIR /work

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/work \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/index-runtime \
    HF_HOME=/tmp/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    SEN_QA_EMBEDDING_MODEL_ROOT=/opt/models/bge-m3 \
    SEN_QA_EMBEDDING_LOCK_SHA256=db9c89ff6e48b94ec8d6013f003f42e1ee824a602542e2a0d373b8a5ea0a12da

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models/bge-m3 /opt/models/bge-m3
COPY --from=builder /work/src /work/src
COPY --from=builder /work/config /work/config

USER 65532:65532

CMD ["/opt/venv/bin/python", "-m", "src.cli"]
