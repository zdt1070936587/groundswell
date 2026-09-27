FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project \
      --extra llm --extra nlp --extra trends --extra dashboard --extra ml --extra queue

COPY src ./src
COPY configs ./configs
COPY alembic.ini ./
RUN uv sync --frozen --no-dev \
      --extra llm --extra nlp --extra trends --extra dashboard --extra ml --extra queue

EXPOSE 8000 8501
CMD ["gs", "serve"]
