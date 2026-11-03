# Tom, the Empório da Música agent: web app + HTTP API.
# The model is NOT inside the image: it talks to any OpenAI-compatible server
# (LM Studio on the host by default, see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VAR_DIR=/app/var

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml ./
COPY emporio ./emporio
COPY data ./data
COPY evals ./evals

# Run as an unprivileged user; var/ (SQLite + embedding cache) is the only writable path.
RUN useradd --create-home --uid 10001 tom && mkdir -p /app/var && chown tom /app/var
USER tom
VOLUME ["/app/var"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["python", "-m", "emporio", "serve", "--host", "0.0.0.0", "--port", "8000"]
