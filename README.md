# 观潮 / Groundswell

多区域互联网舆情 → 话题 → 商机 引擎。

持续采集大中华区（GC）、东南亚（SEA）、北美（NA）、拉美（LATAM）、欧洲（EU）与全球（GLOBAL）的热榜、社区与榜单数据，
把零散条目归并成话题，用 z-score 找出**正在加速**的讨论，再用方法论库 + LLM 漏斗按区域生成可执行的商机，
并追踪话题的跨区扩散路径，发现“别处已起飞、本区还空白”的信息差。

## 能力一览

| 模块 | 说明 |
|---|---|
| 采集 | Hacker News、Reddit、GitHub、Google Trends、App Store 榜单、Product Hunt、RSSHub（微博 / 知乎 / B 站 / 政策…）、DailyHotApi 热榜、通用 RSS；竞品监控（GitHub Issues、Release 更新日志）；源健康记录与故障告警 |
| 处理 | 语言 / 国家 / 区域四级归属、simhash 去重、关键词抽取、按区域时区切小时统计、Z-score + EWMA（带冷启动保护） |
| 话题 | bge-m3 embedding、增量归并 + 夜间近义合并、LLM 命名、六类分类、慢变量（长期趋势）关联 |
| 商机 | `configs/methodology.yaml` 方法论库 → 候选筛选 → 便宜模型初筛 → 强模型深度分析（证据强制引用、预算熔断、无 LLM 时启发式兜底） |
| 验证 | Google Trends 5 年线、GitHub、App Store、招聘、融资新闻、已采集的政策文件 |
| 信息差 | 跨区扩散路径、区域空白检测（≥ 2 区起飞而目标区缺席 / 滞后）→ 专门的空白分析提示词 |
| 排序 | 人工反馈（good / bad / …）训练排序模型（≥ 20 条 NumPy 逻辑回归，≥ 300 条 LightGBM） |
| 报告 | 钉钉日报（区域分块 + 信息差矩阵）、周报 / 月报、单商机深度报告 + 一页纸商业计划、App Store 差评痛点挖掘 |
| 协作 | 点子库生命周期（candidate → watching → validated → archived）、分派、评论、关键词订阅告警 |
| 回测 | 黄金集（`configs/golden_set.yaml`）检验“起飞前能否发出信号”，KPI 历史与回归标记 |
| 界面 | Next.js + ECharts 看板（排行、话题详情、商机、区域矩阵、扩散时间线、点子看板、订阅、报告、系统健康）；Streamlit 快速看板 |

## 架构

```
 采集器 ──► raw_items / item_snapshots ──► 处理（标注·去重·统计）──► 话题（embedding·归并·命名）
                                                                         │
            FastAPI /api/* ◄── queries ◄── topic_hourly_stats（TimescaleDB）◄┘
               │                               │
       Next.js 看板 / Streamlit          扩散路径 · 区域空白 ──► 商机漏斗（LLM）──► 验证器 ──► 排序模型
                                                                                              │
                                                              日报 / 周报 / 月报 / 深度报告 ◄──┘ ──► 钉钉
```

- **模块化单体**：一个 Python 包，`gs` CLI 是所有能力的统一入口；`gs serve` 内置 APScheduler 跑定时流水线。
- **一库多用**：PostgreSQL 16 + TimescaleDB（时序）+ pgvector（向量），`raw_items` 是唯一事实来源。
- **可水平扩展**：配置 `GS_REDIS_URL` 后调度器只负责入队，采集按源拆成独立任务，由多个 `gs worker` 并行执行。

设计决策见 [docs/adr](docs/adr/README.md)（ADR-001 ~ 010）。

## 快速开始（Windows 开发机）

