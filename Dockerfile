FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATLAS_LITE_HOST=0.0.0.0 \
    ATLAS_LITE_PORT=8090 \
    ATLAS_LITE_LOG_LEVEL=WARNING \
    KITE_CREDENTIALS_PATH=/secrets/kite_credentials

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

COPY atlas_lite/ atlas_lite/
COPY static/ static/
COPY scripts/ scripts/

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health')"

CMD ["python", "-m", "atlas_lite"]
