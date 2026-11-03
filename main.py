"""
医院运维工单系统（FastAPI + MySQL 版本）

技术栈：
- Web 框架：FastAPI（ASGI），服务端模板渲染沿用 Jinja2
- 数据库：MySQL（通过 SQLAlchemy ORM + PyMySQL 驱动）
- 鉴权：基于会话（Starlette SessionMiddleware）+ Werkzeug 密码哈希
- AI：通义千问（OpenAI 兼容接口，标准库 urllib 直连）

角色：管理员(admin) / 主管(supervisor) / 工程师(engineer)
工单类型：电脑故障 / 打印机故障 / PDA故障
超时规则：工单提交后 24 小时内未完成即视为超时
"""

import asyncio
import json
import math
import os
import sys
import threading
import urllib.request
import urllib.error
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, Depends
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from werkzeug.security import generate_password_hash, check_password_hash

from config import (SECRET_KEY, QWEN_MODEL, QWEN_MAX_TOKENS, QWEN_FALLBACK,
                    EMBEDDING_MODEL, EMBEDDING_BASE_URL)
from database import engine, SessionLocal, Base, get_db
from models import User, Group, Department, WorkOrder, WorkOrderTag, Evaluation, KnowledgeItem
from web import templates, flash, render, login_required, role_required, get_current_user, AuthRedirect
import seed_data
import schedules as schedule_gen

# 演示数据 / 排班生成（独立于主源码的数据写入模块）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 角色 / 工单类型 / 状态 的中文映射
ROLE_LABELS = {"admin": "管理员", "supervisor": "主管", "engineer": "工程师"}
ORDER_TYPES = {"computer": "电脑故障", "printer": "打印机故障", "pda": "PDA故障"}
STATUS_LABELS = {
    "assigned": "待接受",
    "in_progress": "处理中",
    "completed": "已完成",
}
TIMEOUT_HOURS = 24


# 让所有模板都能直接使用这些常量
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS
templates.env.globals["ORDER_TYPES"] = ORDER_TYPES
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS


# ---------------------------------------------------------------------------
# 结果集 -> 字典列表（兼容 MySQL / SQLite，便于在模板中按列名访问与追加字段）
# 统一使用 SQLAlchemy 2.0 官方的 Row._mapping，避免依赖内部属性 r._fields
# ---------------------------------------------------------------------------
def _row_to_dict(r):
    return dict(r._mapping)


def _rows(result):
    return [dict(m) for m in result.mappings().all()]


def _one(result):
    row = result.mappings().first()
    return dict(row) if row else None


