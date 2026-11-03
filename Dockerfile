# 医院运维工单系统 —— 生产镜像（FastAPI + MySQL）
#
# 关于 worker 数量（重要）：
#   员工评估的 SSE 流式输出依赖「单进程内的事件循环」——_stream_sse 用
#   threading.Thread + asyncio.run_coroutine_threadsafe 把同步 urllib 生成器
#   桥接回当前进程的事件循环。若用 --workers N 起多进程，同一浏览器的连续请求
#   可能落到不同进程，会话与流式任务的归属需要额外的会话粘性/共享存储支持。
#   因此当前固定单 worker（uvicorn 单进程默认仍可并发处理多请求）；
#   若将来要扩多 worker，须在反向代理（Nginx / Ingress）层做会话粘性。

FROM python:3.13-slim

# PYTHONUNBUFFERED：容器日志实时输出（SSE / 启动日志便于排查）
# PYTHONDONTWRITEBYTECODE：不在镜像内产生 __pycache__
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 安装 uv（依赖管理工具；仅用于构建期安装依赖）
RUN pip install --no-cache-dir uv

# 先复制依赖声明文件，利用 Docker 层缓存：
# 只要 pyproject.toml / uv.lock 没变，下面的 uv sync 就会命中缓存
COPY pyproject.toml uv.lock ./

# 用 uv 按锁文件精确安装依赖（不含 pytest 等 dev 依赖）
#   --frozen：严格按 uv.lock 安装，不重新解析版本
#   --no-dev：跳过 [dependency-groups].dev
RUN uv sync --frozen --no-dev

# 复制代码（.env、python/、.venv、*.db 等由 .dockerignore 排除）
COPY . .

# 优先使用 uv 创建的虚拟环境中的可执行文件（uvicorn）
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 5000

# 单 worker：SSE 依赖单进程事件循环，多 worker 需额外处理会话粘性
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]
