"""
排班表自动生成（独立于 main.py 主源码）

规则：
- 各分组每日 24 小时均需有人值班在岗
- 工作日：白班 09:00-17:00 需 2 人在岗，夜班 17:00-次日09:00 需 1 人在岗
- 非工作日（周末 / 法定节假日）：白班 09:00-17:00 需 1 人在岗，夜班 17:00-次日09:00 需 1 人在岗
  （白班与夜班不得为同一人，白班 + 夜班合起来即 24h 全天覆盖）
- 值夜班的工程师次日休息至少一天（次日不可排班）
- 每个工程师一周工时至多 48 小时（软约束：尽量均衡，超出时在界面标红提示）
- 特殊岗位（服务台，special 非空）：仅工作日 08:00-17:30 值班，不计入分组 24h 轮值计数，
  不参与白班/夜班轮换，周末与节假日休息。

生成结果写入 schedules 表，可在「排班表」模块按分组 / 月份查看。
本模块使用 SQLAlchemy Session（兼容 MySQL / SQLite）。
"""

from datetime import date, timedelta

from sqlalchemy import text

# 需生成的月份
MONTHS = ["2026-10", "2026-11"]

# 法定节假日（非工作日）：国庆节
HOLIDAYS = {
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
}

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def is_workday(d: date) -> bool:
    if d.isoformat() in HOLIDAYS:
        return False
    return d.weekday() < 5  # 0=周一 ... 4=周五


def month_dates(month: str):
    y, m = map(int, month.split("-"))
    ds = []
    d = date(y, m, 1)
    while d.month == m:
        ds.append(d)
        d += timedelta(days=1)
    return ds


def shift_hours(start: str, end: str) -> float:
    """按起止时间计算班次工时（跨天自动 +24h）。"""
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    mins = (eh * 60 + em) - (sh * 60 + sm)
    if mins <= 0:
        mins += 24 * 60
    return mins / 60.0


def _week_key(d: date):
    iso = d.isocalendar()  # (iso_year, iso_week, iso_weekday)
    return (iso[0], iso[1])


def generate_for_group(eng, group_id, month):
    """为一个分组生成某月的排班，返回 (待插入行列表, 被迫违规次数)。

    eng: [{"id":..,"name":..}, ...]
    violations: 候选工程师全部处于休息期（不可用）时仍被迫强行排班的次数，
                用于统计违反「夜班次日休息」约束的情况（算法无法在不违反约束下满足 24h 在岗）。
    """
    dates = month_dates(month)
    rest_until = {e["id"]: None for e in eng}
    week_hours = {e["id"]: {} for e in eng}  # (iso_year,iso_week) -> hours
    rows = []
    violations = 0

    def avail(eid, d):
        ru = rest_until[eid]
        return ru is None or ru < d

    def pick(cond_eids, d, wk):
        # 在候选中挑选「当前周工时最少」者，其次看累计工时，实现均衡
        cand = [eid for eid in cond_eids if avail(eid, d)]
        if not cand:
            # 候选工程师全部处于休息期，无法满足「夜班次日休息」约束，
            # 被迫强行排班，记一次违规（后续用于告警与人工复核）。
            cand = list(cond_eids)
            nonlocal violations
            violations += 1
        cand.sort(key=lambda eid: (week_hours[eid].get(wk, 0.0),
                                   sum(week_hours[eid].values())))
        return cand[0]

    for d in dates:
        wk = _week_key(d)
        if is_workday(d):
            # 夜班（17:00-09:00, 16h），尽量均衡
            all_ids = [e["id"] for e in eng]
            N = pick(all_ids, d, wk)
            rows.append((group_id, d.isoformat(), "night", 0, N, "17:00", "09:00"))
            week_hours[N][wk] = week_hours[N].get(wk, 0.0) + 16.0
            rest_until[N] = d + timedelta(days=1)  # 次日休息

            # 白班 2 人（09:00-17:00, 8h），排除夜班本人
            day_need = 2
            others = [e["id"] for e in eng if e["id"] != N]
            chosen = []
            for _ in range(day_need):
                if not others:
                    break
                eid = pick(others, d, wk)
                chosen.append(eid)
                others.remove(eid)
            for slot, eid in enumerate(chosen):
                rows.append((group_id, d.isoformat(), "day", slot, eid, "09:00", "17:00"))
                week_hours[eid][wk] = week_hours[eid].get(wk, 0.0) + 8.0
        else:
            # 非工作日：白班 1 人（09:00-17:00, 8h）+ 夜班 1 人（17:00-09:00, 16h）
            # 白班与夜班不得为同一人；夜班次日休息至少一天
            all_ids = [e["id"] for e in eng]
            # 夜班
            N = pick(all_ids, d, wk)
            rows.append((group_id, d.isoformat(), "night", 0, N, "17:00", "09:00"))
            week_hours[N][wk] = week_hours[N].get(wk, 0.0) + 16.0
            rest_until[N] = d + timedelta(days=1)  # 次日休息
            # 白班 1 人，排除夜班本人（保证一天内白班/夜班不是同一人）
            others = [e["id"] for e in eng if e["id"] != N]
            if others:
                D = pick(others, d, wk)
                rows.append((group_id, d.isoformat(), "day", 0, D, "09:00", "17:00"))
                week_hours[D][wk] = week_hours[D].get(wk, 0.0) + 8.0
    return rows, violations


