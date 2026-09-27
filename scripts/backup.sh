#!/usr/bin/env bash
# pg_dump 自定义格式备份，保留最近 GS_BACKUP_KEEP_DAYS 天（默认 14）。
# 可选：设置 GS_BACKUP_REMOTE（rclone 远端，例如 "r2:groundswell-backups"）同步到对象存储。
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p backups
KEEP="${GS_BACKUP_KEEP_DAYS:-14}"
stamp="$(date +%Y%m%d-%H%M%S)"
out="backups/groundswell-${stamp}.dump"

echo "[backup] $(date -Is) → ${out}"
# TimescaleDB 的 hypertable 会产生循环外键警告，属正常现象
docker compose exec -T db pg_dump -U groundswell -d groundswell -Fc -Z 6 > "${out}.part"
mv "${out}.part" "${out}"

# 同时备份报告与配置（体积很小）
tar -czf "backups/files-${stamp}.tar.gz" reports configs .env 2>/dev/null || true
chmod 600 "backups/files-${stamp}.tar.gz" "${out}"

find backups -name 'groundswell-*.dump' -mtime +"${KEEP}" -delete
find backups -name 'files-*.tar.gz' -mtime +"${KEEP}" -delete

if [[ -n "${GS_BACKUP_REMOTE:-}" ]] && command -v rclone >/dev/null; then
  rclone copy backups "${GS_BACKUP_REMOTE}" --include 'groundswell-*.dump' --include 'files-*.tar.gz' --max-age 2d
  echo "[backup] 已同步到 ${GS_BACKUP_REMOTE}"
fi

size="$(du -h "${out}" | cut -f1)"
echo "[backup] 完成 ${size}"
