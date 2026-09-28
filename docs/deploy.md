# 部署指南：单台 Linux VPS

目标形态：一台 2C4G（最低）/ 4C8G（推荐，开启本地 embedding 时）的 Linux VPS，Docker Compose 跑全部服务，
对外只暴露反向代理的 443 端口。

## 1. 服务器准备

```bash
# Ubuntu 22.04 / Debian 12
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
sudo apt-get install -y git curl cron

# 防火墙：只放行 SSH 与 HTTPS；compose 里所有端口都绑定在 127.0.0.1
sudo ufw allow OpenSSH && sudo ufw allow 443/tcp && sudo ufw enable
```

> 海外 VPS 访问国内平台（微博/抖音）可能被风控，国内 VPS 访问 Reddit/Google Trends 需要代理（`GS_HTTP_PROXY`）。
> 推荐香港/新加坡节点，两边都能直连。

## 2. 首次部署

```bash
git clone <repo> groundswell && cd groundswell
./scripts/deploy.sh          # 首次执行会生成 .env（随机数据库密码与 API token）并退出
vim .env                     # 填写钉钉 webhook/secret、LLM key、Reddit/GitHub 凭证、WEIBO_COOKIES
./scripts/deploy.sh          # 构建镜像 → 启动 db/rsshub/dailyhot → 迁移 → 启动 app → 安装备份 cron → 健康检查
```

`deploy.sh` 做的事：

1. 校验 `.env` 存在且未使用默认密码，权限设为 600；
2. `docker compose build`，启动基础服务并等待数据库就绪；
3. `gs db upgrade` + `gs config check`（配置有误会中止部署）；
4. `docker compose up -d`（`GS_PROFILES` 选择可选组件：`web` = Next.js 前端，`dashboard` = Streamlit，
   `scale` = Redis + worker，如 `GS_PROFILES="web scale"`）；
5. 安装每日 04:30 的 `scripts/backup.sh` cron；
6. 轮询 `/healthz`，失败时打印应用日志。

升级：`./scripts/deploy.sh --pull`（迁移在应用启动前自动执行，向前兼容的迁移可零停机）。

> 容器内的 `GS_DATABASE_URL` 由 compose 按 `POSTGRES_PASSWORD` 拼接，`.env` 里的 `GS_DATABASE_URL` 只供宿主机上直接运行 CLI 时使用。

## 3. 反向代理（Caddy 示例）

```caddyfile
gs.example.com {
    encode gzip
    handle /api/* { reverse_proxy 127.0.0.1:8000 }
    handle /healthz { reverse_proxy 127.0.0.1:8000 }
    handle { reverse_proxy 127.0.0.1:3000 }   # Next.js 前端（--profile web）
}
dash.example.com {
    basicauth { admin <caddy hash-password 生成的哈希> }
    reverse_proxy 127.0.0.1:8501              # Streamlit（--profile dashboard）
}
```

鉴权分三层：

- API：Bearer token（`GS_API_TOKEN`），`/healthz` 除外。
- Next.js：浏览器只访问同源 `/api/*`，由前端服务端代理附加 token（token 不下发到浏览器）；
  `.env` 设置 `GS_WEB_USER` / `GS_WEB_PASSWORD` 后整站启用 Basic Auth。对外只暴露前端时，
  Caddy 可以不单独转发 `/api/*`（前端会代理到 `app:8000`）。
- Streamlit 自身无鉴权，务必加 basicauth 或只走 SSH 隧道：`ssh -L 8501:127.0.0.1:8501 vps`。

## 4. 定时任务

全部由 `gs serve` 内置的 APScheduler 执行（时区 = `GS_REPORT_TIMEZONE`）：

| 任务 | 时间 | 内容 |
|---|---|---|
| collect_due | 每 15 分钟 | 采集到期的源（各源 `interval_minutes`） |
| hourly | 每小时 :07 | 标注 → 关键词统计 → embedding → 归并 → 话题统计 → 订阅匹配 → 源健康告警 |
| daily | 每天 `GS_REPORT_HOUR`:00 | 命名 → 扩散 → 商机漏斗 → 空白分析 → 验证 → 排序 → 日报推送 |
| nightly | 每天 03:00 | 近义话题合并、重算话题统计、训练排序模型（反馈 ≥ 20 条且正负样本都有时） |
| weekly | 每周一 `GS_REPORT_HOUR`:30 | 上一个完整 ISO 周的周报 → `reports/weekly/`，并推送 |
| monthly | 每月 1 日 `GS_REPORT_HOUR`:45 | 上个月的月报 → `reports/monthly/`，并推送 |

