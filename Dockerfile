FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 monitor

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY tools ./tools
COPY tests ./tests
COPY scripts ./scripts
COPY pod_spec.txt ./pod_spec.txt
# [dev] pulls in pytest so the UI can run the suite in ENVIRONMENT=test.
RUN pip install --no-cache-dir ".[dev]" \
    && chmod +x scripts/render-start.sh

USER monitor
EXPOSE 8000

HEALTHCHECK --interval=5s --timeout=5s --start-period=20s --retries=20 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/livez')"]

ENTRYPOINT ["python", "-m", "app.runtime"]
CMD ["api"]
