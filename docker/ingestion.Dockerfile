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

RUN /opt/venv/bin/python -m src.cli prepare-ocr-models \
      --lock config/models.lock.json \
      --output /opt/models/paddleocr \
    && /opt/venv/bin/python -m src.cli validate-ocr-models \
      --lock config/models.lock.json \
      --model-root /opt/models/paddleocr

FROM --platform=linux/amd64 python:3.11-slim@sha256:78b39ef14d8e2b4d71f8dc304f1328c37df95fe0ef99477c2ae6bd3d03784553 AS runtime

WORKDIR /work

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/work \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/ocr-runtime \
    PADDLE_HOME=/opt/models/paddleocr \
    PADDLE_PDX_CACHE_HOME=/opt/models/paddleocr \
    SEN_QA_OCR_MODEL_ROOT=/opt/models/paddleocr

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models/paddleocr /opt/models/paddleocr
COPY --from=builder /work/src /work/src
COPY --from=builder /work/config /work/config
COPY --from=builder /work/data /work/data

USER 65532:65532

CMD ["/opt/venv/bin/python", "-m", "src.cli"]
