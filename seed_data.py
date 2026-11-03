"""
演示 / 测试数据（独立于 main.py 主源码）

本模块仅负责把演示数据写入数据库，运行 main.py 首次启动时自动调用。
这样「测试数据直接落到数据库」，而非硬编码在主程序源码中。
使用 SQLAlchemy ORM 写入，兼容 MySQL / SQLite。
"""

import random
from datetime import datetime, timedelta

from sqlalchemy import text

from werkzeug.security import generate_password_hash

from models import User, Group, Department, WorkOrder, WorkOrderTag

DEMO_DEPARTMENTS = [
    "内科", "外科", "急诊科", "放射科", "检验科", "药剂科", "门诊部", "住院部",
    "收费处", "中心药房", "ICU", "护理部", "信息科", "体检中心",
]

DEMO_GROUPS = {
    "网络运维组": "负责院内网络、无线覆盖、核心交换及 PDA 接入等运维",
    "硬件运维组": "负责计算机、打印机、终端外设等硬件维护与更换",
    "系统软件组": "负责 HIS/LIS/PACS 等系统、软件及账号权限运维",
}

# (用户名, 姓名, 分组, 特殊岗位)；special 为空表示常规轮值工程师
# 梁工为「服务台」特殊工程师：仅工作日 08:00-17:30 值班，不计入网络运维组 24h 轮值计数
DEMO_ENGINEERS = [
    # 网络运维组（含梁工服务台共 7 人，其中常规轮值 6 人）
    ("xugong", "徐工", "网络运维组", ""),
    ("baogong", "包工", "网络运维组", ""),
    ("wugong", "吴工", "网络运维组", ""),
    ("xiagong", "夏工", "网络运维组", ""),
    ("fenggong", "冯工", "网络运维组", ""),
    ("chengong", "陈工", "网络运维组", ""),
    ("lianggong", "梁工", "网络运维组", "服务台"),
    # 硬件运维组（6 人）
    ("zhaogong", "赵工", "硬件运维组", ""),
    ("qiangong", "钱工", "硬件运维组", ""),
    ("sungong", "孙工", "硬件运维组", ""),
    ("hegong", "何工", "硬件运维组", ""),
    ("gaogong", "高工", "硬件运维组", ""),
    ("liugong", "刘工", "硬件运维组", ""),
    # 系统软件组（6 人）
    ("ligong", "李工", "系统软件组", ""),
    ("zhougong", "周工", "系统软件组", ""),
    ("zhenggong", "郑工", "系统软件组", ""),
    ("lingong", "林工", "系统软件组", ""),
    ("huanggong", "黄工", "系统软件组", ""),
    ("magong", "马工", "系统软件组", ""),
]

