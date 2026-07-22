"""P3c 盘中监测套用方案逻辑测试（doc/12 §3 P3c、doc/14 §1 P3c）。

覆盖：
- extract_playbook_levels：做T区/接回区/防线/批次触发区价位解析（动态读取、
  严禁硬编码），缺字段/歧义文案容错跳过；
- check_and_update 方案价位穿越事件：上穿/下穿、首次观测不触发、
  同向冷却去重、无 playbook 股票现有逻辑零改动；
- build_prompt 方案摘要注入：有档案出现「方案档案」章节与规则段；
  无档案股票 system/user prompt 与改造前基线逐字一致（快照断言）。
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.agents.base import AccountInfo, PortfolioInfo
from src.agents.intraday_monitor import (
    PROMPT_PATH,
    IntradayMonitorAgent,
)
from src.core.intraday_event_gate import (
    PLAYBOOK_CROSS_COOLDOWN_SEC,
    PlaybookLevel,
    check_and_update,
    extract_playbook_levels,
)
from src.models.market import MarketCode, StockData
from src.web import models as M
from src.web.database import Base

# --------------------------------------------------------------------------- #
# 改造前基线（捕获脚本：同桩运行 build_prompt，时间行已归一化）
# --------------------------------------------------------------------------- #

_BASELINE_USER_CONTENT = """## 时间：<TS>

## 股票行情
- 股票：东芯股份（688110）
- 现价：112.50
- 涨跌幅：+1.23%
- 涨跌额：+1.37
- 今开：111.00
- 最高：113.20
- 最低：110.50
- 昨收：111.13
- 成交量：123456 手
- 成交额：9877 万

## 系统阈值
- 价格异动：|涨跌幅| ≥ 3.0%（ATR 不可用，回退固定阈值）
- 量能异动：量比 ≥ 2.0
- 止损预警：浮亏 ≤ -5.0%
- 止盈提醒：浮盈 ≥ 10.0%
- 当前涨跌幅：+1.23%（未触发）

## 账户资金
- 总可用资金：5000 元
  - 主账户：5000 元

## 未持仓（仅关注）
- 可用资金充足，可考虑建仓

