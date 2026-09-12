#!/usr/bin/env bash
# Demo 一鍵啟動：載入設定 → 前置檢查 → 起 Streamlit。
#
# 程式本身已會自動載入 .env（見 core/env.py），這支腳本再多做兩件事：
#   1. 把 .env 也 source 進當前 shell，讓「還沒 import core 的」子行程（如 preflight
#      直接讀 os.environ）也拿得到值。
#   2. 跑 preflight_check 擋掉三個假資料陷阱，通過才啟動 Streamlit。
#
# 用法：
#   ./run.sh                 # 檢查 → 啟動 Streamlit
#   ./run.sh --skip-check    # 跳過 preflight（不建議）
#   ./run.sh --purge-cache   # 啟動前順手清掉 dry-run 假快取

set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

# --- 1) 載入 .env（不存在則從範本複製）---
if [ ! -f .env ] && [ -f .env.example ]; then
  echo "[run] 未找到 .env，從 .env.example 複製一份。"
  cp .env.example .env
fi
if [ -f .env ]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
fi

# --- 2) 解析參數 ---
SKIP_CHECK=0
PREFLIGHT_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --skip-check) SKIP_CHECK=1 ;;
    --purge-cache) PREFLIGHT_ARGS+=("--purge-cache") ;;
    *) echo "[run] 未知參數：$arg"; exit 2 ;;
  esac
done

# --- 3) 前置檢查 ---
if [ "$SKIP_CHECK" -eq 0 ]; then
  echo "[run] 執行前置檢查…"
  if ! "$PY" -m scripts.preflight_check ${PREFLIGHT_ARGS[@]+"${PREFLIGHT_ARGS[@]}"}; then
    echo
    echo "[run] 前置檢查未通過。修正後再跑，或用 ./run.sh --skip-check 略過（不建議）。"
    exit 1
  fi
fi

# --- 4) 啟動 Streamlit ---
echo "[run] 啟動 Streamlit…（提醒：側邊欄 Dry-run 開關預設為開，Demo 時請手動關閉）"
exec "$PY" -m streamlit run app/streamlit_app.py
