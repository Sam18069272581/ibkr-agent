#!/usr/bin/env bash
# 定时任务调用的脚本：加载 .env，运行一次，把输出追加到日志。
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
mkdir -p data
{
  echo "===== $(date -Is) ====="
  .venv/bin/ibkr-agent run
  .venv/bin/ibkr-agent report
} >> data/agent.log 2>&1
