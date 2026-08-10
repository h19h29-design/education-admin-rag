FROM python:3.12-slim-bookworm

ENV PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv \
    && useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY app ./app

RUN uv sync --frozen --no-dev

USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
