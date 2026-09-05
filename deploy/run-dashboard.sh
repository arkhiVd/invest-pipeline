#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
export INVEST_MODE=${INVEST_MODE:-demo}
export PYTHONPATH="$root"
exec .venv/bin/streamlit run invest/dashboard.py \
  --server.address=127.0.0.1 \
  --server.port=8501 \
  --server.headless=true \
  --browser.gatherUsageStats=false \
  --server.fileWatcherType=none
