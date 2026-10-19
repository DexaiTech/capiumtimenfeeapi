# Capium Time & Fees API and worker. One image, two targets:
#   docker build -t capium-api .                      # the API (last stage)
#   docker build --target worker -t capium-worker .   # the background worker
FROM python:3.14-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv/app

# tzdata comes from requirements.txt, so zoneinfo works without the OS package.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

RUN useradd --system --uid 10001 --no-create-home app

# Uploaded files (ticket attachments): a volume that the unprivileged user can write to. Mount
# persistent storage here, or the files are lost with the container (docs/deployment.md).
ENV STORAGE_DIR=/var/lib/capium/storage
RUN mkdir -p "$STORAGE_DIR" && chown app:app "$STORAGE_DIR"
VOLUME ["/var/lib/capium/storage"]
USER app

# Background worker (emails, invoice emails with PDF, partitions, retention).
FROM base AS worker
CMD ["python", "-m", "app.workers.main"]

# API: the last stage, so a plain `docker build` produces it. Behind a reverse proxy, limit
# --forwarded-allow-ips to the proxy's address so client IPs (rate limits, audit) are real.
FROM base AS api
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live')"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
