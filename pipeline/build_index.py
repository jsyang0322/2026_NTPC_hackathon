"""向量 + BM25 索引建置（§9.1）。[離線，embedding 呼叫 Bedrock]

檢索單位：事實欄 narrative / petitioner_claim + 理由欄 application 段。
向量以 Bedrock embedding（Cohere Multilingual 優先），BM25 以字元 bigram。
記憶體索引，向量檔存 S3。Demo 案件須自索引排除（§12.3）。
"""

from __future__ import annotations


def build_index(parsed_dir: str = "data/parsed", out_dir: str = "data/index",
                exclude_doc_ids: list[str] | None = None) -> dict:
    """建立向量與 BM25 索引，回傳索引 metadata。"""
    # TODO(§9.1): 讀 parsed → 切檢索單位 → embed（bedrock_client.embed）→ 存向量 + BM25
    return {"_stub": True}


if __name__ == "__main__":
    print(build_index())