# 每个请求加载当前登录用户到 request.state.user（供依赖与模板使用）
# 以 FastAPI 依赖方式注入，确保 SessionMiddleware 已先就绪（request.session 可用）。
# 复用与路由相同的请求级 Session：FastAPI 的依赖缓存保证同一请求内 Depends(get_db)
# 只执行一次，因此本函数与路由 handler 拿到的是同一个 Session 实例，不会重复开连接。
async def load_current_user(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    user = None
    if uid:
        rows = db.execute(
            text("SELECT * FROM users WHERE id=:uid"), {"uid": uid}
        ).fetchall()
        user = _row_to_dict(rows[0]) if rows else None
    request.state.user = user


# ---------------------------------------------------------------------------
# 启动 / 关闭：建表、首次写入演示数据、生成排班
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 默认密钥告警：生产环境必须配置强随机 SECRET_KEY，否则会话签名可被伪造
    if SECRET_KEY == "change-this-secret-key-in-production-please":
        print("[WARN] 正在使用默认 SECRET_KEY，生产环境请务必在 .env 中修改")
    # 确保数据库（MySQL 场景）存在
    try:
        from database import _ensure_database_exists
        _ensure_database_exists()
    except Exception:
        pass
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        if db.execute(text("SELECT COUNT(*) AS c FROM users")).scalar() == 0:
            seed_data.load(db)
        if db.execute(text("SELECT COUNT(*) AS c FROM schedules")).scalar() == 0:
            schedule_gen.generate_all(db)
        db.commit()
    finally:
        db.close()
    yield


app = FastAPI(title="医院运维工单系统", lifespan=lifespan,
              dependencies=[Depends(load_current_user)])
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


# ---------------------------------------------------------------------------
# 鉴权重定向：依赖中 raise AuthRedirect，这里统一转为 302 跳转（可附带闪现消息）
# ---------------------------------------------------------------------------
@app.exception_handler(AuthRedirect)
async def _auth_redirect_handler(request: Request, exc: AuthRedirect):
    if exc.flash_message:
        flash(request, exc.flash_message)
    return RedirectResponse(str(exc.url), status_code=302)


# ---------------------------------------------------------------------------
# 超时判定：衡量「工单整体是否超时」（用户视角的响应时效）
# 口径：从 created_at（报障提交时间）到 完结时间 completed_at（未完成则取当前时间），
#       间隔超过 TIMEOUT_HOURS(24h) 即判超时。它衡量的是「用户等待 / 工单滞留」，并非工程师效率。
# ---------------------------------------------------------------------------
def order_timed_out(order):
    created = datetime.fromisoformat(order["created_at"])
    if order["status"] == "completed" and order["completed_at"]:
        resolve = datetime.fromisoformat(order["completed_at"])
    else:
        resolve = datetime.now()
    return (resolve - created) > timedelta(hours=TIMEOUT_HOURS)


# ---------------------------------------------------------------------------
# 处理时长：衡量「工程师处理效率」
# 口径：从 assigned_at（派发给工程师的时间点）到 completed_at（工程师提交完结）的耗时（小时）。
#      若 assigned_at 为空（历史/异常数据缺字段），回退用 created_at 兜底。
#      若工单尚未完结（completed_at 为空），返回 0.0，表示仍在计时中、尚未产生时长。
# 注：两个函数口径不同是刻意为之（前者看用户等待，后者看工程师效率），并非 bug。
# ---------------------------------------------------------------------------
def processing_hours(order):
    start = order["assigned_at"] or order["created_at"]
    end = order["completed_at"]
    if not end:
        return 0.0
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return 0.0
    return (e - s).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# 大模型调用（千问 / DashScope OpenAI 兼容接口）
# ---------------------------------------------------------------------------
def _qwen_base_url():
    """统一的 chat/completions 地址（兼容用户把完整路径写进 DASHSCOPE_BASE_URL）。"""
    base = os.environ.get(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _dashscope_api_key():
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY，请在 .env 中设置后重启服务")
    return key


def call_qwen(system_prompt, user_prompt, model=None):
    api_key = _dashscope_api_key()
    endpoint = _qwen_base_url()

    payload = {
        "model": model or QWEN_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": QWEN_MAX_TOKENS,
        "temperature": 0.3,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + api_key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError("大模型接口返回错误 %s: %s" % (e.code, detail))
    except Exception as e:
        raise RuntimeError("调用大模型失败：%s" % e)


def call_qwen_stream(system_prompt, user_prompt, model=None):
    """流式调用大模型，逐段 yield 文本增量（供 SSE 使用）。

    与 call_qwen 共用相同的 OpenAI 兼容协议，只是请求体带 "stream": true，
    响应为 SSE（每行 `data: {json}`，以 `data: [DONE]` 结束）。
    该函数是同步阻塞生成器，调用方需放到线程中执行，避免阻塞事件循环。
    """
    api_key = _dashscope_api_key()
    endpoint = _qwen_base_url()
    payload = {
        "model": model or QWEN_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": QWEN_MAX_TOKENS,
        "temperature": 0.3,
        "stream": True,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + api_key)
    try:
        # urllib 的响应对象支持按行迭代，可边收边解析 SSE 帧
        with urllib.request.urlopen(req, timeout=60) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield delta
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError("大模型接口返回错误 %s: %s" % (e.code, detail))
    except Exception as e:
        raise RuntimeError("调用大模型失败：%s" % e)


# ---------------------------------------------------------------------------
# 向量化（RAG）：调用 DashScope 的 OpenAI 兼容 embeddings 接口
# ---------------------------------------------------------------------------
def _embedding_endpoints():
    """embedding 候选地址：优先 EMBEDDING_BASE_URL，失败再回退到 DASHSCOPE_BASE_URL。

    现实中常出现「对话走私有 MaaS 网关、embeddings 走公共 DashScope」或反之，
    这里自动去重并依次尝试，避免用户必须手工试错才能建库。
    """
    bases = [EMBEDDING_BASE_URL]
    chat_base = os.environ.get("DASHSCOPE_BASE_URL", "").rstrip("/")
    if chat_base.endswith("/chat/completions"):
        chat_base = chat_base[: -len("/chat/completions")]
    if chat_base:
        bases.append(chat_base)
    seen, out = set(), []
    for b in bases:
        b = (b or "").rstrip("/")
        if b and b not in seen:
            seen.add(b)
            out.append(b + "/embeddings")
    return out


def call_embedding(text, model=None):
    """把一段文本向量化为 float 列表。失败时抛 RuntimeError（便于上层提示）。"""
    api_key = _dashscope_api_key()
    payload = {"model": model or EMBEDDING_MODEL, "input": text}
    data = json.dumps(payload).encode("utf-8")
    last_err = None
    for endpoint in _embedding_endpoints():
        req = urllib.request.Request(endpoint, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + api_key)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            emb = body["data"][0]["embedding"]
            if not isinstance(emb, list) or not emb:
                raise RuntimeError("embedding 返回格式异常")
            return emb
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:200]
            last_err = RuntimeError("embedding 接口返回错误 %s: %s" % (e.code, detail))
        except Exception as e:
            last_err = e if isinstance(e, RuntimeError) else RuntimeError("调用 embedding 失败：%s" % e)
    raise last_err or RuntimeError("没有可用的 embedding 接口")


def cosine_sim(a, b):
    """余弦相似度（纯 Python 实现，避免引入 numpy 等额外依赖）。"""
    if not a or not b:
        return 0.0
    dot = 0.0
    for x, y in zip(a, b):
        dot += x * y
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _fault_text(row):
    """把一张工单拼成用于向量化的文本（类型 / 科室 / 报障内容 / 原因 / 解决方法）。"""
    return (
        "故障类型：%s\n报障科室：%s\n报障内容：%s\n故障原因：%s\n解决方法：%s"
        % (
            ORDER_TYPES.get(row["type"], row["type"]),
            row.get("dept_name") or "—",
            row.get("content") or "",
            row.get("cause") or "",
            row.get("solution") or "",
        )
    )


def rebuild_knowledge_base(db, batch_commit=20):
    """重建历史工单知识库：清空后逐条向量化写入，返回写入条数。

    只索引「已完成且同时填写了故障原因与解决方法」的工单——这类工单才具备
    可复用的经验价值。每 batch_commit 条提交一次，避免长事务与内存膨胀。
    """
    rows = _rows(db.execute(text(
        "SELECT w.id, w.order_no, w.type, d.name AS dept_name, "
        "w.content, w.cause, w.solution "
        "FROM work_orders w LEFT JOIN departments d ON w.department_id=d.id "
        "WHERE w.status='completed' "
        "AND w.cause IS NOT NULL AND w.cause<>'' "
        "AND w.solution IS NOT NULL AND w.solution<>'' "
        "ORDER BY w.id"
    )))
    db.execute(text("DELETE FROM workorder_knowledge"))
    now_iso = datetime.now().isoformat()
    count = 0
    for r in rows:
        text_blob = _fault_text(r)
        emb = call_embedding(text_blob)
        db.execute(
            text(
                "INSERT INTO workorder_knowledge "
                "(order_id, order_no, fault_type, dept_name, fault_text, "
                " cause, solution, embedding, created_at) "
                "VALUES (:oid,:ono,:ft,:dn,:ftx,:ca,:so,:emb,:ca2)"
            ),
            {
                "oid": r["id"], "ono": r["order_no"], "ft": r["type"],
                "dn": r["dept_name"] or "", "ftx": text_blob,
                "ca": r["cause"], "so": r["solution"],
                "emb": json.dumps(emb, ensure_ascii=False), "ca2": now_iso,
            },
        )
        count += 1
        if count % batch_commit == 0:
            db.commit()
    db.commit()
    return count


def retrieve_similar(db, query, top_k=3):
    """检索与 query 最相似的历史工单，返回 [{"score": float, "row": dict}, ...]。"""
    emb = call_embedding(query)
    rows = _rows(db.execute(text("SELECT * FROM workorder_knowledge")))
    scored = []
    for r in rows:
        try:
            vec = json.loads(r["embedding"])
        except (ValueError, TypeError):
            continue
        scored.append((cosine_sim(emb, vec), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"score": round(s, 4), "row": r} for s, r in scored[:top_k]]


def build_rag_suggestion(query, matches):
    """把检索到的相似工单作为上下文，调用大模型生成针对新故障的解决建议。"""
    context = "\n\n".join(
        "【历史工单 %s】\n故障类型：%s\n报障科室：%s\n报障内容：%s\n故障原因：%s\n解决方法：%s"
        % (
            m["order_no"], ORDER_TYPES.get(m["fault_type"], m["fault_type"]),
            m["dept_name"] or "—", m["fault_text"], m["cause"], m["solution"],
        )
        for m in matches
    )
    system = (
        "你是医院运维团队的「故障经验助手」。下面给出若干条与当前故障相似的历史工单"
        "（含故障原因与解决方法，已按相似度从高到低排列）。请基于这些历史经验，"
        "针对新的故障描述，给出条理清晰的排查与解决建议；若历史经验不足以覆盖当前故障，"
        "请明确说明，并补充通用排查方向。请控制在 300 字以内，使用中文。"
    )
    user = "新的故障描述：\n%s\n\n相似历史工单：\n%s" % (query, context)
    return call_qwen(system, user)


def _build_eval_prompt(engineer, completed_orders):
    """根据工程师与其已完结工单构造评估 prompt。

    返回 (system_prompt, user_prompt, model, n)。流式与非流式评估共用，
    避免两处统计口径/提示词漂移。
    """
    n = len(completed_orders)
    total_h = 0.0
    overtime = 0
    sat = 0
    type_dist = {}
    for o in completed_orders:
        h = processing_hours(o)
        total_h += h
        if h > TIMEOUT_HOURS:
            overtime += 1
        if o["satisfaction"] == "satisfied":
            sat += 1
        type_dist[ORDER_TYPES[o["type"]]] = type_dist.get(ORDER_TYPES[o["type"]], 0) + 1
    avg = total_h / n if n else 0.0
    dist_str = "、".join("%s %d单" % (k, v) for k, v in type_dist.items()) or "—"

    system = (
        "你是医院运维团队的主管助理。请根据工程师的工单数据，用不超过200个汉字，"
        "客观、克制地总结其工作积极性、处理时效与满意度，避免夸张措辞；"
        "如有明显短板，给出一句可执行的改进建议。"
    )
    user = (
        "工程师 %s（%s）的已完结工单统计：\n"
        "- 完成工单：%d 单\n"
        "- 平均处理时长：%.1f 小时\n"
        "- 超时（>%d小时）工单：%d 单\n"
        "- 满意度：%d/%d 满意\n"
        "- 故障类型分布：%s\n"
        "请据此总结该工程师的工作表现。"
        % (engineer["name"], engineer.get("group_name") or "—",
           n, avg, TIMEOUT_HOURS, overtime, sat, n, dist_str)
    )
    return system, user, QWEN_MODEL, n


def build_engineer_evaluation(engineer, completed_orders):
    """汇总某工程师的已完结工单，调用大模型生成工作表现总结，返回 (总结文本, 实际使用的模型)。"""
    system, user, _, n = _build_eval_prompt(engineer, completed_orders)
    if n == 0:
        return "该工程师当前暂无已完结工单，尚无足够数据可供评估。", QWEN_MODEL

    models = [QWEN_MODEL] + [m for m in QWEN_FALLBACK if m != QWEN_MODEL]
    last_err = None
    for m in models:
        try:
            summary = call_qwen(system, user, model=m)
        except RuntimeError as e:
            msg = str(e)
            if "model_not_found" in msg or "Model not exist" in msg or "Access denied" in msg:
                last_err = e
                continue
            raise
        # 大模型输出零校验：空内容（或仅空白）或异常超长都视为不可信赖，拒绝落库
        summary = (summary or "").strip()
        if not summary or len(summary) > 500:
            raise RuntimeError("大模型返回内容异常（空或超长），已中止落库")
        return summary, m
    raise last_err or RuntimeError("没有可用的千问模型，请检查 QWEN_MODEL 配置")


# ---------------------------------------------------------------------------
# SSE（Server-Sent Events）工具：把同步阻塞的 token 生成器桥接为异步 SSE 流
# ---------------------------------------------------------------------------
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # 关闭 Nginx 等反向代理的缓冲，否则前端仍需等整段生成完才收到内容
    "X-Accel-Buffering": "no",
}


def _sse_frame(event, data):
    """构造一个 SSE 帧；data 统一 JSON 编码，避免换行/引号破坏协议。"""
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))


