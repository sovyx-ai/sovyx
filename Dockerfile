# Sovyx — Sovereign Minds Engine
# Multi-stage build: build deps → slim runtime
# Supports: linux/amd64, linux/arm64

FROM python:3.12-slim AS build

WORKDIR /app
RUN pip install --no-cache-dir uv==0.10.11

# Copy everything needed for dependency resolution
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# Install all deps + package in one shot
RUN uv sync --no-dev --frozen

# ── Runtime ──────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Sovyx" \
      org.opencontainers.image.description="Sovereign Minds Engine" \
      org.opencontainers.image.source="https://github.com/sovyx-ai/sovyx" \
      org.opencontainers.image.licenses="AGPL-3.0" \
      org.opencontainers.image.version="0.5.0"

# Create non-root user
RUN groupadd --system sovyx && \
    useradd --system --gid sovyx --create-home sovyx

WORKDIR /app

# Copy virtual environment from build stage
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    SOVYX_DATA_DIR="/data" \
    PYTHONUNBUFFERED=1

# Create data directory
RUN mkdir -p /data && chown sovyx:sovyx /data
VOLUME ["/data"]

USER sovyx

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["sovyx", "doctor"] || exit 1

ENTRYPOINT ["sovyx"]
CMD ["start", "--foreground"]