# 各类工单的 (报障内容, 故障原因, 解决方法) 模板
ORDER_TEMPLATES = {
    "computer": [
        ("门诊医生站电脑蓝屏，反复重启无法进入系统", "内存条接触不良，系统引导文件损坏",
         "重新插拔并清理内存条金手指，重建引导记录，系统恢复正常"),
        ("护士站电脑运行极卡，打开程序需等待数分钟", "系统盘空间不足，后台进程过多",
         "清理临时文件与系统日志，禁用开机自启项，C盘扩容后运行流畅"),
        ("收费处电脑无法连接医保专网，挂号缴费失败", "医保专线网线松动，接入交换机端口异常",
         "重新插接网线并重启接入交换机端口，医保专网连通恢复"),
        ("医生工作站登录 HIS 系统缓慢，频繁提示超时", "与数据库服务器网络延迟高，DNS 解析慢",
         "调整本机 DNS 为内网 DNS，优化网络路由后登录正常"),
        ("住院医生电脑 USB 接口无反应，无法读取 U 盘", "前置 USB 接口供电不足，驱动异常",
         "更换机箱后置 USB 口并重装 USB 驱动，外设读写正常"),
        ("行政办公电脑频繁弹窗报错，疑似中病毒", "感染木马程序，浏览器被劫持",
         "断网查杀木马，重置浏览器并加固系统策略"),
        ("会议室一体机无法投屏，会议受影响", "无线投屏服务未启动，网卡驱动过旧",
         "重启投屏服务并更新无线网卡驱动，投屏恢复正常"),
    ],
    "printer": [
        ("住院部值班室打印机卡纸，无法继续打印", "进纸辊老化且纸张受潮卷曲",
         "清除卡纸并更换搓纸轮，使用干燥纸张后正常"),
        ("检验报告自助打印机提示脱机，无法输出", "打印机 IP 冲突，端口配置错误",
         "重新分配固定 IP 并重建 TCP/IP 端口，恢复联机打印"),
        ("药房标签打印机打印模糊、跳号", "打印头脏污，碳带张力不均",
         "清洁打印头并调整碳带张力，标签打印清晰完整"),
        ("门诊收费彩色打印机偏色，红色打印异常", "彩色墨盒堵塞，色彩校准丢失",
         "执行喷头清洗与色彩校准，偏色问题解决"),
        ("护士站打印机重装系统后驱动丢失无法打印", "系统重装后未恢复打印驱动",
         "重新安装对应型号驱动并配置共享，打印恢复"),
        ("医技科室打印机频繁掉线，时好时坏", "无线网络信号弱，连接不稳定",
         "改用有线网络连接打印机，掉线问题解决"),
        ("急诊打印机卡纸后报错 E02 无法消除", "纸道传感器被纸屑遮挡误报",
         "清理纸道传感器并复位，错误代码清除"),
    ],
    "pda": [
        ("急诊科 PDA 无法扫描病人腕带条码", "扫码模组软件崩溃，服务异常",
         "重启 PDA 扫码服务并升级固件，扫码恢复正常"),
        ("PDA 电池续航骤降，半天需充电两次", "电池老化，循环次数过多",
         "更换 PDA 电池，续航恢复正常"),
        ("PDA 无法连接院内 WiFi，提示认证失败", "无线账号过期，证书失效",
         "重置无线账号并续期证书，PDA 成功入网"),
        ("PDA 屏幕触控失灵，部分区域无反应", "触控层局部损坏",
         "校准触控并更换触控模组，触控恢复灵敏"),
        ("PDA 扫码无反应，但手电筒正常", "扫码窗脏污遮挡，补光异常",
         "清洁扫码窗并重置补光，扫码恢复正常"),
        ("PDA 开机卡在 LOGO 界面无法进入系统", "系统固件损坏",
         "重刷 PDA 系统固件并恢复出厂配置，正常开机"),
        ("移动护理 PDA 同步数据慢，医嘱延迟", "后台同步服务繁忙，本地缓存满",
         "清理本地缓存并错峰同步，数据延迟降低"),
    ],
}


