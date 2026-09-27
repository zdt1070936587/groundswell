# Windows 本地开发助手：.\scripts\dev.ps1 <命令>
#   up       启动 db / rsshub / dailyhot，安装依赖并迁移
#   down     停止容器
#   test     运行测试（含数据库测试）
#   lint     ruff check + format --check
#   fmt      ruff 自动修复与格式化
#   hourly   手动跑一次小时流水线
#   report   生成日报
#   serve    启动调度器 + API
param([Parameter(Position = 0)][string]$Command = "help")

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

function Uv { & python -m uv @args; if ($LASTEXITCODE) { exit $LASTEXITCODE } }
function Gs { & .\.venv\Scripts\gs.exe @args; if ($LASTEXITCODE) { exit $LASTEXITCODE } }

switch ($Command) {
    "up" {
        if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "已生成 .env，请按需修改" }
        docker compose up -d db rsshub dailyhot
        Uv sync --all-extras
        Gs db upgrade
        Gs config check
    }
    "down" { docker compose down }
    "test" {
        $port = (Select-String -Path .env -Pattern '^GS_DB_HOST_PORT=(\d+)' | ForEach-Object { $_.Matches[0].Groups[1].Value })
        if (-not $port) { $port = "5432" }
        docker compose exec -T db psql -U groundswell -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='groundswell_test'" | Out-String | ForEach-Object {
            if (-not $_.Trim()) { docker compose exec -T db psql -U groundswell -d postgres -c "CREATE DATABASE groundswell_test" }
        }
        $env:GS_TEST_DATABASE_URL = "postgresql+psycopg://groundswell:groundswell@127.0.0.1:$port/groundswell_test"
        & .\.venv\Scripts\python.exe -m pytest -q @($args)
    }
    "lint" {
        & .\.venv\Scripts\ruff.exe check .
        & .\.venv\Scripts\ruff.exe format --check .
    }
    "fmt" {
        & .\.venv\Scripts\ruff.exe check --fix .
        & .\.venv\Scripts\ruff.exe format .
    }
    "hourly" { Gs run hourly }
    "report" { Gs report daily }
    "serve" { Gs serve }
    default {
        Get-Content $PSCommandPath | Select-Object -First 10 | ForEach-Object { $_.TrimStart("# ") }
    }
}