def _stream_sse(generator_factory, loop):
    """在后台线程执行阻塞生成器，把 token 通过队列送回事件循环并转成 SSE。

    说明：urllib 是同步阻塞的，若直接在 async 生成器里迭代会卡住事件循环，
    导致同一进程内其他请求全部排队。这里用「线程生产 + asyncio.Queue 消费」
    的方式解耦，保证流式期间服务仍可响应其他请求。
    """
    queue = asyncio.Queue()

    def _produce():
        try:
            for token in generator_factory():
                asyncio.run_coroutine_threadsafe(queue.put(("token", token)), loop)
            asyncio.run_coroutine_threadsafe(queue.put(("done", "")), loop)
        except Exception as ex:  # noqa: BLE001 - 需把任何异常回传给前端
            asyncio.run_coroutine_threadsafe(queue.put(("error", str(ex)[:300])), loop)

    threading.Thread(target=_produce, daemon=True).start()

    async def _consume():
        while True:
            kind, data = await queue.get()
            if kind == "token":
                yield _sse_frame("token", {"token": data})
            elif kind == "done":
                yield _sse_frame("done", {"ok": True})
                break
            else:  # error
                yield _sse_frame("error", {"error": data})
                break

    return _consume()


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
@app.api_route("/login", methods=["GET", "POST"])
async def login(request: Request, db: Session = Depends(get_db)):
    if request.method == "POST":
        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        user = _one(db.execute(text("SELECT * FROM users WHERE username=:u"), {"u": username}))
        if user and check_password_hash(user["password_hash"], password):
            if not user["active"]:
                flash(request, "该账号已被禁用，请联系管理员")
            else:
                request.session["user_id"] = user["id"]
                return RedirectResponse(str(request.url_for("dashboard")), status_code=302)
        else:
            flash(request, "用户名或密码错误")
    return render(request, "login.html")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(str(request.url_for("login")), status_code=302)


# ---------------------------------------------------------------------------
# 工单情况（原仪表盘）
# ---------------------------------------------------------------------------
@app.get("/", dependencies=[Depends(login_required)])
async def dashboard(request: Request, db: Session = Depends(get_db)):
    total = db.execute(text("SELECT COUNT(*) AS c FROM work_orders")).scalar()
    completed = db.execute(
        text("SELECT COUNT(*) AS c FROM work_orders WHERE status='completed'")
    ).scalar()
    open_count = db.execute(
        text("SELECT COUNT(*) AS c FROM work_orders WHERE status IN ('assigned','in_progress')")
    ).scalar()
    orders = _rows(db.execute(text("SELECT * FROM work_orders")))
    overdue_count = sum(1 for o in orders if order_timed_out(o))

    # 近 14 天每日工单数量 & 超时数量
    today = datetime.now().date()
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]
    labels, daily_created, daily_timeout = [], [], []
    for d in days:
        start = datetime(d.year, d.month, d.day)
        end = start + timedelta(days=1)
        s_iso, e_iso = start.isoformat(), end.isoformat()
        cnt = db.execute(
            text("SELECT COUNT(*) AS c FROM work_orders WHERE created_at>=:s AND created_at<:e"),
            {"s": s_iso, "e": e_iso},
        ).scalar()
        tos = _rows(db.execute(
            text("SELECT * FROM work_orders WHERE created_at>=:s AND created_at<:e"),
            {"s": s_iso, "e": e_iso},
        ))
        tcount = sum(1 for o in tos if order_timed_out(o))
        labels.append(d.strftime("%m-%d"))
        daily_created.append(cnt)
        daily_timeout.append(tcount)

    type_labels = [ORDER_TYPES[t] for t in ORDER_TYPES]
    type_data = [
        db.execute(text("SELECT COUNT(*) AS c FROM work_orders WHERE type=:t"), {"t": t}).scalar()
        for t in ORDER_TYPES
    ]

    grp_rows = _rows(db.execute(
        text(
            "SELECT g.name AS gname, COUNT(w.id) AS c FROM groups g "
            "LEFT JOIN users u ON u.group_id=g.id "
            "LEFT JOIN work_orders w ON w.assignee_id=u.id "
            "GROUP BY g.id ORDER BY c DESC"
        )
    ))
    group_labels = [r["gname"] for r in grp_rows]
    group_data = [r["c"] for r in grp_rows]

    return render(
        request, "dashboard.html",
        total=total, completed=completed, open_count=open_count, overdue_count=overdue_count,
        labels=labels, daily_created=daily_created, daily_timeout=daily_timeout,
        type_labels=type_labels, type_data=type_data,
        group_labels=group_labels, group_data=group_data,
    )