请结合技术分析、资金情况和历史分析，给出明确的操作建议。"""


def _dongxin_like_payload() -> dict:
    """东芯 v3.1 风格 payload（数值仅作测试样例，断言传参不硬编码到实现）。"""
    return {
        "schema_version": 1,
        "meta": {"name": "测试方案", "version_label": "v1"},
        "price_levels": [{"label": "防线", "value": 98}],
        "batches": [
            {"name": "①", "trigger": "120±3", "status": "executed"},
            {"name": "④右侧批", "trigger": "三信号同时满足", "status": "frozen"},
        ],
        "t_zone": {
            "sell_range": [125, 135],
            "buyback_range": [110, 115],
            "size": "500-1000股",
            "mode": "先卖后买",
        },
        "defense": {"rule": "连续2日收盘<98", "action": "减仓1/3~1/2"},
    }


# --------------------------------------------------------------------------- #
# ① extract_playbook_levels 解析
# --------------------------------------------------------------------------- #


def test_extract_levels_full_payload():
    """完整 payload：做T区/接回区/防线全部解析；已执行批次与信号型触发跳过。"""
    levels = extract_playbook_levels(_dongxin_like_payload())
    by_name = {lv.name: lv.price for lv in levels}

    assert by_name["做T卖出区下沿"] == 125.0
    assert by_name["做T卖出区上沿"] == 135.0
    assert by_name["做T接回区下沿"] == 110.0
    assert by_name["做T接回区上沿"] == 115.0
    assert by_name["防线"] == 98.0
    # 已执行批次①（120±3）跳过；信号型触发（三信号同时满足）无数值跳过
    assert not any("批次" in lv.name for lv in levels)


def test_extract_levels_tolerant_garbage():
    """容错：None/空 dict/类型错误一律返回空表，不抛异常。"""
    assert extract_playbook_levels(None) == []
    assert extract_playbook_levels({}) == []
    assert extract_playbook_levels("garbage") == []
    assert extract_playbook_levels({"t_zone": "not-a-dict"}) == []
    assert extract_playbook_levels({"t_zone": {"sell_range": "125-135"}}) == []
    assert extract_playbook_levels({"batches": [None, 1, "x"]}) == []


def test_extract_levels_defense_rule_variants():
    """防线规则解析：比较符后数字优先；多数字无比较符视为歧义跳过。"""
    lv = extract_playbook_levels({"defense": {"rule": "连续2日收盘<98"}})
    assert [(x.name, x.price) for x in lv] == [("防线", 98.0)]

    lv = extract_playbook_levels({"defense": {"rule": "跌破100元止损"}})
    assert [(x.name, x.price) for x in lv] == [("防线", 100.0)]

    # 两个数字且无比较符 → 歧义跳过
    assert extract_playbook_levels({"defense": {"rule": "98和100之间"}}) == []
    # 无数字 / 缺 rule → 跳过
    assert extract_playbook_levels({"defense": {"rule": "趋势走坏"}}) == []
    assert extract_playbook_levels({"defense": {}}) == []


def test_extract_levels_batch_triggers():
    """未执行批次触发区：'120±3'取中心价；'110-115'取上下沿；单值直取。"""
    payload = {
        "batches": [
            {"name": "A", "trigger": "120±3", "status": "pending"},
            {"name": "B", "trigger": "110-115", "status": "frozen"},
            {"name": "C", "trigger": "站稳100", "status": "pending"},
            {"name": "D", "trigger": "120±3", "status": "executed"},
        ]
    }
    levels = extract_playbook_levels(payload)
    pairs = [(lv.name, lv.price) for lv in levels]
    assert ("批次A触发区", 120.0) in pairs
    assert ("批次B触发区下沿", 110.0) in pairs
    assert ("批次B触发区上沿", 115.0) in pairs
    assert ("批次C触发区", 100.0) in pairs
    assert not any(lv.name.startswith("批次D") for lv in levels)


def test_extract_levels_dedupe_same_price():
    """同价位多来源去重，保留先出现（更高优先级）的名称。"""
    payload = {
        "t_zone": {"sell_range": [125, 135], "buyback_range": [135, 140]},
        "defense": {"rule": "收盘<135"},
    }
    levels = extract_playbook_levels(payload)
    prices = [lv.price for lv in levels]
    assert prices.count(135.0) == 1
    name_135 = next(lv.name for lv in levels if lv.price == 135.0)
    assert name_135 == "做T卖出区上沿"  # 做T区优先于接回区/防线


# --------------------------------------------------------------------------- #
# ② check_and_update 方案价位穿越事件
# --------------------------------------------------------------------------- #


@pytest.fixture
def gate_state_dir(tmp_path, monkeypatch):
    """事件门状态重定向到临时目录（不碰真实 DATA_DIR）。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


def _gate(symbol: str, **kwargs) -> object:
    defaults = dict(
        change_pct=0.5,
        volume_ratio=1.0,
        kline_summary=None,
        price_threshold=3.0,
        volume_threshold=2.0,
    )
    defaults.update(kwargs)
    return check_and_update(symbol=symbol, **defaults)


def test_cross_up_through_t_zone(gate_state_dir):
    """价格上穿做T卖出区下沿 → 生成 playbook_cross 事件。"""
    levels = [PlaybookLevel("做T卖出区下沿", 125.0)]
    d1 = _gate("T1", current_price=120.0, playbook_levels=levels)
    assert d1.reasons == []  # 首次观测仅建价态基线

    d2 = _gate("T1", current_price=126.0, playbook_levels=levels)
    assert "playbook_cross:上穿做T卖出区下沿@125" in d2.reasons
    assert d2.should_analyze


def test_cross_down_through_defense(gate_state_dir):
    """价格下穿防线 → 生成下穿事件；恰好触及（等于价位）也算穿越。"""
    levels = [PlaybookLevel("防线", 98.0)]
    _gate("T2", current_price=100.0, playbook_levels=levels)
    d = _gate("T2", current_price=98.0, playbook_levels=levels)
    assert "playbook_cross:下穿防线@98" in d.reasons


