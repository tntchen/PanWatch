"""P3d 早盘/尾盘简报 Agent 测试（doc/12 §3 P3d、doc/14 §1 P3d）。

覆盖：
- 模板契约校验：早盘三选一；尾盘五选一 + 数量 + 价格区间；
  校验不过 should_notify=False（不推送）。
- 传参式 run_single：无方案档案跳过且不调 AI；窄化 watchlist 不改共享
  context（两股并行不串）。
- prompt 注入：方案摘要 + PositionInfo.trades_text 渲染。
- dedupe TTL 分档断言（新 Agent 不落默认值 60）。
- 注册/调度：WORKFLOW_AGENT_NAMES、AgentSeedSpec cron/single/enabled、
  AGENT_REGISTRY；daily_report seed 改档 0 18 * * 1-5。
- e2e run()：合法输出落库并推送；非法输出落库但不推送（内存库，AI/通知 mock）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import server
from src.agents.base import (
    AccountInfo,
    AgentContext,
    PortfolioInfo,
    PositionInfo,
)
from src.agents.morning_brief import (
    MORNING_ACTION_MAP,
    MorningBriefAgent,
    validate_brief_contract,
)
from src.agents.tail_brief import TAIL_ACTION_MAP, TailBriefAgent
from src.config import AppConfig, Settings, StockConfig
from src.core import agent_catalog, analysis_history, notify_dedupe
from src.models.market import MarketCode
from src.web import models as M
from src.web.database import Base

# --------------------------------------------------------------------------- #
# 固定 AI 输出样本
# --------------------------------------------------------------------------- #

VALID_MORNING_OUTPUT = (
    "东芯早盘 10:00\n"
    "📊 行情：120.46（+3.2%）量比1.85，强于板块\n"
    "📍 位置：距做T区125 -3.6%，距右侧批触发区110-115 +4.5%\n"
    "⚡ 提示：观望 — 未触及任何触发位，等待回踩\n"
    "💰 持仓：3150股@112.57，浮盈+7.0%\n"
    "以上由 AI 生成，仅供参考，不构成投资建议\n"
    '<!--PANWATCH_JSON-->\n{"action": "watch", "action_label": "观望", "reason": "未触及触发位"}\n<!--/PANWATCH_JSON-->'
)

INVALID_MORNING_OUTPUT = (
    "东芯早盘 10:00\n"
    "📊 行情：120.46（+3.2%）\n"
    "今日行情平稳，继续跟踪。\n"
)

VALID_TAIL_OUTPUT = (
    "东芯尾盘 14:45\n"
    "📊 行情：121.30（+0.7%）缩量整理\n"
    "🎯 决策：持有\n"
    "📋 依据：未破防线98；右侧批仍冻结\n"
    "📝 执行细节：若回踩110-115缩量企稳，执行右侧批约1000股，区间110-115\n"
    "💰 持仓：3150股@112.57，浮盈+7.7%\n"
    '<!--PANWATCH_JSON-->\n{"action": "hold", "action_label": "持有", "quantity": "1000股", "price_range": "110-115", "reason": "未触发"}\n<!--/PANWATCH_JSON-->'
)

INVALID_TAIL_NO_PRICE_RANGE = (
    "东芯尾盘 14:45\n"
    "🎯 决策：减仓\n"
    "📋 依据：放量滞涨；接近压力位\n"
    "📝 执行细节：减出500股\n"
)

INVALID_TAIL_NO_QUANTITY = (
    "东芯尾盘 14:45\n"
    "🎯 决策：做T减出\n"
    "📝 执行细节：区间125-135卖出\n"
)

PLAYBOOK_SUMMARY = "方案:东芯 v3.1\n价位:防线98\n批次:④右侧批冻结\n做T:125-135卖500-1000股,110-115接回"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def mem_db(monkeypatch):
    """内存 sqlite；接管简报链路上的所有 SessionLocal 引用。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import src.web.database as web_db

    monkeypatch.setattr(web_db, "SessionLocal", Session)  # agent 惰性 import 现取
    monkeypatch.setattr(analysis_history, "SessionLocal", Session)
    monkeypatch.setattr(notify_dedupe, "SessionLocal", Session)
    try:
        yield Session
    finally:
        engine.dispose()


