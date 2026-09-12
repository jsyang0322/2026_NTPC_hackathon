"""所有 LLM / Bedrock 呼叫的唯一入口。

競賽硬性規定：Amazon Bedrock 請求須 ≤ 1 RPS。
本模組以全域 token bucket 強制限流，並提供快取與指數退避重試。
extract / issues / defects / draft / critic / similar_cases 等模組
一律透過此 client 呼叫，不得直接 import boto3 打 Bedrock。

設計為「不綁環境」：單體應用與 Lambda 都能直接使用同一個實例。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 預設模型（us-west-2 實測 ACTIVE）。
# ★ 重要：Claude Sonnet 4.5 / Haiku 4.5 不支援裸模型 ID 的 on-demand 呼叫，
#   必須用「inference profile」ID（加 us. 前綴），否則會 ValidationException。
# 分層理由：配額與延遲，非地端與雲端之分（見說明書 §1.4）
MODEL_WRITER = os.environ.get("BEDROCK_MODEL_WRITER", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
MODEL_LIGHT = os.environ.get("BEDROCK_MODEL_LIGHT", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
EMBED_MODEL = os.environ.get("BEDROCK_EMBED_MODEL", "cohere.embed-multilingual-v3")

AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")


@dataclass
class BedrockClient:
    """全域 ≤ 1 RPS 的 Bedrock 呼叫入口。

    參數：
        min_interval: 兩次呼叫之間的最小間隔秒數（1.05 留安全邊際）。
        cache_dir:    依 prompt hash 落檔的快取目錄。
        max_retries:  ThrottlingException 的指數退避重試次數。
        dry_run:      True 時不呼叫真實 Bedrock，回傳 mock，用於骨架驗證與離線測試。
    """

    min_interval: float = 1.05
    cache_dir: str = "data/cache"
    max_retries: int = 5
    dry_run: bool = False
    region: str = AWS_REGION

    _last_call: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _client: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)

    # --- 內部：延遲建立 boto3 client（dry_run 時完全不建立）---
    def _bedrock(self):
        if self._client is None:
            import boto3  # 延遲 import，dry_run 或未裝 boto3 時不受影響

            self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    # --- 內部：全域限流，確保 ≤ 1 RPS ---
    def _throttle(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            wait = self.min_interval - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    # --- 內部：快取鍵 ---
    @staticmethod
    def _hash(payload: dict) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]

    def _cache_path(self, key: str) -> Path:
        return Path(self.cache_dir) / f"{key}.json"

    def _cache_get(self, key: str) -> dict | None:
        p = self._cache_path(key)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return None

    def _cache_put(self, key: str, value: dict) -> None:
        self._cache_path(key).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # --- 對外：文字生成（Converse API）---
    def converse(
        self,
        messages: list[dict],
        model_id: str | None = None,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        use_cache: bool = True,
    ) -> str:
        """呼叫 Bedrock Converse，回傳純文字。受全域限流與快取保護。"""
        model_id = model_id or MODEL_WRITER
        payload = {
            "model_id": model_id,
            "messages": messages,
            "system": system,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        key = self._hash(payload)

        if use_cache:
            cached = self._cache_get(key)
            if cached is not None:
                return cached["text"]

        if self.dry_run:
            text = f"[DRY_RUN::{model_id}] 這是骨架驗證用的假回應，不呼叫真實 Bedrock。"
            if use_cache:
                self._cache_put(key, {"text": text, "dry_run": True})
            return text

        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "messages": messages,
            "inferenceConfig": {"temperature": temperature, "maxTokens": max_tokens},
        }
        if system:
            kwargs["system"] = [{"text": system}]

        text = self._invoke_with_retry(kwargs)
        if use_cache:
            self._cache_put(key, {"text": text})
        return text

    def _invoke_with_retry(self, kwargs: dict) -> str:
        from botocore.exceptions import ClientError

        delay = 1.0
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self._bedrock().converse(**kwargs)
                return resp["output"]["message"]["content"][0]["text"]
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("ThrottlingException", "TooManyRequestsException") and attempt < self.max_retries - 1:
                    time.sleep(delay)
                    delay *= 2  # 指數退避
                    continue
                raise
        raise RuntimeError("Bedrock 重試次數耗盡")

    # --- 對外：embedding ---
    def embed(self, texts: list[str], model_id: str | None = None, use_cache: bool = True) -> list[list[float]]:
        """回傳每段文字的向量。dry_run 回傳零向量以驗證串接。"""
        model_id = model_id or EMBED_MODEL
        if self.dry_run:
            return [[0.0] * 8 for _ in texts]
        # 實作留待 build_index 階段；此處保留介面
        raise NotImplementedError("embed 將於 §9.1 索引建置階段實作")


# 模組級單例：全流程共用同一個限流器，確保全域 ≤ 1 RPS
_default_client: BedrockClient | None = None


def get_client(dry_run: bool | None = None) -> BedrockClient:
    """取得全域共用的 BedrockClient。

    dry_run 可由參數或環境變數 BEDROCK_DRY_RUN=1 控制。
    """
    global _default_client
    if dry_run is None:
        dry_run = os.environ.get("BEDROCK_DRY_RUN", "0") == "1"
    if _default_client is None or _default_client.dry_run != dry_run:
        _default_client = BedrockClient(dry_run=dry_run)
    return _default_client
