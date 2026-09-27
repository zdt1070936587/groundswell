#!/usr/bin/env bash
# 在 Linux VPS 上部署 / 升级 Groundswell。在仓库根目录执行：
#   ./scripts/deploy.sh            # 首次部署或升级
#   ./scripts/deploy.sh --pull     # 先 git pull 再部署
#   GS_PROFILES="dashboard web" ./scripts/deploy.sh   # 同时启动 Streamlit / Next.js
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

log() { printf '\033[32m[deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[deploy]\033[0m %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "未安装 docker（参考 docs/deploy.md）"
docker compose version >/dev/null 2>&1 || die "需要 docker compose v2 插件"

if [[ "${1:-}" == "--pull" ]]; then
  log "git pull"
  git pull --ff-only
fi

# ---------------------------------------------------------------- .env
if [[ ! -f .env ]]; then
  cp .env.example .env
  pw="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32)"
  token="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)"
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=${pw}/" .env
  sed -i "s/^GS_API_TOKEN=.*/GS_API_TOKEN=${token}/" .env
  chmod 600 .env
  die ".env 已从模板生成（已随机化数据库密码与 API token）。请填写钉钉 webhook、LLM key 等后重新执行。"
fi
chmod 600 .env
grep -q '^POSTGRES_PASSWORD=groundswell$' .env && die "请修改 .env 中的默认数据库密码"

mkdir -p reports backups

# ---------------------------------------------------------------- 构建与启动
profiles=()
for p in ${GS_PROFILES:-}; do profiles+=(--profile "$p"); done

log "构建镜像"
docker compose "${profiles[@]}" build

log "启动基础服务（db / rsshub / dailyhot）"
docker compose up -d db rsshub dailyhot

log "等待数据库就绪"
for _ in $(seq 1 60); do
  if docker compose exec -T db pg_isready -U groundswell -d groundswell >/dev/null 2>&1; then break; fi
  sleep 2
done

log "数据库迁移 + 配置校验"
docker compose run --rm app gs db upgrade
docker compose run --rm app gs config check

log "启动应用"
docker compose "${profiles[@]}" up -d

# ---------------------------------------------------------------- 夜间备份
cron_line="30 4 * * * cd ${ROOT} && ./scripts/backup.sh >> ${ROOT}/backups/backup.log 2>&1"
if command -v crontab >/dev/null; then
  if ! crontab -l 2>/dev/null | grep -Fq "scripts/backup.sh"; then
    (crontab -l 2>/dev/null; echo "$cron_line") | crontab -
    log "已安装每日 04:30 备份 cron"
  fi
else
  log "未找到 crontab，请手动配置备份：$cron_line"
fi

# ---------------------------------------------------------------- 健康检查
log "健康检查"
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
    curl -fsS http://127.0.0.1:8000/healthz; echo
    log "部署完成。查看状态：docker compose exec app gs status"
    exit 0
  fi
  sleep 3
done
docker compose logs --tail 80 app
die "应用未通过健康检查"