def _make_position_info(**overrides) -> PositionInfo:
    kwargs = dict(
        account_id=1,
        account_name="主账户",
        stock_id=6,
        symbol="688110",
        name="东芯股份",
        market=MarketCode.CN,
        cost_price=112.572,
        quantity=3150,
        invested_amount=354601.8,
        trading_style="swing",
    )
    kwargs.update(overrides)
    return PositionInfo(**kwargs)


def _make_context(
    *,
    symbols: tuple[str, ...] = ("688110",),
    ai_return: str = VALID_MORNING_OUTPUT,
    trades_text: str = "",
) -> AgentContext:
    names = {"688110": "东芯股份", "600519": "贵州茅台"}
    watchlist = [
        StockConfig(symbol=s, name=names.get(s, s), market=MarketCode.CN)
        for s in symbols
    ]
    positions = [
        _make_position_info(trades_text=trades_text) if s == "688110"
        else _make_position_info(symbol=s, name=names.get(s, s), quantity=100)
        for s in symbols
    ]
    portfolio = PortfolioInfo(
        accounts=[
            AccountInfo(
                id=1,
                name="主账户",
                available_funds=150000.0,
                positions=positions,
            )
        ]
    )
    return AgentContext(
        ai_client=SimpleNamespace(chat=AsyncMock(return_value=ai_return)),
        notifier=server.NotifierManager(),
        config=AppConfig(settings=Settings(), watchlist=watchlist),
        portfolio=portfolio,
        model_label="",
    )


def _fake_collect_data(symbol: str = "688110") -> dict:
    stock = StockConfig(symbol=symbol, name="东芯股份", market=MarketCode.CN)
    pack = SimpleNamespace(
        quote=SimpleNamespace(current_price=120.46, change_pct=3.2, prev_close=116.8),
        technical={
            "volume_ratio": 1.85,
            "trend": "多头",
            "support_m": 110.0,
            "resistance_m": 135.0,
        },
    )
    return {
        "stock": stock,
        "pack": pack,
        "symbol_ctx": {
            "relative_strength": {"index_label": "科创50", "excess_5d": 3.0},
            "playbook": PLAYBOOK_SUMMARY,
        },
        "playbook_summary": PLAYBOOK_SUMMARY,
        "timestamp": "2026-07-22T10:00:00",
    }


def _patch_collect(monkeypatch, agent_cls, recorder: list | None = None):
    async def fake_collect(self, context):
        if recorder is not None:
            recorder.append([s.symbol for s in context.config.watchlist])
        return _fake_collect_data(context.config.watchlist[0].symbol)

    monkeypatch.setattr(agent_cls, "collect", fake_collect)


def _seed_stock_with_playbook(Session, symbol: str = "688110") -> None:
    db = Session()
    st = M.Stock(symbol=symbol, name="东芯股份", market="CN")
    db.add(st)
    db.flush()
    db.add(
        M.StockPlaybook(
            stock_id=st.id,
            version=1,
            is_active=True,
            payload={
                "schema_version": 1,
                "meta": {"name": "东芯股份抄底实施方案", "version_label": "v3.1"},
                "price_levels": [{"label": "防线", "value": 98}],
                "t_zone": {"sell_range": [125, 135], "buyback_range": [110, 115]},
            },
            summary="",
            note="",
        )
    )
    db.commit()
    db.close()


# --------------------------------------------------------------------------- #
# ① 模板契约校验
# --------------------------------------------------------------------------- #