# ---------------------------------------------------------------------------
# 智能助手（RAG）：输入故障描述 -> 向量检索相似历史工单 -> 大模型给建议
# ---------------------------------------------------------------------------
@app.get("/rag", dependencies=[Depends(login_required)])
async def rag_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    kb_count = db.execute(text("SELECT COUNT(*) AS c FROM workorder_knowledge")).scalar() or 0
    return render(request, "rag.html", kb_count=kb_count, is_admin=(user["role"] == "admin"))


@app.post("/rag/ask", dependencies=[Depends(login_required)])
async def rag_ask(request: Request, db: Session = Depends(get_db)):
    """接收工程师的故障描述，检索相似历史工单并生成解决建议（JSON 返回）。"""
    query = ""
    try:
        body = await request.json()
        query = (body.get("query") or "").strip()
    except Exception:
        form = await request.form()
        query = (form.get("query") or "").strip()
    if not query:
        return JSONResponse({"ok": False, "error": "请输入故障描述"}, status_code=400)
    if len(query) > 1000:
        return JSONResponse({"ok": False, "error": "故障描述过长（请控制在 1000 字以内）"}, status_code=400)

    kb_count = db.execute(text("SELECT COUNT(*) AS c FROM workorder_knowledge")).scalar() or 0
    if kb_count == 0:
        return JSONResponse(
            {"ok": False, "error": "知识库为空，请先由管理员在「智能助手」页构建知识库"},
            status_code=400,
        )
    try:
        hits = retrieve_similar(db, query, top_k=3)
        if not hits:
            return JSONResponse({"ok": False, "error": "未检索到相似历史工单"}, status_code=404)
        matches = [h["row"] for h in hits]
        suggestion = build_rag_suggestion(query, matches)
        return JSONResponse({
            "ok": True,
            "query": query,
            "suggestion": suggestion,
            "matches": [
                {
                    "order_no": h["row"]["order_no"],
                    "type": ORDER_TYPES.get(h["row"]["fault_type"], h["row"]["fault_type"]),
                    "dept_name": h["row"]["dept_name"] or "—",
                    "content": h["row"]["fault_text"],
                    "cause": h["row"]["cause"],
                    "solution": h["row"]["solution"],
                    "score": h["score"],
                }
                for h in hits
            ],
        })
    except Exception as ex:  # noqa: BLE001 - 统一转成前端可读的错误
        return JSONResponse({"ok": False, "error": str(ex)[:300]}, status_code=500)


@app.post("/rag/rebuild", dependencies=[Depends(role_required("admin"))])
async def rag_rebuild(request: Request, db: Session = Depends(get_db)):
    """管理员触发：把历史已完成工单的故障原因/解决方法批量向量化入库。"""
    try:
        n = rebuild_knowledge_base(db)
        flash(request, "知识库已重建，共向量化 %d 条历史工单" % n)
    except Exception as ex:  # noqa: BLE001
        flash(request, "知识库构建失败：" + str(ex)[:200])
    return RedirectResponse(str(request.url_for("rag_page")), status_code=302)


# ---------------------------------------------------------------------------
# 排班表（全部角色可见）：按分组 / 月份查看值班安排
# ---------------------------------------------------------------------------
@app.get("/schedule", dependencies=[Depends(login_required)])
async def schedule_view(request: Request, db: Session = Depends(get_db)):
    groups = _rows(db.execute(text("SELECT * FROM groups ORDER BY id")))
    if not groups:
        return render(request, "schedule.html", groups=[], sel_group=None, month="2026-11",
                      cal=[], eng_hours=[], months=schedule_gen.MONTHS)

    gval = request.query_params.get("group")
    sel_group = int(gval) if (gval and gval.isdigit()) else None
    if sel_group is None or not any(g["id"] == sel_group for g in groups):
        sel_group = groups[0]["id"]
    month = request.query_params.get("month") or schedule_gen.MONTHS[-1]
    if month not in schedule_gen.MONTHS:
        month = schedule_gen.MONTHS[-1]

    rows = _rows(db.execute(
        text(
            "SELECT s.*, u.name AS eng_name FROM schedules s "
            "JOIN users u ON u.id=s.engineer_id "
            "WHERE s.group_id=:gid AND s.date LIKE :m "
            "ORDER BY s.date, s.shift, s.slot"
        ), {"gid": sel_group, "m": month + "%"}
    ))
    by_date = {}
    for r in rows:
        d = r["date"]
        cell = by_date.setdefault(d, {"day": [], "night": None, "full": None, "service": None})
        if r["shift"] == "night":
            cell["night"] = r["eng_name"]
        elif r["shift"] == "service":
            cell["service"] = r["eng_name"]
        elif r["start_time"] == "00:00" and r["end_time"] == "24:00":
            cell["full"] = r["eng_name"]
        else:
            cell["day"].append(r["eng_name"])

    cal = []
    for d in schedule_gen.month_dates(month):
        ds = d.isoformat()
        info = by_date.get(ds, {"day": [], "night": None, "full": None, "service": None})
        if ds in schedule_gen.HOLIDAYS:
            dtype = "节假日"
        elif d.weekday() < 5:
            dtype = "工作日"
        else:
            dtype = "周末"
        cal.append({
            "date": ds,
            "wd": schedule_gen.WEEKDAY_CN[d.weekday()],
            "type": dtype,
            "day": info["day"],
            "night": info["night"],
            "full": info["full"],
            "service": info["service"],
        })

    eng_list = _rows(db.execute(
        text(
            "SELECT id, name, special FROM users WHERE group_id=:gid AND role='engineer' "
            "ORDER BY (special IS NOT NULL AND special<>''), name"
        ), {"gid": sel_group}
    ))
    eng_hours = []
    for e in eng_list:
        st = schedule_gen.schedule_stats(db, sel_group, e["id"], month)
        eng_hours.append({"name": e["name"], "special": e["special"], "weekly": st["weekly"],
                          "total": st["total"], "over": st["over"]})

    return render(request, "schedule.html", groups=groups, sel_group=sel_group, month=month,
                  cal=cal, eng_hours=eng_hours, months=schedule_gen.MONTHS)


# ---------------------------------------------------------------------------
# 工单管理
# ---------------------------------------------------------------------------
@app.get("/workorders", dependencies=[Depends(login_required)])
async def workorders(request: Request, db: Session = Depends(get_db)):
    status = request.query_params.get("status", "")
    otype = request.query_params.get("type", "")

    sql = (
        "SELECT w.*, d.name AS dept_name, r.name AS reporter_name, "
        "a.name AS assignee_name, g.name AS assignee_group FROM work_orders w "
        "LEFT JOIN departments d ON w.department_id=d.id "
        "LEFT JOIN users r ON w.reporter_id=r.id "
        "LEFT JOIN users a ON w.assignee_id=a.id "
        "LEFT JOIN groups g ON a.group_id=g.id WHERE 1=1"
    )
    params = {}
    user = get_current_user(request)
    if user["role"] == "engineer":
        sql += " AND w.assignee_id=:uid"
        params["uid"] = user["id"]
    if status:
        sql += " AND w.status=:st"
        params["st"] = status
    if otype:
        sql += " AND w.type=:tp"
        params["tp"] = otype
    sql += " ORDER BY w.created_at DESC"
    rows = _rows(db.execute(text(sql), params))
    for r in rows:
        r["timed_out"] = order_timed_out(r)

    engineers = _rows(db.execute(
        text(
            "SELECT u.*, g.name AS group_name FROM users u "
            "LEFT JOIN groups g ON u.group_id=g.id "
            "WHERE u.role='engineer' AND u.active=1 ORDER BY u.name"
        )
    ))
    return render(request, "workorders.html", orders=rows, engineers=engineers,
                  status_filter=status, type_filter=otype)