前置：Docker Desktop、Python 3.11、[uv](https://docs.astral.sh/uv/)、Node.js 22（仅前端需要）。

```powershell
.\scripts\dev.ps1 up          # 生成 .env → 启动 db / rsshub / dailyhot → 安装依赖 → 迁移 → 校验配置
.\.venv\Scripts\gs.exe collect hackernews github_trending   # 抓几个源试试
.\scripts\dev.ps1 hourly      # 处理 + 归并话题
.\.venv\Scripts\gs.exe run daily       # 扩散 → 商机 → 验证 → 排序 → 日报（不推送）
.\scripts\dev.ps1 serve       # 常驻：调度器 + API（http://127.0.0.1:8000/docs）
.\scripts\dev.ps1 test        # 测试（自动创建 groundswell_test 库）
```

Linux / macOS 用 `./scripts/dev.sh` 对应命令。

前端：

```powershell
cd web
npm install
$env:GS_API_TOKEN = "<与 .env 相同>"; npm run dev      # http://localhost:3000
```

浏览器只访问 Next.js 的同源 `/api/*`，由服务端代理附加 `GS_API_TOKEN`，token 不会下发到浏览器。
设置 `GS_WEB_USER` / `GS_WEB_PASSWORD` 后整站启用 Basic Auth。

## 命令

| 命令 | 作用 |
|---|---|
| `gs collect [SOURCES...] [--due]` | 抓取指定源 / 所有到期源 |
| `gs process [--stage] [--hours]` | 标注 → 关键词统计 → embedding → 归并 → 话题统计 |
| `gs topics name / show / merge / recluster` | 话题命名、查看、人工合并、近义合并 |
| `gs spread [--days] [--gaps]` | 跨区扩散路径；`--gaps` 同时对区域空白做深度分析 |
| `gs score [--regions] [--limit]` | 商机漏斗 |
| `gs validate [--days] [--limit]` | 二次验证 |
| `gs feedback ID LABEL [--note]` | 人工反馈（good 自动进点子库） |
| `gs rank train / apply` | 训练 / 应用排序模型 |
| `gs report daily / weekly / monthly [--push]` | 日报、周报、月报（`--push` 推送钉钉） |
| `gs report brief ID` | 单商机深度报告 + 一页纸商业计划 → `reports/brief/` |
| `gs reviews mine APP_ID [--country] [--pages]` | App Store 差评痛点挖掘 → `reports/reviews/` |
| `gs ideas list / add / move / assign / comment` | 点子库 |
| `gs sub add / list / rm` | 关键词订阅告警 |
| `gs backtest [--golden] [--lookback-days]` | 黄金集回测 → `reports/backtest/` 与 `kpi.jsonl` |
| `gs data import PATH / export PATH` | 历史数据 JSONL 导入导出（回测需要历史数据） |
| `gs run hourly / daily / nightly` | 手动触发定时流水线（与调度器通过 advisory lock 互斥） |
| `gs serve` / `gs worker` | 常驻服务 / Redis 队列 worker |
| `gs status` / `gs config check` / `gs db upgrade` | 运行状态、配置校验、数据库迁移 |

每个命令都支持 `--help`。

## 配置

| 文件 | 内容 |
|---|---|
| `configs/regions.yaml` | 区域、国家、时区、语言，以及渠道 / 支付 / 监管等区域上下文（进入 LLM 提示词与报告） |
| `configs/sources.yaml` | 数据源：类型、区域、采集间隔、参数、分类提示 |
| `configs/methodology.yaml` | 方法论原则与信号（PG、Trends.vc、Schlep、供应链套利…） |
| `configs/slow_variables.yaml` | 慢变量（人口、技术、监管等长期趋势）与关键词 |
| `configs/calendars.yaml` | 区域节假日 / 大促，用于季节性修正 |
| `configs/golden_set.yaml` | 回测黄金集：已知起飞案例与起飞日期 |

环境变量见 [.env.example](.env.example)，常用项：

- `GS_LLM_CHEAP_MODEL` / `GS_LLM_STRONG_MODEL`（LiteLLM 模型名，如 `deepseek/deepseek-chat`）+ 对应厂商 API key；`GS_LLM_DAILY_BUDGET_CNY` 每日预算。未配置时全部走启发式。
- `GS_EMBEDDING_PROVIDER`：`hash`（开发）/ `api`（SiliconFlow 等 bge-m3）/ `local`。
- `GS_DINGTALK_WEBHOOK` / `GS_DINGTALK_SECRET`：钉钉推送。
- `WEIBO_COOKIES`、`GS_REDDIT_CLIENT_ID/SECRET`、`GS_GITHUB_TOKEN`：对应数据源的凭证。
- `GS_HTTP_PROXY`：跨境访问代理。
- `GS_REDIS_URL`：启用队列模式（配合 `--profile scale`）。

## 部署

单台 Linux VPS + Docker Compose，见 [docs/deploy.md](docs/deploy.md)。

```bash
./scripts/deploy.sh                          # 首次生成 .env；填好后再次执行完成部署
GS_PROFILES="web" ./scripts/deploy.sh        # 同时启动 Next.js 前端
```

## 目录

```
src/groundswell/
  collectors/    采集器（注册表 + 各平台实现）
  processing/    标注、去重、关键词、趋势统计、embedding、聚类
  opportunity/   商机漏斗、提示词、验证器、扩散与空白、排序模型
  reporting/     日报、周报 / 月报、深度报告、钉钉推送
  api/           FastAPI
  dashboard/     Streamlit
  db/            SQLAlchemy 模型、Alembic 迁移
  cli.py  pipeline.py  scheduler.py  taskqueue.py  backtest.py  dataio.py  reviews.py
web/             Next.js 前端
configs/         YAML 配置
docs/            ADR 与部署文档
scripts/         开发 / 部署 / 备份脚本
```