def generate_service_desk(eng, group_id, month):
    """为特殊岗位（如服务台）工程师生成排班：仅工作日 08:00-17:30，周末/节假日休息。"""
    dates = month_dates(month)
    rows = []
    for d in dates:
        if not is_workday(d):
            continue
        for e in eng:
            rows.append((group_id, d.isoformat(), "service", 0,
                         e["id"], "08:00", "17:30"))
    return rows


def generate_all(db):
    """为所有分组、所有月份重新生成排班（幂等：先删除旧数据）。db 为 SQLAlchemy Session。"""
    groups = db.execute(text("SELECT id FROM groups ORDER BY id")).mappings().all()
    for g in groups:
        gid = g["id"]
        # 常规轮值工程师（special 为空）与特殊岗位工程师分开处理
        regular = [dict(e) for e in db.execute(
            text(
                "SELECT id, name FROM users WHERE group_id=:gid AND role='engineer' "
                "AND active=1 AND (special IS NULL OR special='') ORDER BY id"
            ), {"gid": gid}).mappings().all()]
        special = [dict(e) for e in db.execute(
            text(
                "SELECT id, name FROM users WHERE group_id=:gid AND role='engineer' "
                "AND active=1 AND special IS NOT NULL AND special<>'' ORDER BY id"
            ), {"gid": gid}).mappings().all()]
        if not regular and not special:
            continue
        for month in MONTHS:
            db.execute(
                text("DELETE FROM schedules WHERE group_id=:gid AND date LIKE :m"),
                {"gid": gid, "m": month + "%"},
            )
            # 常规工程师承担 24h 轮值
            rows, violations = generate_for_group(regular, gid, month)
            if violations > 0:
                print("[WARN] 分组 %s 在 %s 有 %d 次被迫违反休息约束" % (gid, month, violations))
            for row in rows:
                db.execute(
                    text(
                        "INSERT INTO schedules "
                        "(group_id,date,shift,slot,engineer_id,start_time,end_time) "
                        "VALUES (:gid,:date,:shift,:slot,:eid,:st,:et)"
                    ),
                    {"gid": row[0], "date": row[1], "shift": row[2], "slot": row[3],
                     "eid": row[4], "st": row[5], "et": row[6]},
                )
            # 特殊岗位（服务台）单独排班，不计入轮值计数
            for row in generate_service_desk(special, gid, month):
                db.execute(
                    text(
                        "INSERT INTO schedules "
                        "(group_id,date,shift,slot,engineer_id,start_time,end_time) "
                        "VALUES (:gid,:date,:shift,:slot,:eid,:st,:et)"
                    ),
                    {"gid": row[0], "date": row[1], "shift": row[2], "slot": row[3],
                     "eid": row[4], "st": row[5], "et": row[6]},
                )
    db.commit()


def schedule_stats(db, group_id, engineer_id, month):
    """统计某工程师在某月的排班工时（按自然周），返回周明细与总计。db 为 SQLAlchemy Session。"""
    rows = db.execute(
        text(
            "SELECT date, start_time, end_time FROM schedules "
            "WHERE group_id=:gid AND engineer_id=:eid AND date LIKE :m"
        ),
        {"gid": group_id, "eid": engineer_id, "m": month + "%"},
    ).mappings().all()
    week_hours = {}
    total = 0.0
    for r in rows:
        h = shift_hours(r["start_time"], r["end_time"])
        total += h
        d = date.fromisoformat(r["date"])
        wk = _week_key(d)
        week_hours[wk] = week_hours.get(wk, 0.0) + h

    weekly = []
    for wk in sorted(week_hours):
        h = week_hours[wk]
        y, w = wk
        monday = date.fromisocalendar(y, w, 1)
        sunday = monday + timedelta(days=6)
        weekly.append({
            "label": "第%d周 (%s~%s)" % (w, monday.strftime("%m/%d"), sunday.strftime("%m/%d")),
            "hours": round(h, 1),
            "over": h > 48,
        })
    return {
        "weekly": weekly,
        "total": round(total, 1),
        "over": any(x["over"] for x in weekly),
    }
