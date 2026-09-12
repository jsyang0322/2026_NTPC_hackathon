"""引用抽取與共現索引（§8.2）。[純本機，不呼叫 LLM]

正規表示式抽出決定書所有法條/函釋/判決引用，建立「爭點 ↔ 法條」共現統計。
注意「第 15 條之 2」不可抓成 15；列舉式「第 48、72、73、74 條」須展開。
"""

from __future__ import annotations

import re

# 修正版：條之 X 正確捕捉；列舉式另處理（§8.2）
LAW_NAMES = (r"(洗錢防制法|廢棄物清理法|空氣污染防制法|建築法|噪音管制法|行政罰法|"
             r"行政執行法|訴願法|行政程序法|行政訴訟法|政府資訊公開法|民法|"
             r"工廠管理輔導法|環境教育法|檔案法)")
ART = r"第(\d+)條(?:之(\d+))?(?:第(\d+)項)?(?:第(\d+)款)?"
CITE = LAW_NAMES + ART
ENUM = LAW_NAMES + r"第((?:\d+、)+\d+)條"
INTERP = r"([一-鿿]{2,6}部?)?\s*\d{2,3}年\d{1,2}月\d{1,2}日[一-鿿]*字第\d+號函"
JUDGMENT = r"(最高行政法院|[一-鿿]{2,4}高等行政法院|臺灣[一-鿿]{2,4}地方法院)\s*\d{2,3}年度[一-鿿]+字第\d+號"


def extract_citations(text: str) -> list[dict]:
    """抽出單筆文字中的所有引用。"""
    cites: list[dict] = []
    for m in re.finditer(CITE, text):
        cites.append({"law": m.group(1), "article": m.group(2),
                      "sub_article": m.group(3), "paragraph": m.group(4), "clause": m.group(5)})
    # TODO(§8.2): ENUM 展開、同法/同條/前揭上下文補齊、範圍式展開
    return cites


def build_cooccurrence(parsed_dir: str = "data/parsed") -> dict:
    """建立爭點 ↔ 法條共現統計表。"""
    # TODO(§8.2): 讀 parsed → extract_citations → 與 issue_tag 交叉統計
    return {"_stub": True}


if __name__ == "__main__":
    print(build_cooccurrence())
