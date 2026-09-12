"""啟動時自動載入 .env，讓 KB id、模型 id 等設定不必每個 shell 手動 source。

為什麼需要：本專案所有模組直接讀 os.environ，而環境變數不會自動存在。
過去若忘了 `source .env`，BEDROCK_KB_ID 就是空的，kb.retrieve() 會靜默回 mock
（假檢索餵真撰稿，最難察覺）。此模組在 core 被 import 時執行一次，從此
任何進入點（Streamlit / CLI / preflight / henry 匯入 core）都自動帶到設定。

設計原則：
  - override=False：shell 已手動設的值優先，.env 只補「沒設的」，不覆蓋。
    這樣臨時 `BEDROCK_DRY_RUN=1 python ...` 之類的覆寫仍然有效。
  - .env 不存在但 .env.example 在 → 自動複製一份，第一次啟動就不會漏設。
    範本裡的 BEDROCK_KB_ID 等值即成為預設，值仍集中在 .env（不進 git、不寫死進碼）。
  - 找不到 python-dotenv 時不報錯，僅提示——維持「沒有它也能跑（靠手動 source）」。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ENV = _ROOT / ".env"
_ENV_EXAMPLE = _ROOT / ".env.example"

_loaded = False


def _ensure_env_file() -> None:
    """.env 不存在時，從 .env.example 複製一份當起點。"""
    if _ENV.exists() or not _ENV_EXAMPLE.exists():
        return
    try:
        shutil.copyfile(_ENV_EXAMPLE, _ENV)
        print(f"[core.env] 未找到 .env，已從 .env.example 複製一份：{_ENV}")
    except OSError as exc:  # 權限或唯讀檔案系統：不擋流程
        print(f"[core.env] 無法建立 .env（{exc}），請手動 cp .env.example .env")


def load(force: bool = False) -> bool:
    """載入 .env 到 os.environ（只做一次）。回傳是否成功載入檔案。

    override=False：不覆蓋 shell 既有值。
    """
    global _loaded
    if _loaded and not force:
        return True

    _ensure_env_file()

    try:
        from dotenv import load_dotenv
    except ImportError:
        # 沒裝 python-dotenv：回退到「靠手動 source」，只在真的沒帶到 KB id 時提示
        if not os.environ.get("BEDROCK_KB_ID", "").strip():
            print("[core.env] 未安裝 python-dotenv 且 BEDROCK_KB_ID 未設；"
                  "請 `pip install python-dotenv` 或先 `set -a; source .env; set +a`。")
        _loaded = True
        return False

    ok = load_dotenv(dotenv_path=_ENV, override=False)
    _loaded = True
    return ok
