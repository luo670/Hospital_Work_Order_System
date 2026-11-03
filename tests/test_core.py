"""
工单系统核心纯函数单元测试（pytest）。

覆盖：
- main.order_timed_out / main.processing_hours（超时与处理时长口径）
- schedules.shift_hours（班次工时，含跨天）
- schedules.generate_for_group（排班约束 + 违规计数）

全部为纯函数，使用 dict 构造输入，不连接任何数据库。
"""

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

# 让 tests/ 能 import 到项目根目录的 main / schedules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import order_timed_out, processing_hours  # noqa: E402
from schedules import shift_hours, generate_for_group  # noqa: E402


# ---------------------------------------------------------------------------
# 构造工单 dict（与 main.py 中使用的字段结构一致）
# ---------------------------------------------------------------------------
def _mk_order(created_at, status="completed", completed_at=None, assigned_at=None):
    return {
        "created_at": created_at,
        "status": status,
        "completed_at": completed_at,
        "assigned_at": assigned_at,
    }


# ---------------------------------------------------------------------------
# order_timed_out：衡量「用户等待 / 工单整体是否超时」
# 口径：created_at 到 (completed_at 或 now()) 超过 24h 即超时
# ---------------------------------------------------------------------------
def test_order_timed_out_uncompleted_within_24h():
    # 未完成 + 未超 24h：冻结 now() 到 created 当天中午，创建于 2 小时前
    o = _mk_order("2026-11-01T10:00:00", status="assigned", completed_at=None)

    # datetime 是不可变 C 类型，无法直接 patch 其类方法 now，
    # 用子类覆盖 now 再替换 main.datetime 命名空间
    class _Frozen(datetime):
        @classmethod
        def now(cls, *a, **k):
            return datetime(2026, 11, 1, 12, 0, 0)

    with patch("main.datetime", _Frozen):
        assert order_timed_out(o) is False


def test_order_timed_out_completed_over_24h():
    # 已完成但超 24h（创建 11-01 10:00，完结 11-03 10:00）
    o = _mk_order("2026-11-01T10:00:00", status="completed", completed_at="2026-11-03T10:00:00")
    assert order_timed_out(o) is True


def test_order_timed_out_completed_within_24h():
    # 已完成且未超 24h（创建 11-01 10:00，完结 11-01 12:00）
    o = _mk_order("2026-11-01T10:00:00", status="completed", completed_at="2026-11-01T12:00:00")
    assert order_timed_out(o) is False


# ---------------------------------------------------------------------------
# processing_hours：衡量「工程师处理效率」
# 口径：assigned_at（空则回退 created_at）到 completed_at；未完成返回 0.0
# ---------------------------------------------------------------------------
def test_processing_hours_assigned_at_none_falls_back_to_created():
    o = _mk_order("2026-11-01T08:00:00", status="completed",
                  completed_at="2026-11-01T12:00:00", assigned_at=None)
    # assigned_at 为空 => 回退 created_at 08:00 -> 4.0h
    assert processing_hours(o) == 4.0


def test_processing_hours_completed_at_none_returns_zero():
    o = _mk_order("2026-11-01T08:00:00", status="assigned",
                  completed_at=None, assigned_at="2026-11-01T09:00:00")
    # 尚未完结 => 0.0（仍在计时）
    assert processing_hours(o) == 0.0


def test_processing_hours_normal():
    o = _mk_order("2026-11-01T08:00:00", status="completed",
                  completed_at="2026-11-01T11:30:00", assigned_at="2026-11-01T09:00:00")
    # 09:00 -> 11:30 = 2.5h
    assert processing_hours(o) == 2.5


# ---------------------------------------------------------------------------
# shift_hours：班次工时（跨天自动 +24h）
# ---------------------------------------------------------------------------
def test_shift_hours_normal():
    assert shift_hours("09:00", "17:00") == 8.0


def test_shift_hours_cross_day():
    assert shift_hours("17:00", "09:00") == 16.0


# ---------------------------------------------------------------------------
# generate_for_group：排班约束
# 说明：24h 在岗需「夜班 1 + 白班 2」。3 人时次日必然被迫违反「夜班次日休息」
#      （见下方 understaffed 测试，P1-4 已能计数）；4 人是满足约束的最小编制，
#      此处用 4 人验证正常约束成立。
# ---------------------------------------------------------------------------
def test_generate_for_group_constraints():
    eng = [{"id": i, "name": "E%d" % i} for i in range(1, 5)]  # 4 人（最小可满足编制）
    gid = 99
    rows, violations = generate_for_group(eng, gid, "2026-10")

    # 索引：row = (group_id, date, shift, slot, engineer_id, start, end)
    by_day_eng = {}
    night_next = {}  # date -> 夜班工程师 id
    for (g, d, shift, slot, eid, st, et) in rows:
        assert g == gid
        by_day_eng.setdefault((d, eid), []).append(shift)
        if shift == "night":
            night_next[d] = eid

    # 断言 1：每人每天最多一个班次
    for (d, eid), shifts in by_day_eng.items():
        assert len(shifts) == 1, "工程师 %s 在 %s 出现多个班次: %s" % (eid, d, shifts)

    # 断言 2：夜班次日不排班
    for d, eid in night_next.items():
        nxt = (datetime.fromisoformat(d) + timedelta(days=1)).isoformat()
        assert (nxt, eid) not in by_day_eng, \
            "夜班工程师 %s 次日 %s 仍被排班，违反休息约束" % (eid, nxt)

    # 正常编制下不应发生被迫违规
    assert violations == 0


def test_generate_for_group_understaffed_detects_violations():
    # 3 人编制无法满足「夜班次日休息」+ 24h 在岗，算法会被迫违规
    eng = [{"id": i, "name": "E%d" % i} for i in range(1, 4)]
    rows, violations = generate_for_group(eng, 98, "2026-10")

    # 即便人员不足，算法仍保证「每人每天最多一个班次」
    by_day_eng = {}
    for (g, d, shift, slot, eid, st, et) in rows:
        by_day_eng.setdefault((d, eid), []).append(shift)
    for (d, eid), shifts in by_day_eng.items():
        assert len(shifts) == 1

    # P1-4：人员不足导致的被迫违规应被计数（>0）
    assert isinstance(violations, int)
    assert violations > 0