def test_morning_contract_valid_three_way_keywords():
    """早盘：三选一关键词（可执行/观望/冻结）任一出现即通过。"""
    for keyword in ("可执行", "观望", "冻结"):
        ok, reason, hit = validate_brief_contract(
            f"⚡ 提示：{keyword} — 依据",
            None,
            action_map=MORNING_ACTION_MAP,
            require_execution_detail=False,
        )
        assert ok and reason == "ok" and hit == keyword


def test_morning_contract_rejects_missing_keyword():
    """早盘：正文无三选一结论 → missing_action_keyword。"""
    ok, reason, hit = validate_brief_contract(
        INVALID_MORNING_OUTPUT,
        None,
        action_map=MORNING_ACTION_MAP,
        require_execution_detail=False,
    )
    assert not ok and reason == "missing_action_keyword" and hit is None


def test_morning_contract_structured_json_fallback():
    """早盘：正文缺关键词但 PANWATCH_JSON action_label 合法 → 通过。"""
    ok, reason, hit = validate_brief_contract(
        "今日缩量整理。",
        {"action": "avoid", "action_label": "冻结"},
        action_map=MORNING_ACTION_MAP,
        require_execution_detail=False,
    )
    assert ok and hit == "冻结"


def test_tail_contract_requires_quantity_and_price_range():
    """尾盘：五选一之外还强制数量（X股）与价格区间（如110-115）。"""
    ok, reason, _ = validate_brief_contract(
        INVALID_TAIL_NO_PRICE_RANGE,
        None,
        action_map=TAIL_ACTION_MAP,
        require_execution_detail=True,
    )
    assert not ok and reason == "missing_price_range"

    ok, reason, _ = validate_brief_contract(
        INVALID_TAIL_NO_QUANTITY,
        None,
        action_map=TAIL_ACTION_MAP,
        require_execution_detail=True,
    )
    assert not ok and reason == "missing_quantity"


def test_tail_contract_valid_five_way_with_detail():
    """尾盘：五选一 + 数量 + 价格区间 → 通过；长标签优先不误命中。"""
    text = (
        "🎯 决策：执行右侧批\n"
        "📝 执行细节：买入1000股，区间110-115\n"
    )
    ok, reason, hit = validate_brief_contract(
        text,
        None,
        action_map=TAIL_ACTION_MAP,
        require_execution_detail=True,
    )
    assert ok and reason == "ok" and hit == "执行右侧批"


def test_action_maps_sync_with_allowed_actions():
    """词表 action 取值与 structured_output.ALLOWED_ACTIONS 保持同步。"""
    from src.core.signals.structured_output import ALLOWED_ACTIONS

    for mapping in (MORNING_ACTION_MAP, TAIL_ACTION_MAP):
        for info in mapping.values():
            assert info["action"] in ALLOWED_ACTIONS


# --------------------------------------------------------------------------- #
# ② run_single：档案门控 + 传参式并发安全
# --------------------------------------------------------------------------- #


def test_run_single_skips_stock_without_playbook(mem_db, monkeypatch, caplog):
    """无方案档案的股票：跳过（返回 None）、记日志、不调用 AI。"""
    import src.agents.morning_brief as mb

    # 库里有股票但无档案
    db = mem_db()
    db.add(M.Stock(symbol="688110", name="东芯股份", market="CN"))
    db.commit()
    db.close()
    _patch_collect(monkeypatch, MorningBriefAgent)

    context = _make_context()
    agent = MorningBriefAgent()
    with caplog.at_level("INFO"):
        result = asyncio.run(agent.run_single(context, "688110"))

    assert result is None
    assert not context.ai_client.chat.await_count
    assert any("无方案档案" in r.message for r in caplog.records)


def test_run_single_symbol_not_in_watchlist(mem_db):
    """symbol 不在 watchlist：返回 None，不抛异常。"""
    context = _make_context()
    result = asyncio.run(MorningBriefAgent().run_single(context, "000001"))
    assert result is None


