FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

LABEL org.opencontainers.image.source="https://github.com/penz6/RA-Draft"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/ra_draft.db \
    PORT=8000 \
    WEB_THREADS=64

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data

COPY --chown=appuser:appuser . .

VOLUME ["/data"]
USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8000/healthz',headers={'Host':os.environ['PUBLIC_HOST']}); urllib.request.urlopen(r,timeout=3)" || exit 1

# One process is intentional: the lightweight SSE broker is in memory. The
# thread count leaves room for normal requests while clients keep event streams
# open. Use a shared pub/sub service before scaling to multiple app replicas.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8000} --worker-class gthread --workers 1 --threads ${WEB_THREADS:-64} --timeout 0 --access-logfile - --error-logfile - main:app"]
