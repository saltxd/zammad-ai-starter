FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so editing the worker doesn't re-resolve the world.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115,<1.0" \
      "uvicorn[standard]>=0.30,<1.0" \
      "httpx>=0.27,<1.0" \
      "anthropic>=0.40"

COPY triage_worker ./triage_worker

RUN useradd --create-home --uid 10001 worker
USER worker

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "triage_worker.main:app", "--host", "0.0.0.0", "--port", "8000"]
