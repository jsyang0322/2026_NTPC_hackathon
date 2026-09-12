"""核心業務邏輯套件：純函式，dict 進 dict 出，不綁執行環境。

單體應用、Lambda、Step Functions 皆共用此套件。
所有 LLM 呼叫一律透過 core.bedrock_client。
"""

# 在任何子模組讀取 os.environ 之前，先自動載入 .env（KB id、模型 id 等）。
# 這確保無論從哪個進入點啟動（Streamlit / CLI / preflight），設定都已到位，
# 不必每個 shell 手動 source。shell 既有值優先（override=False）。
from . import env as _env  # noqa: E402

_env.load()
