#!/usr/bin/env bash
# 从 pg_dump 备份恢复（会清空当前数据库！）
#   ./scripts/restore.sh backups/groundswell-20260927-043000.dump
set -euo pipefail

cd "$(dirname "$0")/.."
dump="${1:?用法: restore.sh <dump 文件>}"
[[ -f "$dump" ]] || { echo "找不到 $dump" >&2; exit 1; }

read -r -p "将清空并用 ${dump} 覆盖数据库 groundswell，输入 yes 继续: " ok
[[ "$ok" == "yes" ]] || exit 1

docker compose stop app worker dashboard web 2>/dev/null || true
docker compose up -d db

psql() { docker compose exec -T db psql -U groundswell -v ON_ERROR_STOP=1 "$@"; }

psql -d postgres -c "DROP DATABASE IF EXISTS groundswell WITH (FORCE)"
psql -d postgres -c "CREATE DATABASE groundswell"
psql -d groundswell -c "CREATE EXTENSION IF NOT EXISTS timescaledb"
psql -d groundswell -c "SELECT timescaledb_pre_restore()"
docker compose exec -T db pg_restore -U groundswell -d groundswell --no-owner < "$dump" || \
  echo "[restore] pg_restore 有警告（timescaledb 目录表常见），继续"
psql -d groundswell -c "SELECT timescaledb_post_restore()"

docker compose up -d app
echo "[restore] 完成；用 docker compose exec app gs db current 确认迁移版本"
