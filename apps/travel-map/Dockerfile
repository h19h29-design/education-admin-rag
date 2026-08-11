FROM python:3.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_PROGRESS=1

WORKDIR /app

# The installer version and lockfile make dependency resolution reproducible.
RUN pip install --no-cache-dir uv==0.11.17
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

ENV ENVIRONMENT=production \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/app /app/app
COPY --chown=appuser:appuser resources/rules /app/resources/rules
COPY --chown=appuser:appuser resources/geodata/manifest.json /app/resources/geodata/manifest.json
COPY --chown=appuser:appuser resources/geodata/seoul.geojson /app/resources/geodata/seoul.geojson
COPY --chown=appuser:appuser resources/geodata/seoul-plus-12km.geojson /app/resources/geodata/seoul-plus-12km.geojson

# A caller must name the snapshot selected by current.json. The image contains
# only that approved normalized snapshot; a missing, mismatched, or invalid one
# fails the build before any image is published.
ARG SNAPSHOT_ID
COPY --chown=appuser:appuser resources/institution-snapshots/current.json /app/resources/institution-snapshots/current.json
COPY --chown=appuser:appuser resources/institution-snapshots/${SNAPSHOT_ID}/ /app/resources/institution-snapshots/${SNAPSHOT_ID}/
RUN test -n "$SNAPSHOT_ID" \
    && test "$(python -c 'import json; print(json.load(open("/app/resources/institution-snapshots/current.json", encoding="utf-8"))["snapshotId"])')" = "$SNAPSHOT_ID" \
    && python -c 'from app.institutions.snapshot import verify_snapshot; verify_snapshot("/app/resources/institution-snapshots")'

USER appuser

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "from urllib.request import urlopen; response = urlopen('http://127.0.0.1:8080/healthz', timeout=2); raise SystemExit(response.status != 200)"

# create_app builds production dependencies during the lifespan startup. Invalid
# credentials or a missing/invalid approved snapshot therefore prevent serving.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
