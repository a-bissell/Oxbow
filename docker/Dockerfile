# Oxide Dielectric Triage — application image.
# Build from the repo root:  docker build -f docker/Dockerfile -t oxide-triage .
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OXIDE_TRIAGE_CACHE=/data/cache.sqlite \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

RUN useradd --create-home --uid 10001 triage
WORKDIR /app

# Dependencies first so source edits do not invalidate the layer.
COPY pyproject.toml README.md ./
COPY oxide_triage ./oxide_triage
COPY config ./config
COPY eval ./eval
RUN pip install --upgrade pip && pip install ".[app,llm,mcp]"

RUN mkdir -p /data && chown -R triage:triage /data /app
USER triage
VOLUME ["/data"]
EXPOSE 8501 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).status==200 else 1)"

# Default: the Streamlit front end. Override the command for the CLI, e.g.
#   docker compose run --rm app oxide-triage query "..." --profile conservative
CMD ["streamlit", "run", "oxide_triage/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