同一阶段由 PostgreSQL advisory lock 互斥，手动执行 `gs run hourly` 不会与调度冲突。

### 队列模式（可选）

单进程顺序采集跟不上时（源 > 50 个或单源耗时长），开启 Redis 队列：

```bash
# .env
GS_REDIS_URL=redis://redis:6379/0
# 启动 redis + worker，worker 可扩多份
GS_PROFILES="web scale" ./scripts/deploy.sh
docker compose --profile scale up -d --scale worker=3
```

开启后 `gs serve` 的调度器只负责入队：`collect_due` 按源拆成 `collect:<source_id>` 任务（同名任务去重），
其余流水线作为单个任务入队，由 worker 执行；流水线之间仍由 advisory lock 互斥。
未设置 `GS_REDIS_URL` 时调度器在进程内直接执行，行为与之前一致。

## 5. 备份与恢复

```bash
./scripts/backup.sh                                   # 手动备份（pg_dump -Fc + reports/configs/.env 打包）
GS_BACKUP_REMOTE=r2:gs-backups ./scripts/backup.sh    # 同步到对象存储（需 rclone）
./scripts/restore.sh backups/groundswell-XXXX.dump    # 恢复（timescaledb_pre_restore / post_restore）
```

默认保留 14 天（`GS_BACKUP_KEEP_DAYS`）。**每月至少演练一次恢复**：在另一台机器上 `restore.sh` 后执行
`gs db current` 与 `gs status`。

## 6. 运维命令

```bash
docker compose exec app gs status           # 各源新鲜度、24h 成功率、LLM 成本
docker compose logs -f --tail 200 app       # 调度日志
docker compose exec app gs run hourly       # 手动触发
docker compose exec app gs report daily --push
docker compose exec db psql -U groundswell  # 直接查库
docker compose exec app gs report brief 42  # 单商机深度报告 + 一页纸计划
docker compose exec app gs reviews mine 1232780281 --country us   # App Store 差评挖掘
```

报告文件都写在 `reports/`（已挂载到宿主机并纳入备份）：`daily/`、`weekly/`、`monthly/`、`brief/`、`reviews/`、`backtest/`。

## 回测与历史数据

回测检验系统能否在黄金集（`configs/golden_set.yaml`）案例起飞前发出信号，需要案例起飞前约 60 天的历史数据。
刚部署时库里只有几天的数据，大部分案例会是 `no_data`，属正常现象。两种补数据方式：

```bash
# 1. 导入外部历史数据（JSONL，每行至少 title 与 published_at；可选 url/text/country/language/metrics/external_id）
docker compose cp hn-2024.jsonl app:/app/data/     # /app/data 是 app-data 卷
docker compose exec app gs data import /app/data/hn-2024.jsonl --source hn_archive --region NA
# 2. 让系统自然积累，一个季度后回测结果才有统计意义

docker compose exec app gs backtest                    # 输出 reports/backtest/*.md，并追加 kpi.jsonl
docker compose exec app gs data export /app/data/raw.jsonl --days 30   # 导出供离线分析 / 迁移
```

每次回测会与上一次（同一版黄金集）比较，命中率或平均提前天数下降时报告中标记“回归”；前端“系统健康”页展示 KPI 历史与排序模型状态。
修改评分、阈值或方法论后，先跑回测再上线。

资源参考（Phase 1–3 实测量级）：每天约 1–3 万条条目、快照 10–30 万行；压缩策略开启后每月增长 < 1 GB。

## 7. Phase 1 验收：连续 7 天日报

上线后按下表逐日打勾，全部满足才进入 Phase 2：

- [ ] 每天 `GS_REPORT_HOUR` 准时收到钉钉日报（超过 4000 字自动分条）
- [ ] 日报含 GLOBAL / GC / NA 三个区块，每块热度榜 ≥ 5 条
- [ ] 第 2 天起“增长关键词”出现非空结果（冷启动需累积 `GS_TREND_WINDOW_HOURS/2` 小时基线）
- [ ] 启用源 24h 成功率 ≥ 90%，`gs status` 无“故障”源；故障源 6h 内收到告警
- [ ] 每天 04:30 备份文件生成，第 7 天完成一次恢复演练
- [ ] 人工抽查：当天真实热点（微博/知乎/HN 首页）在日报中能找到
