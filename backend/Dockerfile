# Maintain the same repository depth used by app.data.DB_PATH:
# /workspace/backend/app/data.py -> /workspace/data/demo.sqlite
FROM python:3.14-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /usr/local/bin/

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1 \
    UV_NO_CACHE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/workspace/backend/.venv/bin:$PATH"

WORKDIR /workspace/backend
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app

RUN useradd --uid 10001 --create-home agentuser \
    && mkdir -p /workspace/data /workspace/opsdata \
    && chown agentuser:agentuser /workspace/data /workspace/opsdata
USER agentuser
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
