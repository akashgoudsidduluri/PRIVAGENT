# PrivAgent backend — FastAPI orchestration hub.
#
# Build from the repository root:
#   docker build -f backend/Dockerfile -t privagent-backend .
# Run standalone:
#   docker run --rm -p 127.0.0.1:8000:8000 --env-file backend/.env \
#     -v privagent-data:/app/backend/data privagent-backend
#
# Notes:
# - The local dev server binds 127.0.0.1 (see backend/run_server.py). A
#   container must listen on 0.0.0.0 for its published port to work, so the
#   CMD below binds 0.0.0.0 — the local-only property is preserved by
#   publishing the port on the host loopback only (see docker-compose.yml:
#   "127.0.0.1:8000:8000"). Do not publish 8000 on all host interfaces.
# - backend/.env is NEVER copied into the image (.dockerignore). Secrets
#   reach the container via --env-file / compose `env_file`, and are then
#   migrated into the encrypted store on first startup (Task 2 behaviour).
# - The encrypted secret store, SQLite DB and master key live in
#   backend/data/, which is a volume in compose so state survives rebuilds.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app/backend

# Pinned requirements first, so dependency layers are cached across
# code-only rebuilds.
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/scripts ./scripts
COPY backend/pytest.ini ./pytest.ini
# run_server.py binds 127.0.0.1 and is a local-dev helper; it is not used
# by this image (uvicorn is started directly in CMD).

RUN groupadd -r privagent && useradd -r -g privagent -d /app privagent \
    && mkdir -p /app/backend/data \
    && chown -R privagent:privagent /app

USER privagent

EXPOSE 8000

# /api/health is auth-exempt by design (see backend/app/main.py) and
# returns {"status": "ok", ...} while the app is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)); raise SystemExit(0 if d.get('status')=='ok' else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