@app.api_route("/workorders/create", methods=["GET", "POST"], dependencies=[Depends(login_required)])
async def create_workorder(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    if request.method == "POST":
        form = await request.form()
        otype = form.get("type", "")
        department_id = form.get("department_id", "")
        content = (form.get("content") or "").strip()
        if otype not in ORDER_TYPES:
            flash(request, "请选择有效的故障类型")
            return RedirectResponse(str(request.url_for("create_workorder")), status_code=302)
        if not department_id:
            flash(request, "请选择报障科室")
            return RedirectResponse(str(request.url_for("create_workorder")), status_code=302)
        if not content:
            flash(request, "报障内容不能为空")
            return RedirectResponse(str(request.url_for("create_workorder")), status_code=302)

        if user["role"] == "engineer":
            assignee_id = user["id"]
            assigned_by = user["id"]
        else:
            assignee_id = form.get("assignee_id", "")
            assigned_by = user["id"]
            eng = _one(db.execute(
                text("SELECT * FROM users WHERE id=:aid AND role='engineer'"), {"aid": assignee_id}
            ))
            if not eng:
                flash(request, "请选择负责处理的工程师")
                return RedirectResponse(str(request.url_for("create_workorder")), status_code=302)

        now = datetime.now()
        now_iso = now.isoformat()
        day_start = datetime(now.year, now.month, now.day).isoformat()
        seq = db.execute(
            text("SELECT COUNT(*) AS c FROM work_orders WHERE created_at>=:ds"), {"ds": day_start}
        ).scalar() + 1
        order_no = "WO-%s-%03d" % (now.strftime("%Y%m%d"), seq)
        # 并发提示：当前用「当日 COUNT(*)+1」生成 order_no，高并发下可能撞唯一约束。
        # 生产环境应改用数据库自增序列 / 雪花 ID，或在事务内用 SELECT ... FOR UPDATE 原子取号。
        try:
            db.execute(
                text(
                    "INSERT INTO work_orders "
                    "(order_no,type,department_id,reporter_id,assignee_id,assigned_by,content,status,created_at,assigned_at) "
                    "VALUES (:order_no,:type,:dept,:rep,:aid,:aby,:content,'assigned',:now,:now)"
                ),
                {"order_no": order_no, "type": otype, "dept": department_id, "rep": user["id"],
                 "aid": assignee_id, "aby": assigned_by, "content": content, "now": now_iso},
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            flash(request, "工单号冲突，请重试")
            return RedirectResponse(str(request.url_for("create_workorder")), status_code=302)
        if user["role"] == "engineer":
            flash(request, "工单已创建并登记在您本人名下，请及时处理")
        else:
            flash(request, "工单已创建并下派给指定工程师")
        return RedirectResponse(str(request.url_for("workorders")), status_code=302)

    depts = _rows(db.execute(text("SELECT * FROM departments ORDER BY name")))
    engineers = _rows(db.execute(
        text(
            "SELECT u.*, g.name AS group_name FROM users u "
            "LEFT JOIN groups g ON u.group_id=g.id "
            "WHERE u.role='engineer' AND u.active=1 ORDER BY g.name, u.name"
        )
    ))
    return render(request, "workorder_form.html", depts=depts, engineers=engineers,
                  is_engineer=(user["role"] == "engineer"))


@app.get("/workorders/{oid}", dependencies=[Depends(login_required)])
async def workorder_detail(request: Request, oid: int, db: Session = Depends(get_db)):
    row = _one(db.execute(
        text(
            "SELECT w.*, d.name AS dept_name, r.name AS reporter_name, "
            "a.name AS assignee_name, g.name AS assignee_group, ab.name AS assigned_by_name "
            "FROM work_orders w "
            "LEFT JOIN departments d ON w.department_id=d.id "
            "LEFT JOIN users r ON w.reporter_id=r.id "
            "LEFT JOIN users a ON w.assignee_id=a.id "
            "LEFT JOIN groups g ON a.group_id=g.id "
            "LEFT JOIN users ab ON w.assigned_by=ab.id WHERE w.id=:oid"
        ), {"oid": oid}
    ))
    if not row:
        flash(request, "工单不存在")
        return RedirectResponse(str(request.url_for("workorders")), status_code=302)
    row["timed_out"] = order_timed_out(row)
    row["hours"] = processing_hours(row)
    engineers = _rows(db.execute(
        text(
            "SELECT u.*, g.name AS group_name FROM users u "
            "LEFT JOIN groups g ON u.group_id=g.id "
            "WHERE u.role='engineer' AND u.active=1 ORDER BY g.name, u.name"
        )
    ))
    reason_tags = _rows(db.execute(
        text("SELECT content FROM workorder_tags WHERE kind='reason' ORDER BY id DESC LIMIT 40")
    ))
    solution_tags = _rows(db.execute(
        text("SELECT content FROM workorder_tags WHERE kind='solution' ORDER BY id DESC LIMIT 40")
    ))
    return render(request, "workorder_detail.html", o=row, engineers=engineers,
                  reason_tags=[t["content"] for t in reason_tags],
                  solution_tags=[t["content"] for t in solution_tags])


@app.post("/workorders/{oid}/assign", dependencies=[Depends(role_required("supervisor", "admin"))])
async def assign_workorder(request: Request, oid: int, db: Session = Depends(get_db)):
    form = await request.form()
    assignee_id = form.get("assignee_id", "")
    o = _one(db.execute(text("SELECT * FROM work_orders WHERE id=:oid"), {"oid": oid}))
    if not o:
        flash(request, "工单不存在")
        return RedirectResponse(str(request.url_for("workorders")), status_code=302)
    if o["status"] not in ("assigned", "in_progress"):
        flash(request, "当前工单状态不可改派")
        return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)
    eng = _one(db.execute(
        text("SELECT * FROM users WHERE id=:aid AND role='engineer'"), {"aid": assignee_id}
    ))
    if not eng:
        flash(request, "请选择有效的工程师")
        return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)
    db.execute(
        text(
            "UPDATE work_orders SET assignee_id=:aid, assigned_by=:aby, accepted_at=NULL, "
            "assigned_at=:now WHERE id=:oid"
        ),
        {"aid": assignee_id, "aby": get_current_user(request)["id"],
         "now": datetime.now().isoformat(), "oid": oid},
    )
    db.commit()
    flash(request, "工单负责人已更新")
    return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)


