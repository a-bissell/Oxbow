# Oxide Dielectric Triage — application image.
# Build from the repo root:  docker build -f docker/Dockerfile -t oxide-triage .
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OXIDE_TRIAGE_CACHE=/data/cache.sqlite

RUN useradd --create-home --uid 10001 triage
WORKDIR /app

# Dependencies first so source edits do not invalidate the layer.
COPY pyproject.toml README.md ./
COPY oxide_triage ./oxide_triage
COPY config ./config
COPY eval ./eval
RUN pip install --upgrade pip && pip install ".[web,llm,mcp]"

RUN mkdir -p /data && chown -R triage:triage /data /app
USER triage
VOLUME ["/data"]
EXPOSE 8000 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/status', timeout=3).status==200 else 1)"

# Default: the web app (assistant, results canvas, admin panel). Override the command for the
# CLI, e.g.  docker compose run --rm app oxide-triage query "..." --profile conservative
CMD ["oxide-triage", "serve", "--host", "0.0.0.0", "--port", "8000"]
