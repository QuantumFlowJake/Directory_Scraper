FROM python:3.12-slim

# Set WITH_PLAYWRIGHT=true at build time to enable --render / JS-rendered sites.
# It pulls Chromium + system deps (~400MB), so it's off by default.
ARG WITH_PLAYWRIGHT=false

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCRAPER_DB=/data/scraper.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN if [ "$WITH_PLAYWRIGHT" = "true" ]; then \
        pip install --no-cache-dir playwright \
        && playwright install --with-deps chromium ; \
    fi

COPY scrape_directory.py service.py ./
RUN mkdir -p /data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/ || exit 1

# Single worker by default: async job execution uses in-process BackgroundTasks.
# Job/profile STATE lives in SQLite, so polling works across workers, but a job
# running when its worker restarts is lost. Scale via RQ/Redis before bumping this.
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