@app.post("/workorders/{oid}/accept", dependencies=[Depends(login_required)])
async def accept_workorder(request: Request, oid: int, db: Session = Depends(get_db)):
    o = _one(db.execute(text("SELECT * FROM work_orders WHERE id=:oid"), {"oid": oid}))
    if not o:
        flash(request, "工单不存在")
        return RedirectResponse(str(request.url_for("workorders")), status_code=302)
    user = get_current_user(request)
    if user["role"] not in ("engineer", "admin"):
        flash(request, "只有工程师可以接受工单")
        return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)
    if user["role"] == "engineer" and o["assignee_id"] != user["id"]:
        flash(request, "该工单未指派给您")
        return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)
    if o["status"] != "assigned":
        flash(request, "当前工单状态不可接受")
        return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)
    db.execute(
        text("UPDATE work_orders SET status='in_progress', accepted_at=:now WHERE id=:oid"),
        {"now": datetime.now().isoformat(), "oid": oid},
    )
    db.commit()
    flash(request, "已接受工单，开始处理")
    return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)


@app.post("/workorders/{oid}/complete", dependencies=[Depends(login_required)])
async def complete_workorder(request: Request, oid: int, db: Session = Depends(get_db)):
    form = await request.form()
    cause = (form.get("cause") or "").strip()
    solution = (form.get("solution") or "").strip()
    o = _one(db.execute(text("SELECT * FROM work_orders WHERE id=:oid"), {"oid": oid}))
    if not o:
        flash(request, "工单不存在")
        return RedirectResponse(str(request.url_for("workorders")), status_code=302)
    user = get_current_user(request)
    if user["role"] not in ("engineer", "admin"):
        flash(request, "只有工程师可以完成工单")
        return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)
    if user["role"] == "engineer" and o["assignee_id"] != user["id"]:
        flash(request, "该工单未指派给您")
        return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)
    if o["status"] not in ("assigned", "in_progress"):
        flash(request, "当前工单状态不可完成")
        return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)
    if not cause or not solution:
        flash(request, "请填写故障原因和解决方法")
        return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)
    db.execute(
        text(
            "UPDATE work_orders SET cause=:cause, solution=:sol, status='completed', "
            "completed_at=:now WHERE id=:oid"
        ),
        {"cause": cause, "sol": solution, "now": datetime.now().isoformat(), "oid": oid},
    )
    # 沉淀为常用标签，便于工程师下次直接复用
    now_iso = datetime.now().isoformat()
    _add_tag(db, "reason", cause, now_iso, user["id"])
    _add_tag(db, "solution", solution, now_iso, user["id"])
    db.commit()
    flash(request, "工单已完成")
    return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)


@app.post("/workorders/{oid}/satisfaction", dependencies=[Depends(role_required("admin"))])
async def set_satisfaction(request: Request, oid: int, db: Session = Depends(get_db)):
    form = await request.form()
    satisfaction = form.get("satisfaction", "satisfied")
    reason = (form.get("unsat_reason") or "").strip()
    o = _one(db.execute(text("SELECT * FROM work_orders WHERE id=:oid"), {"oid": oid}))
    if not o:
        flash(request, "工单不存在")
        return RedirectResponse(str(request.url_for("workorders")), status_code=302)
    if satisfaction not in ("satisfied", "unsatisfied"):
        satisfaction = "satisfied"
    if satisfaction == "unsatisfied" and not reason:
        flash(request, "标记为「不满意」时，必须填写不满意原因")
        return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)
    db.execute(
        text("UPDATE work_orders SET satisfaction=:sat, unsat_reason=:reason WHERE id=:oid"),
        {"sat": satisfaction, "reason": reason if satisfaction == "unsatisfied" else None, "oid": oid},
    )
    db.commit()
    flash(request, "满意度评定已更新")
    return RedirectResponse(str(request.url_for("workorder_detail", oid=oid)), status_code=302)


def _add_tag(db, kind, content, now_iso, created_by):
    """去重插入标签（兼容 MySQL / SQLite）。"""
    exists = db.execute(
        text("SELECT 1 FROM workorder_tags WHERE kind=:k AND content=:c LIMIT 1"),
        {"k": kind, "c": content},
    ).first()
    if exists:
        return
    db.execute(
        text(
            "INSERT INTO workorder_tags (kind,content,created_by,created_at) "
            "VALUES (:k,:c,:cb,:ca)"
        ),
        {"k": kind, "c": content, "cb": created_by, "ca": now_iso},
    )


# ---------------------------------------------------------------------------
# 员工评估（主管 / 管理员）：接入大模型，按工程师汇总工单表现
# ---------------------------------------------------------------------------
@app.get("/evaluations", dependencies=[Depends(role_required("admin", "supervisor"))])
async def evaluations(request: Request, db: Session = Depends(get_db)):
    engineers = _rows(db.execute(
        text(
            "SELECT u.*, g.name AS group_name FROM users u "
            "LEFT JOIN groups g ON u.group_id=g.id "
            "WHERE u.role='engineer' ORDER BY g.name, u.name"
        )
    ))
    data = []
    for e in engineers:
        rows = _rows(db.execute(
            text("SELECT w.* FROM work_orders w WHERE w.assignee_id=:eid AND w.status='completed'"),
            {"eid": e["id"]},
        ))
        n = len(rows)
        if n:
            avg = sum(processing_hours(r) for r in rows) / n
            overtime = sum(1 for r in rows if processing_hours(r) > TIMEOUT_HOURS)
            sat = sum(1 for r in rows if r["satisfaction"] == "satisfied")
        else:
            avg = 0.0
            overtime = 0
            sat = 0
        ev = _one(db.execute(
            text("SELECT * FROM evaluations WHERE engineer_id=:eid ORDER BY created_at DESC LIMIT 1"),
            {"eid": e["id"]},
        ))
        data.append({
            "engineer": e, "n": n, "avg": avg,
            "overtime": overtime, "sat": sat, "eval": ev,
        })
    return render(request, "evaluations.html", data=data)


@app.get("/evaluations/{eid}", dependencies=[Depends(role_required("admin", "supervisor"))])
async def evaluation_detail(request: Request, eid: int, db: Session = Depends(get_db)):
    e = _one(db.execute(
        text(
            "SELECT u.*, g.name AS group_name FROM users u "
            "LEFT JOIN groups g ON u.group_id=g.id "
            "WHERE u.id=:eid AND u.role='engineer'"
        ), {"eid": eid}
    ))
    if not e:
        flash(request, "工程师不存在")
        return RedirectResponse(str(request.url_for("evaluations")), status_code=302)
    rows = _rows(db.execute(
        text(
            "SELECT w.*, d.name AS dept_name FROM work_orders w "
            "LEFT JOIN departments d ON w.department_id=d.id "
            "WHERE w.assignee_id=:eid AND w.status='completed' ORDER BY w.completed_at DESC"
        ), {"eid": eid}
    ))
    for r in rows:
        r["hours"] = processing_hours(r)
        r["timed_out"] = order_timed_out(r)
    n = len(rows)
    avg = (sum(r["hours"] for r in rows) / n) if n else 0.0
    overtime = sum(1 for r in rows if r["hours"] > TIMEOUT_HOURS)
    sat = sum(1 for r in rows if r["satisfaction"] == "satisfied")
    ev = _one(db.execute(
        text("SELECT * FROM evaluations WHERE engineer_id=:eid ORDER BY created_at DESC LIMIT 1"),
        {"eid": eid},
    ))
    return render(request, "evaluation_detail.html", e=e, rows=rows, n=n, avg=avg,
                  overtime=overtime, sat=sat, eval=ev)


