#!/usr/bin/env bash
# Linux / macOS 本地开发助手：./scripts/dev.sh <up|down|test|lint|fmt|hourly|report|serve>
set -euo pipefail
cd "$(dirname "$0")/.."

cmd="${1:-help}"
shift || true

case "$cmd" in
  up)
    [[ -f .env ]] || { cp .env.example .env; echo "已生成 .env，请按需修改"; }
    docker compose up -d db rsshub dailyhot
    uv sync --all-extras
    uv run gs db upgrade
    uv run gs config check
    ;;
  down) docker compose down ;;
  test)
    port="$(grep -E '^GS_DB_HOST_PORT=' .env 2>/dev/null | cut -d= -f2)"
    port="${port:-5432}"
    docker compose exec -T db psql -U groundswell -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='groundswell_test'" | grep -q 1 || \
      docker compose exec -T db psql -U groundswell -d postgres -c "CREATE DATABASE groundswell_test"
    GS_TEST_DATABASE_URL="postgresql+psycopg://groundswell:groundswell@127.0.0.1:${port}/groundswell_test" \
      uv run pytest -q "$@"
    ;;
  lint) uv run ruff check . && uv run ruff format --check . ;;
  fmt) uv run ruff check --fix . && uv run ruff format . ;;
  hourly) uv run gs run hourly ;;
  report) uv run gs report daily ;;
  serve) uv run gs serve ;;
  *) sed -n '2p' "$0" ;;
esac
