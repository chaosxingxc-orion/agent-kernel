FROM python:3.12-slim AS base

WORKDIR /app

RUN addgroup --system kernel && adduser --system --ingroup kernel kernel

COPY pyproject.toml ./
RUN pip install --no-cache-dir . \
    && pip install --no-cache-dir ".[temporal]"

COPY agent_kernel/ agent_kernel/

USER kernel

EXPOSE 8400

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8400/health/liveness')"

# Use create_app_temporal (real Temporal substrate) as the default entrypoint.
# Set AGENT_KERNEL_TEMPORAL_HOST to point at your Temporal frontend.
# Mount a volume at /app/data for durable SQLite storage.
# For local in-memory mode (dev/testing only) override with:
#   --factory agent_kernel.service.http_server:create_app_default
ENTRYPOINT ["python", "-m", "uvicorn", "agent_kernel.service.http_server:create_app_temporal", \
    "--host", "0.0.0.0", "--port", "8400", "--factory"]