@app.post("/evaluations/{eid}/generate", dependencies=[Depends(role_required("admin", "supervisor"))])
async def generate_evaluation(request: Request, eid: int, db: Session = Depends(get_db)):
    e = _one(db.execute(
        text(
            "SELECT u.*, g.name AS group_name FROM users u "
            "LEFT JOIN groups g ON u.group_id=g.id "
            "WHERE u.id=:eid AND u.role='engineer'"
        ), {"eid": eid}
    ))
    if not e:
        flash(request, "工程师不存在")
        return RedirectResponse(str(request.url_for("evaluations")), status_code=302)
    rows = _rows(db.execute(
        text("SELECT w.* FROM work_orders w WHERE w.assignee_id=:eid AND w.status='completed'"),
        {"eid": eid},
    ))
    try:
        summary, used_model = build_engineer_evaluation(e, rows)
        db.execute(
            text(
                "INSERT INTO evaluations (engineer_id, summary, model, created_at) "
                "VALUES (:eid,:summary,:model,:now)"
            ),
            {"eid": eid, "summary": summary, "model": used_model, "now": datetime.now().isoformat()},
        )
        db.commit()
        flash(request, "已生成最新评估")
    except Exception as ex:
        flash(request, "评估生成失败：" + str(ex)[:200])
    return RedirectResponse(str(request.url_for("evaluation_detail", eid=eid)), status_code=302)


