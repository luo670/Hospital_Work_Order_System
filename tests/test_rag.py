"""
RAG 与 SSE 相关逻辑单元测试（pytest）。

覆盖：
- main.cosine_sim：余弦相似度基础性质
- main.rebuild_knowledge_base + main.retrieve_similar：知识库构建与相似检索
- main._build_eval_prompt / build_engineer_evaluation：无工单时的兜底
- main._stream_sse：SSE 帧格式与完成事件

不依赖真实大模型 / embedding 接口：用 monkeypatch 替换为确定性假实现。
"""

import asyncio
import os
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from database import Base  # noqa: E402
from models import Department, WorkOrder  # noqa: E402


# ---------------------------------------------------------------------------
# 确定性假 embedding：按关键词出现次数构造向量，便于验证检索排序
# ---------------------------------------------------------------------------
VOCAB = ["蓝屏", "内存", "引导", "打印机", "卡纸", "搓纸轮", "PDA", "电池", "扫码", "网络", "驱动", "系统"]


def _fake_embed(text):
    return [float(text.count(w)) for w in VOCAB]


@pytest.fixture()
def db():
    """内存 SQLite + 三张已完成工单（分别对应电脑 / 打印机 / PDA 故障）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    s = Session()
    s.add(Department(id=1, name="内科", description="", created_at="2026-11-01T00:00:00"))
    now = "2026-11-01T10:00:00"
    s.add_all([
        WorkOrder(
            order_no="WO-COMP-001", type="computer", department_id=1,
            reporter_id=1, assignee_id=1, assigned_by=1,
            content="门诊电脑开机蓝屏无法进入系统",
            cause="内存条接触不良，系统引导文件损坏",
            solution="重新插拔内存条并重建引导记录",
            status="completed", created_at=now, completed_at="2026-11-01T12:00:00",
        ),
        WorkOrder(
            order_no="WO-PRINT-001", type="printer", department_id=1,
            reporter_id=1, assignee_id=1, assigned_by=1,
            content="住院部打印机卡纸无法打印",
            cause="进纸辊老化，纸张受潮",
            solution="清除卡纸并更换搓纸轮",
            status="completed", created_at=now, completed_at="2026-11-01T12:00:00",
        ),
        WorkOrder(
            order_no="WO-PDA-001", type="pda", department_id=1,
            reporter_id=1, assignee_id=1, assigned_by=1,
            content="PDA 电池续航骤降",
            cause="电池老化循环次数过多",
            solution="更换 PDA 电池",
            status="completed", created_at=now, completed_at="2026-11-01T12:00:00",
        ),
    ])
    s.commit()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 余弦相似度
# ---------------------------------------------------------------------------
def test_cosine_sim_identical_is_one():
    assert main.cosine_sim([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_sim_orthogonal_is_zero():
    assert main.cosine_sim([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_sim_zero_vector_is_zero():
    assert main.cosine_sim([0.0, 0.0], [1.0, 2.0]) == 0.0


# ---------------------------------------------------------------------------
# 知识库构建 + 检索
# ---------------------------------------------------------------------------
def test_rebuild_knowledge_base_indexes_completed_orders(db, monkeypatch):
    monkeypatch.setattr(main, "call_embedding", _fake_embed)
    n = main.rebuild_knowledge_base(db)
    assert n == 3
    total = db.execute(text("SELECT COUNT(*) FROM workorder_knowledge")).scalar()
    assert total == 3


def test_retrieve_similar_ranks_relevant_order_first(db, monkeypatch):
    monkeypatch.setattr(main, "call_embedding", _fake_embed)
    main.rebuild_knowledge_base(db)

    hits = main.retrieve_similar(db, "电脑开机蓝屏，怀疑内存故障", top_k=3)
    assert len(hits) == 3
    assert hits[0]["row"]["order_no"] == "WO-COMP-001"
    # 相似度应单调递减
    assert hits[0]["score"] >= hits[1]["score"] >= hits[2]["score"]


def test_retrieve_similar_returns_empty_when_kb_empty(db, monkeypatch):
    monkeypatch.setattr(main, "call_embedding", _fake_embed)
    assert main.retrieve_similar(db, "任意故障", top_k=3) == []


# ---------------------------------------------------------------------------
# embedding 端点回退
# ---------------------------------------------------------------------------
def test_embedding_endpoints_falls_back_to_chat_base(monkeypatch):
    monkeypatch.setattr(main, "EMBEDDING_BASE_URL", "https://public.example/v1")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://maas.example/compatible-mode/v1")
    eps = main._embedding_endpoints()
    assert eps[0] == "https://public.example/v1/embeddings"
    assert eps[1] == "https://maas.example/compatible-mode/v1/embeddings"


def test_embedding_endpoints_dedupes_and_strips_chat_path(monkeypatch):
    monkeypatch.setattr(main, "EMBEDDING_BASE_URL", "https://x/v1")
    monkeypatch.setenv("DASHSCOPE_BASE_URL", "https://x/v1/chat/completions")
    assert main._embedding_endpoints() == ["https://x/v1/embeddings"]


# ---------------------------------------------------------------------------
# 评估 prompt / 无工单兜底
# ---------------------------------------------------------------------------
def test_eval_prompt_no_orders():
    system, user, model, n = main._build_eval_prompt({"name": "测试工程师"}, [])
    assert n == 0
    assert isinstance(system, str) and isinstance(user, str)


def test_build_evaluation_no_orders_short_circuits():
    # 无工单时不应调用大模型，直接返回兜底文案
    summary, model = main.build_engineer_evaluation({"name": "测试工程师"}, [])
    assert "暂无" in summary


# ---------------------------------------------------------------------------
# SSE 帧格式
# ---------------------------------------------------------------------------
def test_stream_sse_emits_token_and_done_frames():
    def gen():
        yield "你"
        yield "好"

    async def collect():
        loop = asyncio.get_running_loop()
        frames = []
        async for frame in main._stream_sse(gen, loop):
            frames.append(frame)
        return frames

    frames = asyncio.run(collect())
    text = "".join(frames)
    assert "event: token" in text
    assert "你" in text and "好" in text
    assert "event: done" in text
    # 每个帧都应以空行结尾，符合 SSE 规范
    assert all(f.endswith("\n\n") for f in frames)
