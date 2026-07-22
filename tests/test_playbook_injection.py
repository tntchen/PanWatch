"""P3a 档案 + 流水注入层测试（doc/12 §3 P3a v1.5、doc/14 §1 P3a）。

覆盖：
- build_symbol_contexts 新增 "playbook" 键：无档案为 None，且**无档案股票的
  payload 其余字段与改造前基线逐字一致**（快照断言，防行为漂移）；
- 有档案时 payload["playbook"] 为 summarize_playbook 摘要；
- 档案加载异常时容错为 None，不影响主流程；
- load_portfolio_for_agent 的 PositionInfo 附带近期流水（当日全部 + 近 10 笔，
  字段 日期/方向/价/量/费/realized_pnl/note）与紧凑文本 trades_text（≤200 token/股）；
- 无流水持仓 trades=[]、trades_text=""（向后兼容）。

基线捕获方式：改造前用同桩运行 build_symbol_contexts 得到的 payload
（688110 东芯股份 + 单账户持仓，外部取数全部打桩，确定性）。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import server
from src.agents.base import AccountInfo, PortfolioInfo, PositionInfo
from src.core import context_builder
from src.core.context_builder import ContextBuilder
from src.models.market import MarketCode
from src.web import models as M
from src.web.database import Base

# --------------------------------------------------------------------------- #
# 改造前基线（先存基线再改；捕获脚本已删除，此处内嵌为常量）
# --------------------------------------------------------------------------- #

_BASELINE_PAYLOAD: dict = {
    "symbol": "688110",
    "name": "东芯股份",
    "market": "CN",
    "technical_current": {"trend": "多头"},
    "kline_history": {
        "available": True,
        "ret_5d": 5.0,
        "ret_20d": 12.0,
        "trend": "多头",
    },
    "relative_strength": {
        "index_label": "沪深300",
        "stock_5d": 5.0,
        "index_5d": 2.0,
        "excess_5d": 3.0,
        "stock_20d": 12.0,
        "index_20d": 4.0,
        "excess_20d": 8.0,
    },
    "ta_verdict": None,
    "news": {
        "realtime": [],
        "extended": [],
        "history": [],
        "history_topic": {
            "summary": "近期无显著新闻主题",
            "topics": [],
            "sentiment": "neutral",
            "counts": {"positive": 0, "negative": 0, "neutral": 0},
        },
    },
    "events": [],
    "constraints": {
        "has_position": True,
        "position": {
            "symbol": "688110",
            "name": "东芯股份",
            "market": "CN",
            "total_quantity": 3150,
            "avg_cost": 112.572,
            "total_cost": 354601.8,
            "trading_style": "swing",
            "positions": [
                {
                    "account_id": 1,
                    "account_name": "主账户",
                    "quantity": 3150,
                    "cost_price": 112.572,
                    "trading_style": "swing",
                }
            ],
        },
        "total_available_funds": 5000.0,
        "total_cost": 354601.8,
        "account_count": 1,
        "single_position_ratio": 1.0,
        "risk_budget_hint": "strict",
    },
    "memory": {},
    "data_quality": {
        "score": 60,
        "coverage": {
            "quote": True,
            "technical": True,
            "events": False,
            "news_realtime": False,
            "news_extended": False,
            "history_news": False,
            "kline_history": True,
        },
        "realtime_news_count": 0,
        "extended_news_count": 0,
        "history_news_count": 0,
    },
}


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def mem_db(monkeypatch):
    """内存 sqlite；同时接管 context_builder 与 server 的 SessionLocal。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(context_builder, "SessionLocal", Session)
    monkeypatch.setattr(server, "SessionLocal", Session)
    try:
        yield Session
    finally:
        engine.dispose()