def test_no_cross_no_event(gate_state_dir):
    """价格在关键位同侧波动 → 无 playbook 事件，should_analyze False。"""
    levels = [PlaybookLevel("做T卖出区下沿", 125.0)]
    _gate("T3", current_price=120.0, playbook_levels=levels)
    d = _gate("T3", current_price=121.0, playbook_levels=levels)
    assert d.reasons == []
    assert not d.should_analyze


def test_first_observation_no_fire(gate_state_dir):
    """首次观测即使已在关键位另一侧也不触发（无穿越基线）。"""
    levels = [PlaybookLevel("防线", 98.0)]
    d = _gate("T4", current_price=95.0, playbook_levels=levels)
    assert d.reasons == []


def test_cross_cooldown_dedupe(gate_state_dir):
    """同一价位同一方向在冷却期内重复穿越只报一次；反向穿越不受限。"""
    levels = [PlaybookLevel("做T卖出区下沿", 125.0)]
    _gate("T5", current_price=120.0, playbook_levels=levels)

    d1 = _gate("T5", current_price=126.0, playbook_levels=levels)
    assert "playbook_cross:上穿做T卖出区下沿@125" in d1.reasons

    d2 = _gate("T5", current_price=124.0, playbook_levels=levels)
    assert "playbook_cross:下穿做T卖出区下沿@125" in d2.reasons  # 反向不受冷却限制

    d3 = _gate("T5", current_price=126.0, playbook_levels=levels)
    assert d3.reasons == []  # 冷却期内同向重复穿越被去重


def test_cooldown_key_format_stable():
    """冷却常量存在且为正（防误删；冷却语义见上一用例）。"""
    assert PLAYBOOK_CROSS_COOLDOWN_SEC > 0


def test_no_playbook_levels_legacy_behavior_unchanged(gate_state_dir):
    """无 playbook（空价位表）：带 current_price 与不带时事件判定完全一致。"""
    common = dict(change_pct=3.5, volume_ratio=2.5, kline_summary=None)
    d_legacy = _gate("T6", **common)  # 旧签名：无 current_price
    d_with = _gate("T7", current_price=120.0, playbook_levels=[], **common)
    assert d_legacy.reasons == d_with.reasons
    assert set(d_legacy.reasons) == {"price_threshold", "volume_threshold"}
    assert not any(r.startswith("playbook_cross") for r in d_with.reasons)


def test_auto_load_levels_from_db(gate_state_dir, monkeypatch):
    """playbook_levels=None 时按 symbol 自动从库读激活档案并提取价位。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr("src.web.database.SessionLocal", Session)
    try:
        db = Session()
        st = M.Stock(symbol="T8", name="测试股", market="CN")
        db.add(st)
        db.flush()
        db.add(
            M.StockPlaybook(
                stock_id=st.id,
                version=1,
                is_active=True,
                payload=_dongxin_like_payload(),
                summary="",
                note="",
            )
        )
        db.commit()
        db.close()

        d1 = _gate("T8", current_price=120.0)  # 建基线
        assert d1.reasons == []
        d2 = _gate("T8", current_price=126.0)  # 自动加载价位，穿越 125
        assert "playbook_cross:上穿做T卖出区下沿@125" in d2.reasons
    finally:
        engine.dispose()


def test_auto_load_no_playbook_failsoft(gate_state_dir, monkeypatch):
    """库中无档案：自动加载 fail-soft 为空，现有事件逻辑不变。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr("src.web.database.SessionLocal", Session)
    try:
        d1 = _gate("T9", current_price=100.0, change_pct=3.5)
        d2 = _gate("T9", current_price=50.0, change_pct=0.1)
        assert d1.reasons == ["price_threshold"]
        assert d2.reasons == []  # 大幅跌穿无数值也无 playbook 事件（无档案）
    finally:
        engine.dispose()


def test_auto_load_db_error_failsoft(gate_state_dir, monkeypatch):
    """档案加载抛异常：事件门不崩，按无方案处理。"""

    def boom(symbol: str):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "src.core.intraday_event_gate._load_playbook_levels", boom
    )
    d = _gate("T10", current_price=100.0, change_pct=3.5)
    assert d.reasons == ["price_threshold"]


