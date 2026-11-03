"""
数据库层：SQLAlchemy 引擎 / 会话工厂 / 声明基类

- 使用 SQLAlchemy 2.x 风格（future=True）
- 生产使用 MySQL + PyMySQL 驱动（mysql+pymysql://...）
- 通过 get_db 依赖向路由注入数据库会话（请求级生命周期，自动关闭）
"""

from urllib.parse import urlparse

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from config import DATABASE_URL

# pool_pre_ping 在每次取出连接前做心跳检测，避免用到被服务端关闭的死连接。
# 连接池：容器内「多个并发请求 + SSE 流式任务」会同时占用多条连接，
# 默认 pool_size=5 / max_overflow=10 在突发并发下容易被耗尽而排队；
# 这里放大到 10 + 20（仍在 db 服务默认 max_connections=151 以内）。
# SQLite（本地冒烟）为文件库时同样使用 QueuePool，这些参数会被接受但不影响行为。
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

Base = declarative_base()


def _ensure_database_exists():
    """MySQL 场景下，若目标库尚未创建则自动 CREATE DATABASE（IF NOT EXISTS）。

    SQLite 文件由引擎自动创建，跳过。连接失败仅告警，不阻断启动。
    """
    u = urlparse(DATABASE_URL)
    if not u.scheme.startswith("mysql"):
        return
    try:
        import pymysql

        db_name = u.path.lstrip("/").split("?")[0]
        conn = pymysql.connect(
            host=u.hostname or "127.0.0.1",
            port=u.port or 3306,
            user=u.username or "root",
            password=u.password or "",
        )
        with conn.cursor() as cur:
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                % db_name
            )
        conn.close()
    except Exception as e:  # pragma: no cover - 依赖外部 MySQL 可用性
        print("[WARN] 无法自动创建数据库，请确认 MySQL 可连接或手动建库：", e)


def get_db():
    """FastAPI 依赖：每个请求一个会话，结束时自动关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