def _make_position_info(**overrides) -> PositionInfo:
    kwargs = dict(
        account_id=1,
        account_name="主账户",
        stock_id=1,
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


def _make_context(**pos_overrides):
    stock = SimpleNamespace(symbol="688110", market=MarketCode.CN, name="东芯股份")
    portfolio = PortfolioInfo(
        accounts=[
            AccountInfo(
                id=1,
                name="主账户",
                available_funds=5000.0,
                positions=[_make_position_info(**pos_overrides)],
            )
        ]
    )
    return SimpleNamespace(watchlist=[stock], portfolio=portfolio)


def _make_pack():
    return SimpleNamespace(
        quote=object(),
        technical={"trend": "多头"},
        news=SimpleNamespace(items=[]),
        events=SimpleNamespace(days=7, items=[]),
    )


def _stub_externals(monkeypatch):
    """与基线捕获时完全一致的打桩集合。"""
    monkeypatch.setattr(
        context_builder,
        "build_kline_history_context",
        lambda *, symbol, market, lookback_days=120: {
            "available": True,
            "ret_5d": 5.0,
            "ret_20d": 12.0,
            "trend": "多头",
        },
    )
    monkeypatch.setattr(
        ContextBuilder,
        "_fetch_index_context",
        lambda self, symbol, market: {
            "available": True,
            "ret_5d": 2.0,
            "ret_20d": 4.0,
        },
    )
    monkeypatch.setattr(
        context_builder,
        "get_latest_ta_verdict",
        lambda symbol, within_days=14: None,
    )
    monkeypatch.setattr(
        ContextBuilder, "_load_history_news", staticmethod(lambda *a, **k: [])
    )
    monkeypatch.setattr(
        ContextBuilder, "_build_snapshot_memory", staticmethod(lambda *a, **k: {})
    )


def _build(context) -> dict:
    return asyncio.run(
        ContextBuilder().build_symbol_contexts(
            agent_name="daily_report",
            context=context,
            packs={"688110": _make_pack()},
            persist_snapshot=False,
        )
    )["symbols"]["688110"]


def _playbook_payload() -> dict:
    day10 = (date.today() + timedelta(days=10)).isoformat()
    return {
        "schema_version": 1,
        "meta": {
            "name": "东芯股份抄底实施方案",
            "version_label": "v3.1",
            "strategy_mode": "激进·满仓单票",
        },
        "price_levels": [{"label": "防线", "value": 98}],
        "t_zone": {
            "sell_range": [125, 135],
            "buyback_range": [110, 115],
            "size": "500-1000股",
            "mode": "先卖后买",
        },
        "defense": {"rule": "连续2日收盘<98", "action": "减仓1/3~1/2"},
        "calendar": [{"date": day10, "event": "长鑫科技挂牌"}],
    }


# --------------------------------------------------------------------------- #
# ① 快照断言：无档案且无流水 → prompt 相关字段与改造前逐字一致
# --------------------------------------------------------------------------- #


def test_no_playbook_payload_identical_to_baseline(mem_db, monkeypatch):
    """无档案股票：playbook 为 None，其余字段与改造前基线逐字一致。"""
    _stub_externals(monkeypatch)
    payload = _build(_make_context())

    assert payload.pop("playbook") is None
    assert payload == _BASELINE_PAYLOAD


def test_position_with_trades_keeps_constraints_unchanged(mem_db, monkeypatch):
    """PositionInfo 附带流水时，constraints（prompt 持仓段）仍只含原 5 键。"""
    _stub_externals(monkeypatch)
    trades = [
        {
            "date": "2026-07-21",
            "direction": "sell",
            "price": 130.0,
            "quantity": 500,
            "fee": 0.0,
            "realized_pnl": 8704.0,
            "note": "",
        }
    ]
    context = _make_context(trades=trades, trades_text="7/21 卖500@130(盈8704)")
    payload = _build(context)

    assert payload["constraints"] == _BASELINE_PAYLOAD["constraints"]
    row = payload["constraints"]["position"]["positions"][0]
    assert set(row.keys()) == {
        "account_id",
        "account_name",
        "quantity",
        "cost_price",
        "trading_style",
    }


# --------------------------------------------------------------------------- #
# ② 有档案 → playbook 摘要注入；异常容错
# --------------------------------------------------------------------------- #


def test_playbook_summary_injected_when_present(mem_db, monkeypatch):
    """有激活档案的股票：payload['playbook'] 为 summarize_playbook 摘要。"""
    _stub_externals(monkeypatch)
    db = mem_db()
    st = M.Stock(symbol="688110", name="东芯股份", market="CN")
    db.add(st)
    db.flush()
    db.add(
        M.StockPlaybook(
            stock_id=st.id,
            version=1,
            is_active=True,
            payload=_playbook_payload(),
            summary="",
            note="",
        )
    )
    db.commit()
    db.close()

    payload = _build(_make_context())

    summary = payload["playbook"]
    assert isinstance(summary, str) and summary
    assert "防线98" in summary
    assert "125-135" in summary
    assert "连续2日收盘<98" in summary
    assert "长鑫科技挂牌" in summary
    # 其余字段不受档案影响
    assert payload["constraints"] == _BASELINE_PAYLOAD["constraints"]


def test_playbook_loader_error_failsoft(mem_db, monkeypatch):
    """档案加载抛异常时 playbook=None，整体构建不崩。"""
    _stub_externals(monkeypatch)

    def boom(db, stock_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(context_builder, "load_active_playbook", boom)
    payload = _build(_make_context())
    assert payload["playbook"] is None


def test_playbook_other_stock_isolated(mem_db, monkeypatch):
    """其他股票的档案不会注入到本股票。"""
    _stub_externals(monkeypatch)
    db = mem_db()
    other = M.Stock(symbol="600519", name="贵州茅台", market="CN")
    db.add(other)
    db.flush()
    db.add(
        M.StockPlaybook(
            stock_id=other.id,
            version=1,
            is_active=True,
            payload=_playbook_payload(),
            summary="",
            note="",
        )
    )
    db.commit()
    db.close()

    payload = _build(_make_context())
    assert payload["playbook"] is None


# --------------------------------------------------------------------------- #
# ③ format_trades_text 紧凑文本格式
# --------------------------------------------------------------------------- #


def _trade(d, direction, price, qty, fee=0.0, pnl=None, note=""):
    return {
        "date": d,
        "direction": direction,
        "price": price,
        "quantity": qty,
        "fee": fee,
        "realized_pnl": pnl,
        "note": note,
    }


def test_trades_text_exact_format():
    """紧凑文本形如 '7/17 买1000@95; 7/21 卖500@130(盈8704)'。"""
    trades = [
        _trade("2026-07-17", "buy", 95.0, 1000),
        _trade("2026-07-21", "sell", 130.0, 500, pnl=8704.0),
    ]
    assert server.format_trades_text(trades) == "7/17 买1000@95; 7/21 卖500@130(盈8704)"


def test_trades_text_empty_when_no_trades():
    """无流水返回空串。"""
    assert server.format_trades_text([]) == ""


def test_trades_text_loss_and_adjustment():
    """亏损卖单标注(亏N)；adjustment 流水带截断备注。"""
    trades = [
        _trade("2026-07-10", "sell", 80.0, 100, pnl=-325.5),
        _trade("2026-07-11", "adjustment", 100.0, 0, note="手动调整: 成本 100→112.572"),
    ]
    text = server.format_trades_text(trades)
    assert "(亏325.5)" in text
    assert "调整" in text
    assert len(text) <= server.TRADES_TEXT_CHAR_BUDGET


def test_trades_text_within_budget_drops_oldest():
    """超 200 字符预算时从最老流水开始舍弃，保留近期操作。"""
    trades = [
        _trade(f"2026-07-{day:02d}", "buy", 100.0 + day, 1000, note="x")
        for day in range(1, 29)
    ]
    text = server.format_trades_text(trades)
    assert len(text) <= server.TRADES_TEXT_CHAR_BUDGET
    assert "7/28" in text  # 最新一笔保留
    assert "7/1 " not in text  # 最老的被舍弃


# --------------------------------------------------------------------------- #
# ④ load_portfolio_for_agent：流水注入（当日全部 + 近 10 笔）
# --------------------------------------------------------------------------- #


def _seed_position(db, *, with_trades: bool = True) -> M.Position:
    stock = M.Stock(symbol="688110", name="东芯股份", market="CN")
    db.add(stock)
    db.flush()
    db.add(M.StockAgent(stock_id=stock.id, agent_name="daily_report"))
    acc = M.Account(name="主账户", available_funds=5000.0, enabled=True)
    db.add(acc)
    db.flush()
    pos = M.Position(
        account_id=acc.id,
        stock_id=stock.id,
        cost_price=112.572,
        quantity=3150,
        invested_amount=354601.8,
        trading_style="swing",
    )
    db.add(pos)
    db.flush()
    if with_trades:
        today = date.today()
        rows = [
            # 当日两笔（全部保留）
            M.PositionTrade(
                position_id=pos.id,
                direction="buy",
                price=120.0,
                quantity=100,
                fee=5.0,
                traded_at=datetime.combine(today, time(9, 45)),
                realized_pnl=None,
                note="补仓",
            ),
            M.PositionTrade(
                position_id=pos.id,
                direction="sell",
                price=130.0,
                quantity=100,
                fee=5.0,
                traded_at=datetime.combine(today, time(14, 0)),
                realized_pnl=995.0,
                note="做T",
            ),
        ]
        # 12 笔历史流水：近 8 笔应被选中补齐到 10 条
        for i in range(12):
            rows.append(
                M.PositionTrade(
                    position_id=pos.id,
                    direction="buy",
                    price=100.0 + i,
                    quantity=100,
                    fee=0.0,
                    traded_at=datetime.combine(
                        today - timedelta(days=20 - i), time(10, 0)
                    ),
                    realized_pnl=None,
                    note=f"历史{i}",
                )
            )
        db.add_all(rows)
    db.commit()
    return pos


def test_load_portfolio_injects_recent_trades(mem_db):
    """持仓附带流水：当日全部 + 历史补齐到 10 笔，时间升序，字段齐全。"""
    db = mem_db()
    _seed_position(db)
    db.close()

    portfolio = server.load_portfolio_for_agent("daily_report")
    positions = portfolio.all_positions
    assert len(positions) == 1
    pos = positions[0]

    trades = pos.trades
    assert len(trades) == 10  # 当日 2 笔 + 历史近 8 笔
    today_str = date.today().strftime("%Y-%m-%d")
    todays = [t for t in trades if t["date"] == today_str]
    assert len(todays) == 2  # 当日全部保留
    # 时间升序
    assert [t["date"] for t in trades] == sorted(t["date"] for t in trades)
    # 字段齐全：日期/方向/价/量/费/realized_pnl/note
    for t in trades:
        assert set(t.keys()) == {
            "date",
            "direction",
            "price",
            "quantity",
            "fee",
            "realized_pnl",
            "note",
        }
    sell = next(t for t in trades if t["direction"] == "sell")
    assert sell["realized_pnl"] == 995.0
    assert sell["fee"] == 5.0
    assert sell["note"] == "做T"

    # 紧凑文本：含方向/价/量与盈亏标注，且不超预算
    text = pos.trades_text
    assert text
    assert len(text) <= server.TRADES_TEXT_CHAR_BUDGET
    assert "@130" in text and "(盈995)" in text


def test_load_portfolio_no_trades_backward_compatible(mem_db):
    """无流水持仓：trades=[]、trades_text=''，其余行为不变。"""
    db = mem_db()
    _seed_position(db, with_trades=False)
    db.close()

    portfolio = server.load_portfolio_for_agent("daily_report")
    pos = portfolio.all_positions[0]
    assert pos.trades == []
    assert pos.trades_text == ""
    assert pos.cost_price == 112.572
    assert pos.quantity == 3150


def test_load_portfolio_today_trades_all_kept_beyond_limit(mem_db):
    """当日流水超过 10 笔时全部保留（当日全部优先于近 10 笔上限）。"""
    db = mem_db()
    pos = _seed_position(db, with_trades=False)
    today = date.today()
    db.add_all(
        M.PositionTrade(
            position_id=pos.id,
            direction="buy",
            price=100.0,
            quantity=100,
            fee=0.0,
            traded_at=datetime.combine(today, time(9, 30)) + timedelta(minutes=i),
            realized_pnl=None,
            note=f"t{i}",
        )
        for i in range(15)
    )
    db.commit()
    db.close()

    portfolio = server.load_portfolio_for_agent("daily_report")
    pos_info = portfolio.all_positions[0]
    assert len(pos_info.trades) == 15  # 当日全部
    assert len(pos_info.trades_text) <= server.TRADES_TEXT_CHAR_BUDGET


def test_load_portfolio_agent_without_stocks(mem_db):
    """Agent 无关联股票时返回空组合（原行为不变）。"""
    db = mem_db()
    db.close()
    portfolio = server.load_portfolio_for_agent("nonexistent_agent")
    assert portfolio.accounts == []
    assert portfolio.all_positions == []
