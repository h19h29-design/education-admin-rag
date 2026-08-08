FROM --platform=linux/amd64 python:3.11-slim@sha256:78b39ef14d8e2b4d71f8dc304f1328c37df95fe0ef99477c2ae6bd3d03784553

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir uv==0.11.17

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra ocr

COPY src ./src

CMD ["uv", "run", "python", "-m", "src.cli"]
