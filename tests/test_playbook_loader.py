"""方案档案 loader 与摘要生成测试（src/core/playbook.py，doc/14 §1 P2a）。

覆盖：
- 无档案返回 None；
- 多版本取 is_active（而非最新版本号）；
- 缺 schema_version 信封容错不抛异常；
- 摘要 ≤500 token（以字符数为保守上界）且含价位表/批次状态/防线/做T区/临近日历项；
- get_trigger_hint 按规则名匹配 / 无匹配返回 None / 档案不存在容错。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.playbook import (
    SUMMARY_CHAR_BUDGET,
    get_trigger_hint,
    load_active_playbook,
    summarize_playbook,
)
from src.web import models as M
from src.web.database import Base


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _make_stock(db) -> M.Stock:
    st = M.Stock(symbol="688110", name="东芯股份", market="CN")
    db.add(st)
    db.commit()
    return st


def _full_payload() -> dict:
    day10 = (date.today() + timedelta(days=10)).isoformat()
    day40 = (date.today() + timedelta(days=40)).isoformat()
    return {
        "schema_version": 1,
        "meta": {
            "name": "东芯股份抄底实施方案",
            "version_label": "v3.1",
            "strategy_mode": "激进·满仓单票",
            "base_date": "2026-07-20",
            "base_price": 100.38,
        },
        "price_levels": [
            {"label": "高点", "value": 209.55},
            {"label": "防线", "value": 98},
        ],
        "batches": [
            {"name": "①", "trigger": "120±3", "logic": "6月平台下沿", "status": "executed"},
            {"name": "④右侧批", "trigger": "三信号", "logic": "企稳确认", "status": "frozen"},
        ],
        "t_zone": {
            "sell_range": [125, 135],
            "buyback_range": [110, 115],
            "size": "500-1000股",
            "mode": "先卖后买",
        },
        "defense": {"rule": "连续2日收盘<98", "action": "减仓1/3~1/2"},
        "stop_loss_tracks": [
            {"track": "行业轨", "trigger": "Q4合约价转跌", "action": "减仓至观察仓"},
        ],
        "calendar": [
            {"date": day10, "event": "长鑫科技挂牌", "bias": "中性偏多", "plan": "观察"},
            {"date": day40, "event": "远期事件不应出现", "bias": "", "plan": ""},
            {"date": "不是日期", "event": "坏数据容错", "bias": "", "plan": ""},
        ],
        "scenarios": [
            {"name": "上行", "trigger": "毛利率≥45%", "action": "右侧批执行"},
        ],
        "trigger_hints": {"防线98": "连续2日收盘<98→减仓1/3~1/2"},
        "raw_markdown": "# 原始方案全文",
    }


_DEFAULT = object()


def _add_playbook(db, stock_id, version, is_active, payload=_DEFAULT) -> M.StockPlaybook:
    row = M.StockPlaybook(
        stock_id=stock_id,
        version=version,
        is_active=is_active,
        payload=_full_payload() if payload is _DEFAULT else payload,
        summary="",
        note="",
    )
    db.add(row)
    db.commit()
    return row


# ------------------------------------------------------------- load_active


def test_load_active_returns_none_when_no_playbook(db):
    """无档案股票返回 None，不抛异常。"""
    st = _make_stock(db)
    assert load_active_playbook(db, st.id) is None


def test_load_active_picks_is_active_not_latest_version(db):
    """多版本时取 is_active=True 的版本（即使版本号不是最大）。"""
    st = _make_stock(db)
    _add_playbook(db, st.id, version=1, is_active=False)
    _add_playbook(db, st.id, version=2, is_active=False)
    active = _add_playbook(db, st.id, version=3, is_active=True)
    _add_playbook(db, st.id, version=4, is_active=False)

    row = load_active_playbook(db, st.id)
    assert row is not None
    assert row.id == active.id
    assert row.version == 3


def test_load_active_other_stock_isolated(db):
    """其他股票的档案不影响查询结果。"""
    st1 = _make_stock(db)
    st2 = M.Stock(symbol="600519", name="贵州茅台", market="CN")
    db.add(st2)
    db.commit()
    _add_playbook(db, st2.id, version=1, is_active=True)
    assert load_active_playbook(db, st1.id) is None


# ----------------------------------------------------------- trigger hint


def test_get_trigger_hint_exact_match(db):
    """规则名精确命中 trigger_hints 返回方案提示文案。"""
    st = _make_stock(db)
    pb = _add_playbook(db, st.id, version=1, is_active=True)
    hint = get_trigger_hint(db, pb.id, "防线98")
    assert hint == "连续2日收盘<98→减仓1/3~1/2"


def test_get_trigger_hint_whitespace_tolerant(db):
    """规则名两侧空白可容错匹配。"""
    st = _make_stock(db)
    pb = _add_playbook(db, st.id, version=1, is_active=True)
    assert get_trigger_hint(db, pb.id, "  防线98 ") == "连续2日收盘<98→减仓1/3~1/2"


def test_get_trigger_hint_no_match_returns_none(db):
    """无匹配规则名返回 None。"""
    st = _make_stock(db)
    pb = _add_playbook(db, st.id, version=1, is_active=True)
    assert get_trigger_hint(db, pb.id, "不存在的规则") is None


def test_get_trigger_hint_missing_playbook_or_hints(db):
    """档案不存在 / payload 无 trigger_hints 时返回 None，不抛异常。"""
    assert get_trigger_hint(db, 99999, "防线98") is None

    st = _make_stock(db)
    pb = _add_playbook(db, st.id, version=1, is_active=True, payload={"meta": {}})
    assert get_trigger_hint(db, pb.id, "防线98") is None

    pb2 = _add_playbook(db, st.id, version=2, is_active=True, payload=None)
    assert get_trigger_hint(db, pb2.id, "防线98") is None


# -------------------------------------------------------------- summarize


def test_summarize_contains_key_sections(db):
    """摘要含策略模式/价位/批次状态/做T区/防线/临近30天日历项。"""
    summary = summarize_playbook(_full_payload())

    assert "激进·满仓单票" in summary  # 策略模式
    assert "v3.1" in summary
    assert "高点209.55" in summary  # 价位表
    assert "防线98" in summary
    assert "已执行" in summary and "冻结" in summary  # 批次状态
    assert "125-135" in summary and "110-115" in summary  # 做T区
    assert "先卖后买" in summary
    assert "连续2日收盘<98" in summary  # 防线
    assert "减仓1/3~1/2" in summary
    assert "长鑫科技挂牌" in summary  # 临近30天日历项
    assert "远期事件不应出现" not in summary  # 30天外排除
    assert "坏数据容错" not in summary  # 非法日期容错


def test_summarize_within_token_budget(db):
    """摘要 ≤500 token（CJK 1字≈1token，len 为保守上界）。"""
    summary = summarize_playbook(_full_payload())
    assert len(summary) <= 500
    assert len(summary) <= SUMMARY_CHAR_BUDGET


def test_summarize_truncates_low_priority_sections(db):
    """超预算时舍弃尾部章节（止损轨/情景），必需项保留。"""
    payload = _full_payload()
    payload["stop_loss_tracks"] = [
        {"track": f"轨道{i}", "trigger": "很长" * 40, "action": "动作" * 40}
        for i in range(5)
    ]
    payload["scenarios"] = [
        {"name": f"情景{i}", "trigger": "触发" * 40, "action": "动作" * 40}
        for i in range(5)
    ]
    summary = summarize_playbook(payload)
    assert len(summary) <= 500
    assert "激进·满仓单票" in summary
    assert "防线98" in summary
    assert "长鑫科技挂牌" in summary


def test_summarize_missing_schema_version_tolerant(db):
    """缺 schema_version 信封不抛异常，仍能提取字段。"""
    payload = _full_payload()
    del payload["schema_version"]
    summary = summarize_playbook(payload)
    assert "激进·满仓单票" in summary


def test_summarize_bad_inputs_never_raise(db):
    """非 dict / 空 dict / 字段类型错误的 payload 均不抛异常，返回 str。"""
    assert summarize_playbook(None) == ""
    assert summarize_playbook("not a dict") == ""
    assert summarize_playbook({}) == ""
    assert summarize_playbook({"schema_version": 1}) == ""
    weird = {
        "meta": "oops",
        "price_levels": "oops",
        "batches": [1, "x", {"name": "①", "trigger": None}],
        "t_zone": [1, 2],
        "defense": None,
        "calendar": [{"date": 123, "event": None}],
        "scenarios": {"name": "上行"},
    }
    result = summarize_playbook(weird)
    assert isinstance(result, str)