def test_run_single_narrows_watchlist_concurrently(mem_db, monkeypatch):
    """传参式：两股并行 run_single 互不串 watchlist，共享 context 不被改。"""
    monkeypatch.setattr(
        "src.agents.morning_brief._load_playbook_summary",
        lambda symbol, market: PLAYBOOK_SUMMARY,
    )
    seen: list[list[str]] = []
    _patch_collect(monkeypatch, MorningBriefAgent, recorder=seen)

    context = _make_context(symbols=("688110", "600519"))
    agent = MorningBriefAgent()

    async def _run_both():
        return await asyncio.gather(
            agent.run_single(context, "688110"),
            agent.run_single(context, "600519"),
        )

    results = asyncio.run(_run_both())

    assert all(r is not None for r in results)
    # 每次执行只看到自己的目标股票
    assert sorted(tuple(s) for s in seen) == [("600519",), ("688110",)]
    # 共享 context 的 watchlist 未被改动，且未泄漏私有属性
    assert [s.symbol for s in context.config.watchlist] == ["688110", "600519"]
    assert not hasattr(context, "_playbook_summary")


# --------------------------------------------------------------------------- #
# ③ prompt 注入：方案摘要 + 近期流水
# --------------------------------------------------------------------------- #


def test_prompt_renders_playbook_and_trades_text():
    """prompt 含方案摘要、持仓浮盈速算与 trades_text 紧凑流水。"""
    agent = MorningBriefAgent()
    context = _make_context(trades_text="7/21 卖500@130(盈8704)")
    _, user_content = agent.build_prompt(_fake_collect_data(), context)

    assert PLAYBOOK_SUMMARY in user_content
    assert "3150股 成本112.57" in user_content
    assert "浮盈速算" in user_content
    assert "近期流水：7/21 卖500@130(盈8704)" in user_content
    assert "多头" in user_content and "量比：1.85" in user_content


def test_prompt_without_trades_and_position_backward_compatible():
    """无持仓无流水：prompt 不含流水行，行为不变。"""
    agent = TailBriefAgent()
    context = _make_context(trades_text="")
    context.portfolio.accounts[0].positions = []
    _, user_content = agent.build_prompt(_fake_collect_data(), context)

    assert "近期流水" not in user_content
    assert "当前无持仓" in user_content
    assert PLAYBOOK_SUMMARY in user_content


def test_prompt_files_exist_and_contain_contract_keywords():
    """两个 prompt 模板存在且含各自结论词表与模板锚点。"""
    morning_prompt = MorningBriefAgent.prompt_path.read_text(encoding="utf-8")
    tail_prompt = TailBriefAgent.prompt_path.read_text(encoding="utf-8")
    assert MorningBriefAgent.prompt_path.name == "morning_brief.txt"
    assert TailBriefAgent.prompt_path.name == "tail_brief.txt"
    for kw in ("可执行", "观望", "冻结", "180"):
        assert kw in morning_prompt
    for kw in ("持有", "执行右侧批", "做T减出", "减仓", "观望", "数量", "价格区间"):
        assert kw in tail_prompt
    assert "PANWATCH_JSON" in morning_prompt and "PANWATCH_JSON" in tail_prompt


# --------------------------------------------------------------------------- #
# ④ dedupe TTL 分档断言
# --------------------------------------------------------------------------- #


def test_dedupe_ttl_explicit_tier_not_default():
    """新 Agent 命中 12h 分档（不落默认值 60）。"""
    context = _make_context()
    assert MorningBriefAgent()._notify_dedupe_ttl_minutes(context) == 12 * 60
    assert TailBriefAgent()._notify_dedupe_ttl_minutes(context) == 12 * 60


# --------------------------------------------------------------------------- #
# ⑤ e2e：合法输出推送落库；非法输出不推送落日志
# --------------------------------------------------------------------------- #