def load(db):
    """将演示数据写入数据库。db 为 SQLAlchemy Session。"""
    random.seed(20261102)
    now = datetime.now()
    now_iso = now.isoformat()

    # 管理员（默认账号）
    admin = User(username="admin", password_hash=generate_password_hash("admin123"),
                 name="系统管理员", role="admin", group_id=None, active=True, created_at=now_iso)
    db.add(admin)
    db.flush()
    admin_id = admin.id

    # 主管（测试账号）
    sup = User(username="supervisor", password_hash=generate_password_hash("sup123"),
               name="王主管", role="supervisor", group_id=None, active=True, created_at=now_iso)
    db.add(sup)
    db.flush()
    sup_id = sup.id

    # 分组
    group_ids = {}
    for gname, gdesc in DEMO_GROUPS.items():
        g = Group(name=gname, description=gdesc, created_at=now_iso)
        db.add(g)
        db.flush()
        group_ids[gname] = g.id

    # 工程师
    eng_by_group = {g: [] for g in DEMO_GROUPS}      # 含全部（用于分组统计）
    reg_by_group = {g: [] for g in DEMO_GROUPS}      # 仅常规轮值工程师（用于工单派发）
    for uname, name, grp, special in DEMO_ENGINEERS:
        e = User(username=uname, password_hash=generate_password_hash("eng123"),
                 name=name, role="engineer", group_id=group_ids[grp],
                 special=(special or None), active=True, created_at=now_iso)
        db.add(e)
        db.flush()
        eng_by_group[grp].append(e.id)
        if not special:
            reg_by_group[grp].append(e.id)

    # 科室
    for d in DEMO_DEPARTMENTS:
        db.add(Department(name=d, description="", created_at=now_iso))
    db.flush()
    dept_ids = [r[0] for r in db.execute(text("SELECT id FROM departments")).fetchall()]

    # 工单分布计划：(分组, 总数, 超时数, 未结数)
    plan = [
        ("网络运维组", 30, 0, 5),
        ("硬件运维组", 12, 1, 2),
        ("系统软件组", 8, 1, 1),
    ]

    orders = []
    day_seq = {}
    for grp, total, overtime_n, open_n in plan:
        members = reg_by_group[grp]  # 工单仅派发给常规轮值工程师（服务台不接工单）
        completed_n = total - open_n
        for i in range(total):
            assignee = random.choice(members)
            dept = random.choice(dept_ids)
            otype = random.choice(list(ORDER_TEMPLATES))
            content, cause, solution = random.choice(ORDER_TEMPLATES[otype])

            is_open = i < open_n
            is_overtime = (not is_open) and (i - open_n < overtime_n)

            if is_open:
                created = now - timedelta(hours=random.randint(1, 22))
                status = "in_progress" if i % 2 == 0 else "assigned"
                accepted = (created + timedelta(hours=random.randint(1, 3))) if status == "in_progress" else None
                completed = None
            elif is_overtime:
                created = now - timedelta(days=random.randint(20, 29),
                                          hours=random.randint(0, 12))
                # 延迟完结（>24h）
                delay = timedelta(hours=random.randint(26, 72))
                accepted = created + timedelta(hours=random.randint(1, 6))
                completed = created + delay
                status = "completed"
            else:
                created = now - timedelta(days=random.randint(1, 28),
                                          hours=random.randint(0, 18))
                delay = timedelta(hours=random.randint(1, 20))
                accepted = created + timedelta(hours=random.randint(1, 4))
                completed = created + delay
                status = "completed"

            # 报障人：30% 概率为工程师本人，其余为管理员/主管
            reporter = assignee if random.random() < 0.3 else random.choice([admin_id, sup_id])
            assigned_by = reporter if reporter == assignee else random.choice([admin_id, sup_id])

            c_iso = created.isoformat()
            a_iso = accepted.isoformat() if accepted else None
            co_iso = completed.isoformat() if completed else None
            dkey = created.strftime("%Y%m%d")
            day_seq[dkey] = day_seq.get(dkey, 0) + 1
            order_no = "WO-%s-%03d" % (dkey, day_seq[dkey])

            orders.append({
                "order_no": order_no, "type": otype, "department_id": dept,
                "reporter_id": reporter, "assignee_id": assignee, "assigned_by": assigned_by,
                "content": content,
                "cause": cause if status == "completed" else None,
                "solution": solution if status == "completed" else None,
                "status": status, "created_at": c_iso,
                "accepted_at": a_iso, "completed_at": co_iso,
                "satisfaction": "satisfied", "assigned_at": c_iso,
            })

    # 按创建时间排序后写入，使单号更自然
    orders.sort(key=lambda x: x["created_at"])
    for o in orders:
        db.add(WorkOrder(**o))
    db.flush()

    # 沉淀常用「故障原因 / 解决方法」标签，便于工程师复用
    # 同一故障原因/解决方法在多个工单中重复出现，用集合去重后入库
    seen_tags = set()
    for o in orders:
        if o["status"] == "completed" and o["cause"]:
            key = ("reason", o["cause"])
            if key not in seen_tags:
                seen_tags.add(key)
                db.add(WorkOrderTag(kind="reason", content=o["cause"], created_by=None, created_at=now_iso))
        if o["status"] == "completed" and o["solution"]:
            key = ("solution", o["solution"])
            if key not in seen_tags:
                seen_tags.add(key)
                db.add(WorkOrderTag(kind="solution", content=o["solution"], created_by=None, created_at=now_iso))

    db.commit()