@app.get("/evaluations/{eid}/stream", dependencies=[Depends(role_required("admin", "supervisor"))])
async def stream_evaluation(request: Request, eid: int, db: Session = Depends(get_db)):
    """SSE 流式生成员工评估：逐 token 推给前端，前端实时显示，完成后落库。

    读取工程师与工单在请求线程内完成；真正的大模型调用放到后台线程，
    通过 _stream_sse 把 token 送回事件循环，避免阻塞其它请求。
    落库使用独立的 SessionLocal（后台线程不与请求级 Session 共享连接）。
    """
    e = _one(db.execute(
        text(
            "SELECT u.*, g.name AS group_name FROM users u "
            "LEFT JOIN groups g ON u.group_id=g.id "
            "WHERE u.id=:eid AND u.role='engineer'"
        ), {"eid": eid}
    ))
    if not e:
        async def _not_found():
            yield _sse_frame("error", {"error": "工程师不存在"})
        return StreamingResponse(_not_found(), media_type="text/event-stream", headers=SSE_HEADERS)

    orders = _rows(db.execute(
        text("SELECT w.* FROM work_orders w WHERE w.assignee_id=:eid AND w.status='completed'"),
        {"eid": eid},
    ))
    system, user, model, n = _build_eval_prompt(e, orders)

    def _generate():
        """同步生成器（在后台线程执行）：yield 文本增量；结束前校验并落库。"""
        if n == 0:
            msg = "该工程师当前暂无已完结工单，尚无足够数据可供评估。"
            yield msg
            _save_evaluation(eid, msg, model)
            return
        parts = []
        for delta in call_qwen_stream(system, user, model=model):
            parts.append(delta)
            yield delta
        summary = "".join(parts).strip()
        # 与 build_engineer_evaluation 相同的零校验：空/超长都拒绝落库
        if not summary or len(summary) > 500:
            raise RuntimeError("大模型返回内容异常（空或超长），已中止落库")
        _save_evaluation(eid, summary, model)

    return StreamingResponse(
        _stream_sse(_generate, asyncio.get_running_loop()),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _save_evaluation(eid, summary, model):
    """用独立会话保存评估结果（供后台流式线程调用）。"""
    sdb = SessionLocal()
    try:
        sdb.execute(
            text(
                "INSERT INTO evaluations (engineer_id, summary, model, created_at) "
                "VALUES (:eid,:summary,:model,:now)"
            ),
            {"eid": eid, "summary": summary, "model": model,
             "now": datetime.now().isoformat()},
        )
        sdb.commit()
    finally:
        sdb.close()


# ---------------------------------------------------------------------------
# 分组管理（仅管理员）
# ---------------------------------------------------------------------------
@app.get("/groups", dependencies=[Depends(role_required("admin"))])
async def groups(request: Request, db: Session = Depends(get_db)):
    rows = _rows(db.execute(
        text(
            "SELECT g.*, (SELECT COUNT(*) FROM users u WHERE u.group_id=g.id) AS member_count "
            "FROM groups g ORDER BY g.id"
        )
    ))
    return render(request, "groups.html", groups=rows)


@app.api_route("/groups/create", methods=["GET", "POST"], dependencies=[Depends(role_required("admin"))])
async def create_group(request: Request, db: Session = Depends(get_db)):
    if request.method == "POST":
        form = await request.form()
        name = (form.get("name") or "").strip()
        desc = (form.get("description") or "").strip()
        if not name:
            flash(request, "分组名称不能为空")
            return RedirectResponse(str(request.url_for("create_group")), status_code=302)
        try:
            db.execute(
                text("INSERT INTO groups (name,description,created_at) VALUES (:name,:desc,:now)"),
                {"name": name, "desc": desc, "now": datetime.now().isoformat()},
            )
            db.commit()
            flash(request, "分组已添加")
        except IntegrityError:
            flash(request, "分组名称已存在")
        return RedirectResponse(str(request.url_for("groups")), status_code=302)
    return render(request, "group_form.html", g=None)


@app.api_route("/groups/edit/{gid}", methods=["GET", "POST"], dependencies=[Depends(role_required("admin"))])
async def edit_group(request: Request, gid: int, db: Session = Depends(get_db)):
    grp = _one(db.execute(text("SELECT * FROM groups WHERE id=:gid"), {"gid": gid}))
    if not grp:
        flash(request, "分组不存在")
        return RedirectResponse(str(request.url_for("groups")), status_code=302)
    if request.method == "POST":
        form = await request.form()
        name = (form.get("name") or "").strip()
        desc = (form.get("description") or "").strip()
        if not name:
            flash(request, "分组名称不能为空")
            return RedirectResponse(str(request.url_for("edit_group", gid=gid)), status_code=302)
        try:
            db.execute(
                text("UPDATE groups SET name=:name, description=:desc WHERE id=:gid"),
                {"name": name, "desc": desc, "gid": gid},
            )
            db.commit()
            flash(request, "分组已更新")
        except IntegrityError:
            flash(request, "分组名称已存在")
        return RedirectResponse(str(request.url_for("groups")), status_code=302)
    return render(request, "group_form.html", g=grp)


@app.get("/groups/delete/{gid}", dependencies=[Depends(role_required("admin"))])
async def delete_group(request: Request, gid: int, db: Session = Depends(get_db)):
    cnt = db.execute(
        text("SELECT COUNT(*) AS c FROM users WHERE group_id=:gid"), {"gid": gid}
    ).scalar()
    if cnt > 0:
        flash(request, "该分组下还有工程师，无法删除（请先调整工程师所属分组）")
    else:
        db.execute(text("DELETE FROM groups WHERE id=:gid"), {"gid": gid})
        db.commit()
        flash(request, "分组已删除")
    return RedirectResponse(str(request.url_for("groups")), status_code=302)


# ---------------------------------------------------------------------------
# 科室管理（仅管理员）
# ---------------------------------------------------------------------------
@app.get("/departments", dependencies=[Depends(role_required("admin"))])
async def departments(request: Request, db: Session = Depends(get_db)):
    depts = _rows(db.execute(text("SELECT * FROM departments ORDER BY id DESC")))
    counts = {}
    for d in depts:
        counts[d["id"]] = db.execute(
            text("SELECT COUNT(*) AS c FROM work_orders WHERE department_id=:did"),
            {"did": d["id"]},
        ).scalar()
    return render(request, "departments.html", depts=depts, counts=counts)


@app.api_route("/departments/create", methods=["GET", "POST"], dependencies=[Depends(role_required("admin"))])
async def create_department(request: Request, db: Session = Depends(get_db)):
    if request.method == "POST":
        form = await request.form()
        name = (form.get("name") or "").strip()
        desc = (form.get("description") or "").strip()
        if not name:
            flash(request, "科室名称不能为空")
            return RedirectResponse(str(request.url_for("create_department")), status_code=302)
        try:
            db.execute(
                text("INSERT INTO departments (name,description,created_at) VALUES (:name,:desc,:now)"),
                {"name": name, "desc": desc, "now": datetime.now().isoformat()},
            )
            db.commit()
            flash(request, "科室已添加")
        except IntegrityError:
            flash(request, "科室名称已存在")
        return RedirectResponse(str(request.url_for("departments")), status_code=302)
    return render(request, "department_form.html", d=None)


@app.api_route("/departments/edit/{did}", methods=["GET", "POST"], dependencies=[Depends(role_required("admin"))])
async def edit_department(request: Request, did: int, db: Session = Depends(get_db)):
    d = _one(db.execute(text("SELECT * FROM departments WHERE id=:did"), {"did": did}))
    if not d:
        flash(request, "科室不存在")
        return RedirectResponse(str(request.url_for("departments")), status_code=302)
    if request.method == "POST":
        form = await request.form()
        name = (form.get("name") or "").strip()
        desc = (form.get("description") or "").strip()
        if not name:
            flash(request, "科室名称不能为空")
            return RedirectResponse(str(request.url_for("edit_department", did=did)), status_code=302)
        try:
            db.execute(
                text("UPDATE departments SET name=:name, description=:desc WHERE id=:did"),
                {"name": name, "desc": desc, "did": did},
            )
            db.commit()
            flash(request, "科室已更新")
        except IntegrityError:
            flash(request, "科室名称已存在")
        return RedirectResponse(str(request.url_for("departments")), status_code=302)
    return render(request, "department_form.html", d=d)


@app.get("/departments/delete/{did}", dependencies=[Depends(role_required("admin"))])
async def delete_department(request: Request, did: int, db: Session = Depends(get_db)):
    cnt = db.execute(
        text("SELECT COUNT(*) AS c FROM work_orders WHERE department_id=:did"), {"did": did}
    ).scalar()
    if cnt > 0:
        flash(request, "该科室下还有工单，无法删除（请先处理相关工单）")
    else:
        db.execute(text("DELETE FROM departments WHERE id=:did"), {"did": did})
        db.commit()
        flash(request, "科室已删除")
    return RedirectResponse(str(request.url_for("departments")), status_code=302)


# ---------------------------------------------------------------------------
# 用户管理（仅管理员）
# ---------------------------------------------------------------------------
@app.get("/users", dependencies=[Depends(role_required("admin"))])
async def users(request: Request, db: Session = Depends(get_db)):
    rows = _rows(db.execute(
        text(
            "SELECT u.*, g.name AS group_name FROM users u "
            "LEFT JOIN groups g ON u.group_id=g.id ORDER BY u.id"
        )
    ))
    return render(request, "users.html", users=rows)


@app.api_route("/users/create", methods=["GET", "POST"], dependencies=[Depends(role_required("admin"))])
async def create_user(request: Request, db: Session = Depends(get_db)):
    if request.method == "POST":
        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        name = (form.get("name") or "").strip()
        role = form.get("role") or ""
        group_id = form.get("group_id") or None
        active = 1 if form.get("active") else 0
        if not (username and password and name) or role not in ROLE_LABELS:
            flash(request, "请填写完整的账号信息")
            return RedirectResponse(str(request.url_for("create_user")), status_code=302)
        if role != "engineer":
            group_id = None
        try:
            db.execute(
                text(
                    "INSERT INTO users (username,password_hash,name,role,group_id,active,created_at) "
                    "VALUES (:u,:ph,:name,:role,:gid,:active,:now)"
                ),
                {"u": username, "ph": generate_password_hash(password), "name": name,
                 "role": role, "gid": group_id, "active": active, "now": datetime.now().isoformat()},
            )
            db.commit()
            flash(request, "账号已创建")
        except IntegrityError:
            flash(request, "用户名已存在")
        return RedirectResponse(str(request.url_for("users")), status_code=302)
    groups = _rows(db.execute(text("SELECT * FROM groups ORDER BY name")))
    return render(request, "user_form.html", u=None, groups=groups)


@app.api_route("/users/edit/{uid}", methods=["GET", "POST"], dependencies=[Depends(role_required("admin"))])
async def edit_user(request: Request, uid: int, db: Session = Depends(get_db)):
    u = _one(db.execute(text("SELECT * FROM users WHERE id=:uid"), {"uid": uid}))
    if not u:
        flash(request, "用户不存在")
        return RedirectResponse(str(request.url_for("users")), status_code=302)
    if request.method == "POST":
        form = await request.form()
        name = (form.get("name") or "").strip()
        role = form.get("role") or ""
        group_id = form.get("group_id") or None
        active = 1 if form.get("active") else 0
        password = form.get("password") or ""
        if not name or role not in ROLE_LABELS:
            flash(request, "请填写完整的账号信息")
            return RedirectResponse(str(request.url_for("edit_user", uid=uid)), status_code=302)
        if role != "engineer":
            group_id = None
        sql = "UPDATE users SET name=:name, role=:role, group_id=:gid, active=:active"
        params = {"name": name, "role": role, "gid": group_id, "active": active, "uid": uid}
        if password:
            sql += ", password_hash=:ph"
            params["ph"] = generate_password_hash(password)
        sql += " WHERE id=:uid"
        db.execute(text(sql), params)
        db.commit()
        flash(request, "账号已更新")
        return RedirectResponse(str(request.url_for("users")), status_code=302)
    groups = _rows(db.execute(text("SELECT * FROM groups ORDER BY name")))
    return render(request, "user_form.html", u=u, groups=groups)


@app.get("/users/delete/{uid}", dependencies=[Depends(role_required("admin"))])
async def delete_user(request: Request, uid: int, db: Session = Depends(get_db)):
    if uid == get_current_user(request)["id"]:
        flash(request, "不能删除当前登录的账号")
        return RedirectResponse(str(request.url_for("users")), status_code=302)
    db.execute(text("DELETE FROM users WHERE id=:uid"), {"uid": uid})
    db.commit()
    flash(request, "账号已删除")
    return RedirectResponse(str(request.url_for("users")), status_code=302)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=False)
