# Builder stage - compiles Python dependencies
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install uv and sync dependencies
RUN pip install uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Runtime base stage
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r -g 1000 appuser && \
    useradd -r -u 1000 -g appuser -m appuser

WORKDIR /app
RUN chown appuser:appuser /app

# Install uv for runtime commands
RUN pip install uv

# Copy virtual environment from builder
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

# Switch to non-root user
USER appuser

# Set up environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV MODELS_DIR=/home/appuser/models
ENV AUDIO_WORKSPACE_DIR=/home/appuser/audio_workspace

# Copy application code
COPY --chown=appuser:appuser . .

# Create directories in user space
RUN mkdir -p /home/appuser/audio_workspace /home/appuser/models

# Web server stage
FROM base AS web
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uv", "run", "uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# Worker stage
FROM base AS worker
CMD ["uv", "run", "python", "worker.py"]

# Default stage
FROM web AS default