# --------------------------------------------------------------------------- #
# ③ build_prompt 方案摘要注入
# --------------------------------------------------------------------------- #


def _strip_playbook_block(text: str) -> str:
    """测试侧独立实现：剔除模板中标记的方案规则段，还原改造前模板。"""
    start = text.find("<!-- PLAYBOOK_SECTION_START -->")
    if start == -1:
        return text
    end = text.find("<!-- PLAYBOOK_SECTION_END -->", start)
    end = text.index("-->", end) + 3
    return text[:start].rstrip("\n") + "\n" + text[end:].lstrip("\n")


def _make_prompt_context() -> SimpleNamespace:
    return SimpleNamespace(
        portfolio=PortfolioInfo(
            accounts=[AccountInfo(id=1, name="主账户", available_funds=5000.0)]
        )
    )


def _make_prompt_data(symbol_context: dict | None = None) -> dict:
    stock = StockData(
        symbol="688110",
        name="东芯股份",
        market=MarketCode.CN,
        current_price=112.5,
        change_pct=1.23,
        change_amount=1.37,
        volume=123456.0,
        turnover=98765432.0,
        open_price=111.0,
        high_price=113.2,
        low_price=110.5,
        prev_close=111.13,
    )
    return {
        "stock_data": stock,
        "stocks": [stock],
        "kline_summary": None,
        "signal_pack": None,
        "symbol_context": symbol_context or {},
        "quality_overview": {},
        "daily_analysis": None,
        "premarket_analysis": None,
    }


def _normalize(user_content: str) -> str:
    return re.sub(r"^## 时间：.*$", "## 时间：<TS>", user_content, flags=re.M)


def test_prompt_no_playbook_identical_to_baseline():
    """快照断言：无 playbook 股票的 system/user prompt 与改造前逐字一致。"""
    agent = IntradayMonitorAgent()
    system_prompt, user_content = agent.build_prompt(
        _make_prompt_data(), _make_prompt_context()
    )

    raw_template = PROMPT_PATH.read_text(encoding="utf-8")
    assert system_prompt == _strip_playbook_block(raw_template)
    assert "方案档案" not in system_prompt
    assert _normalize(user_content) == _BASELINE_USER_CONTENT


def test_prompt_playbook_summary_injected():
    """有 playbook 股票：user prompt 注入方案摘要章节，system 保留规则段。"""
    summary = "方案:东芯股份抄底实施方案 v3.1｜策略:激进·满仓单票\n做T:125-135卖,110-115接回\n防线:连续2日收盘<98→减仓1/3~1/2"
    agent = IntradayMonitorAgent()
    system_prompt, user_content = agent.build_prompt(
        _make_prompt_data({"playbook": summary}), _make_prompt_context()
    )

    # system：规则段保留且不含注释标记
    assert "方案档案使用规则" in system_prompt
    assert "<!--" not in system_prompt

    # user：摘要章节出现在系统阈值之后、其余章节不受影响
    assert "## 方案档案（操作纪律）" in user_content
    assert "做T:125-135卖,110-115接回" in user_content
    assert "防线:连续2日收盘<98→减仓1/3~1/2" in user_content
    assert "## 股票行情" in user_content
    assert "## 系统阈值" in user_content
    # 方案章节位于系统阈值之后、账户资金之前
    i_threshold = user_content.index("## 系统阈值")
    i_playbook = user_content.index("## 方案档案（操作纪律）")
    i_funds = user_content.index("## 账户资金")
    assert i_threshold < i_playbook < i_funds


def test_prompt_playbook_none_vs_missing_key():
    """symbol_context 缺 playbook 键或为 None：均按无档案处理（与基线一致）。"""
    agent = IntradayMonitorAgent()
    _, u1 = agent.build_prompt(
        _make_prompt_data({"playbook": None}), _make_prompt_context()
    )
    assert _normalize(u1) == _BASELINE_USER_CONTENT