def test_e2e_valid_output_notified_and_saved(mem_db, monkeypatch):
    """合法早盘简报：契约通过 → 推送成功 + analysis_history 落库。"""
    _patch_collect(monkeypatch, MorningBriefAgent)
    context = _make_context(ai_return=VALID_MORNING_OUTPUT)
    agent = MorningBriefAgent()

    result = asyncio.run(agent.run(context))

    assert result.raw_data["contract_valid"] is True
    assert result.raw_data["action_label"] == "观望"
    assert result.raw_data["notified"] is True
    assert "PANWATCH_JSON" not in result.content  # 结构化块已剥离

    db = mem_db()
    row = (
        db.query(M.AnalysisHistory)
        .filter_by(agent_name="morning_brief", stock_symbol="688110")
        .first()
    )
    assert row is not None and "观望" in row.content
    db.close()


def test_e2e_invalid_output_not_notified_but_logged(mem_db, monkeypatch, caplog):
    """非法尾盘简报（缺价格区间）：不推送、落错误日志、历史仍落库备查。"""
    _patch_collect(monkeypatch, TailBriefAgent)
    context = _make_context(ai_return=INVALID_TAIL_NO_PRICE_RANGE)
    agent = TailBriefAgent()

    with caplog.at_level("ERROR"):
        result = asyncio.run(agent.run(context))

    assert result.raw_data["contract_valid"] is False
    assert result.raw_data["contract_reason"] == "missing_price_range"
    assert result.raw_data["notified"] is False
    assert any("契约校验失败" in r.message for r in caplog.records)

    db = mem_db()
    row = (
        db.query(M.AnalysisHistory)
        .filter_by(agent_name="tail_brief", stock_symbol="688110")
        .first()
    )
    assert row is not None
    db.close()


def test_e2e_tail_valid_output_five_way(mem_db, monkeypatch):
    """合法尾盘简报：五选一 + 数量 + 价格区间 → 推送。"""
    _patch_collect(monkeypatch, TailBriefAgent)
    context = _make_context(ai_return=VALID_TAIL_OUTPUT)
    result = asyncio.run(TailBriefAgent().run(context))

    assert result.raw_data["contract_valid"] is True
    assert result.raw_data["action_label"] == "持有"
    assert result.raw_data["notified"] is True


# --------------------------------------------------------------------------- #
# ⑥ 注册与调度
# --------------------------------------------------------------------------- #


def test_seed_specs_and_workflow_names():
    """两个简报 Agent 进 WORKFLOW_AGENT_NAMES + SeedSpec（cron/single/disabled）。"""
    assert "morning_brief" in agent_catalog.WORKFLOW_AGENT_NAMES
    assert "tail_brief" in agent_catalog.WORKFLOW_AGENT_NAMES

    specs = {s.name: s for s in agent_catalog.AGENT_SEED_SPECS}
    morning = specs["morning_brief"]
    tail = specs["tail_brief"]
    assert morning.schedule == "0 10 * * 1-5"
    assert tail.schedule == "45 14 * * 1-5"
    for spec in (morning, tail):
        assert spec.execution_mode == "single"
        assert spec.enabled is False
        assert spec.kind == agent_catalog.AGENT_KIND_WORKFLOW
        assert spec.visible is True


def test_daily_report_seed_rescheduled_to_1800():
    """daily_report seed 从 15:30 改档 18:00（seed 不覆盖已有库配置）。"""
    spec = next(s for s in agent_catalog.AGENT_SEED_SPECS if s.name == "daily_report")
    assert spec.schedule == "0 18 * * 1-5"


def test_agent_registry_contains_brief_agents():
    """AGENT_REGISTRY 注册两个新 Agent。"""
    assert server.AGENT_REGISTRY["morning_brief"] is MorningBriefAgent
    assert server.AGENT_REGISTRY["tail_brief"] is TailBriefAgent
