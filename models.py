"""
ORM 模型：与原有 SQLite 表结构等价，改为 SQLAlchemy 声明式模型。

字段说明（沿用原设计）：
- 时间戳统一以 ISO 字符串（String）存储，便于直接做字符串比较（如 created_at>=?）
- active 用 Boolean（MySQL 映射为 TINYINT(1)，查询 active=1 等价）
- 唯一约束：work_orders.order_no、workorder_tags(kind,content)、schedules(group_id,date,shift,slot)
"""

from sqlalchemy import (Column, Integer, String, Text, Boolean, ForeignKey,
                        UniqueConstraint)

from database import Base


class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), unique=True, nullable=False)
    description = Column(String(255))
    created_at = Column(String(30), nullable=False)


class Department(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), unique=True, nullable=False)
    description = Column(String(255))
    created_at = Column(String(30), nullable=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(80), nullable=False)
    role = Column(String(20), nullable=False)  # admin / supervisor / engineer
    group_id = Column(Integer, ForeignKey("groups.id"))
    special = Column(String(40))  # 特殊岗位（如「服务台」），空字符串/None 表示常规
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(String(30), nullable=False)


class WorkOrder(Base):
    __tablename__ = "work_orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_no = Column(String(40), unique=True, nullable=False)
    type = Column(String(20), nullable=False)  # computer / printer / pda
    department_id = Column(Integer, nullable=False)
    reporter_id = Column(Integer, nullable=False)
    assignee_id = Column(Integer, nullable=False)
    # 派发人 ID，两种场景语义不同：
    # - 主管/管理员下派工单时 => 记录「操作下派的主管/管理员」ID（即派发人）
    # - 工程师自建工单时       => 等于 assignee_id 本人（即创建人）
    # 阅读/统计时须结合场景判断，不要简单等同为「创建人」或「派发人」。
    assigned_by = Column(Integer)
    content = Column(Text, nullable=False)
    cause = Column(Text)
    solution = Column(Text)
    status = Column(String(20), nullable=False, default="assigned")  # assigned/in_progress/completed
    created_at = Column(String(30), nullable=False)
    accepted_at = Column(String(30))
    completed_at = Column(String(30))
    satisfaction = Column(String(20), default="satisfied")  # satisfied / unsatisfied
    unsat_reason = Column(Text)
    assigned_at = Column(String(30))  # 派发给工程师的时间点（用于统计处理时长）


class WorkOrderTag(Base):
    __tablename__ = "workorder_tags"
    __table_args__ = (
        UniqueConstraint("kind", "content", name="uq_tag_kind_content"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    kind = Column(String(20), nullable=False)  # reason / solution
    content = Column(Text, nullable=False)
    created_by = Column(Integer)
    created_at = Column(String(30), nullable=False)


class Evaluation(Base):
    __tablename__ = "evaluations"
    id = Column(Integer, primary_key=True, autoincrement=True)
    engineer_id = Column(Integer, nullable=False)
    summary = Column(Text, nullable=False)
    model = Column(String(40))
    created_at = Column(String(30), nullable=False)


class KnowledgeItem(Base):
    """历史工单知识库（RAG 检索用）。

    每条记录对应一张「已完成且填写了故障原因/解决方法」的工单，
    将其拼接成一段文本后向量化，embedding 以 JSON 数组字符串存储。
    检索时在应用内做余弦相似度计算，无需引入外部向量数据库，
    对医院运维这种数据量（百~千级）而言性价比最高。
    """
    __tablename__ = "workorder_knowledge"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, nullable=False)      # 来源工单 ID
    order_no = Column(String(40))                   # 来源工单编号（展示用）
    fault_type = Column(String(20))                 # computer / printer / pda
    dept_name = Column(String(80))                  # 报障科室
    fault_text = Column(Text, nullable=False)       # 实际送去向量化的文本
    cause = Column(Text)                            # 故障原因
    solution = Column(Text)                         # 解决方法
    embedding = Column(Text, nullable=False)        # JSON 数组字符串（float 列表）
    created_at = Column(String(30), nullable=False)


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (
        UniqueConstraint("group_id", "date", "shift", "slot", name="uq_sched"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, nullable=False)
    date = Column(String(20), nullable=False)
    shift = Column(String(20), nullable=False)  # day / night / service / full
    slot = Column(Integer, nullable=False, default=0)
    engineer_id = Column(Integer, nullable=False)
    start_time = Column(String(10), nullable=False)
    end_time = Column(String(10), nullable=False)
