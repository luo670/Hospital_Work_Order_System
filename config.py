"""
配置中心

生产环境通过 .env 或环境变量注入敏感信息与连接串，源码不硬编码密钥。
- DATABASE_URL：SQLAlchemy 连接串，生产使用 MySQL（mysql+pymysql://...）
- SECRET_KEY：会话签名密钥
- DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL / QWEN_MODEL：通义千问（员工评估 AI）
"""

import os

from dotenv import load_dotenv

# 载入 .env（若存在）。load_dotenv 默认不覆盖已存在的环境变量，
# 因此命令行显式传入的 DATABASE_URL 等优先级更高，便于本地用 SQLite 冒烟测试。
load_dotenv()

# 生产环境连接示例：
#   mysql+pymysql://user:password@host:3306/workorder?charset=utf8mb4
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "mysql+pymysql://root:root@127.0.0.1:3306/workorder?charset=utf8mb4",
)

SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production-please")

# 大模型配置：默认选用成本最低的可用千问模型；可通过 .env 的 QWEN_MODEL 覆盖
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen-plus")
QWEN_MAX_TOKENS = 400
# 候选模型（按单价从低到高），首个不可用自动尝试下一个
QWEN_FALLBACK = ["qwen-plus", "qwen-max", "qwen-long", "qwen-turbo"]

# 向量化（RAG）配置：与 chat 共用同一个 DASHSCOPE_API_KEY
# - 默认走公共 DashScope 的 OpenAI 兼容 embeddings 接口（text-embedding-v3 单价极低）
# - 若自建 MaaS 网关同样代理了 embeddings，可用 EMBEDDING_BASE_URL 覆盖；
#   调用失败时会自动回退到 DASHSCOPE_BASE_URL 对应的 /embeddings，便于混合部署
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-v3")
EMBEDDING_BASE_URL = os.environ.get(
    "EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
