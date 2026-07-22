"""P3b 收盘复盘 daily_report 改版测试（doc/12 §3 P3b、doc/14 §1 P3b）。

覆盖：
- 零漂移硬约束：无方案档案且无龙虎榜数据时，渲染 prompt 与改造前**逐字一致**
  （system prompt 基础段 sha256 快照 + user content 基线快照）；
- 有档案股票：方案档案章节（✅触发器/💰持仓盈亏/📅次日预案/🗓日历/当日流水）
  注入 prompt，且条件段模板进入 system prompt；
- 混合批次：无档案股票的个股段落不受档案股票影响；
- 触发器状态数据源：PriceAlertRule(playbook_id) + 当日 PriceAlertHit；
- 持仓盈亏：position_trades 已实现盈亏聚合 + 浮动盈亏（非简单价差敷衍）；
- 龙虎榜：md_dragon_tiger accessor、按 watchlist CN 过滤、失败/空容错跳过章节。

一律内存 sqlite + mock，不碰真实库、不做真实网络调用。
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.agents import daily_report
from src.agents.base import AccountInfo, PortfolioInfo, PositionInfo
from src.agents.daily_report import DailyReportAgent
from src.core import marketdata_client
from src.models.market import MarketCode
from src.web import models as M
from src.web.database import Base

# 改造前 prompts/daily_report.txt 全文的 sha256（条件段标记追加前捕获）。
# 若有人改动了标记之前的基础段，此断言立即失败 —— 零漂移的第一道防线。
_BASELINE_PROMPT_SHA256 = (
    "34aeacfe45a6afa42a48efa36aba6c1afe100604415fd90b5e77853d29a2b09d"
)

# 无档案/无龙虎榜时 user content 基线（单只 CN 自选股、无任何数据包）。
_BASELINE_USER_TEMPLATE = (
    "## 日期：{today}\n"
    "\n"
    "## 大盘指数\n"
    "\n"
    "## 自选股详情\n"
    "\n"
    "### 东芯股份（688110）\n"
    "- 今日：行情数据缺失\n"
    "- 相关新闻：暂无"
)


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def mem_db(monkeypatch):
    """内存 sqlite；接管 daily_report 的 SessionLocal。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(daily_report, "SessionLocal", Session)
    try:
        yield Session
    finally:
        engine.dispose()


def _watch_stock(symbol="688110", name="东芯股份", market=MarketCode.CN):
    return SimpleNamespace(symbol=symbol, market=market, name=name)


def _make_context(watchlist=None, positions=None):
    accounts = []
    if positions:
        accounts = [
            AccountInfo(id=1, name="默认账户", available_funds=150000.0, positions=positions)
        ]
    return SimpleNamespace(
        watchlist=watchlist or [_watch_stock()],
        portfolio=PortfolioInfo(accounts=accounts),
    )


def _empty_data(**overrides):
    data = {
        "indices": [],
        "signal_packs": {},
        "symbol_contexts": {},
        "quality_overview": {},
        "playbook_sections": {},
        "dragon_tiger": [],
        "timestamp": "",
    }
    data.update(overrides)
    return data


def _playbook_payload() -> dict:
    return {
        "schema_version": 1,
        "meta": {"name": "东芯股份抄底实施方案", "version_label": "v3.1"},
        "price_levels": [{"label": "防线", "value": 98}],
        "t_zone": {"sell_range": [125, 135], "buyback_range": [110, 115]},
        "defense": {"rule": "连续2日收盘<98", "action": "减仓1/3~1/2"},
        "calendar": [
            {
                "date": (date.today() + timedelta(days=5)).isoformat(),
                "event": "长鑫科技挂牌（预计）",
                "bias": "中性偏多",
                "plan": "挂牌后板块止跌→情绪底信号",
            },
            {
                "date": (date.today() - timedelta(days=1)).isoformat(),
                "event": "已过期事件",
            },
            {
                "date": (date.today() + timedelta(days=60)).isoformat(),
                "event": "超窗事件",
            },
        ],
        "trigger_hints": {"防线98": "防线：连续2日收盘<98→减仓1/3~1/2"},
    }


def _seed_playbook_stock(db) -> M.Stock:
    stock = M.Stock(symbol="688110", name="东芯股份", market="CN")
    db.add(stock)
    db.flush()
    db.add(
        M.StockPlaybook(
            stock_id=stock.id,
            version=1,
            is_active=True,
            payload=_playbook_payload(),
            summary="",
            note="",
        )
    )
    db.commit()
    return stock


def _section_fixture() -> dict:
    return {
        "summary": "方案:东芯 v3.1\n防线:连续2日收盘<98→减仓1/3~1/2",
        "triggers": [
            {
                "name": "防线98",
                "hint": "防线：连续2日收盘<98→减仓1/3~1/2",
                "enabled": True,
                "triggered": False,
                "times": [],
            },
            {
                "name": "做T卖出区",
                "hint": "做T（先卖后买）：125-135卖出",
                "enabled": True,
                "triggered": True,
                "times": ["13:05"],
            },
        ],
        "position": {
            "quantity": 2150,
            "avg_cost": 112.572,
            "floating_pnl": 15997.0,
            "realized_pnl": 8704.0,
            "trades_text": "7/22 卖500@130(盈8704)",
            "has_today_trades": True,
        },
        "calendar": [
            {
                "date": (date.today() + timedelta(days=5)).isoformat(),
                "event": "长鑫科技挂牌（预计）",
                "bias": "中性偏多",
                "plan": "挂牌后板块止跌→情绪底信号",
                "days_until": 5,
            }
        ],
    }


# --------------------------------------------------------------------------- #
# ① 零漂移快照断言：无档案且无龙虎榜 → prompt 与改造前逐字一致
# --------------------------------------------------------------------------- #


def test_prompt_base_part_byte_identical_to_baseline():
    """prompt 文件条件段标记之前的基础段与改造前全文逐字一致（sha256 快照）。"""
    base, conditional = daily_report._load_prompt_parts()
    assert hashlib.sha256(base.encode("utf-8")).hexdigest() == _BASELINE_PROMPT_SHA256
    assert conditional.startswith(daily_report._CONDITIONAL_MARKER)


def test_no_playbook_prompt_identical_to_baseline():
    """无档案且无龙虎榜数据：system/user prompt 均与改造前逐字一致。"""
    agent = DailyReportAgent()
    system_prompt, user_content = agent.build_prompt(_empty_data(), _make_context())

    base, _ = daily_report._load_prompt_parts()
    assert system_prompt == base  # 不拼接条件段
    assert hashlib.sha256(system_prompt.encode("utf-8")).hexdigest() == (
        _BASELINE_PROMPT_SHA256
    )
    assert user_content == _BASELINE_USER_TEMPLATE.format(
        today=date.today().strftime("%Y-%m-%d")
    )


def test_empty_dragon_tiger_adds_no_section():
    """龙虎榜空数据：报告中不出现龙虎榜章节（容错跳过）。"""
    agent = DailyReportAgent()
    _, user_content = agent.build_prompt(
        _empty_data(dragon_tiger=[]), _make_context()
    )
    assert "龙虎榜" not in user_content


# --------------------------------------------------------------------------- #
# ② 有档案 → 档案章节注入；混合批次无档案股票不受影响
# --------------------------------------------------------------------------- #


def test_playbook_section_rendered_in_prompt():
    """有档案股票：✅/💰/📝/📅/🗓 各小节与 trades_text 注入 prompt。"""
    agent = DailyReportAgent()
    data = _empty_data(playbook_sections={"688110": _section_fixture()})
    system_prompt, user_content = agent.build_prompt(data, _make_context())

    assert daily_report._CONDITIONAL_MARKER in system_prompt
    assert "方案档案章节" in system_prompt
    assert "- 方案档案：" in user_content
    assert "✅ 触发器状态：" in user_content
    assert "防线98：未触发" in user_content
    assert "做T卖出区：已触发(13:05)" in user_content
    assert "连续2日收盘<98" in user_content  # 方案提示
    assert "💰 持仓盈亏（流水精确口径）：2150股@均价112.57" in user_content
    assert "浮动+15997元" in user_content
    assert "已实现+8704元" in user_content
    assert "合计+24701元" in user_content
    assert "📝 当日操作（流水）：7/22 卖500@130(盈8704)" in user_content
    assert "📅 次日预案依据（方案摘要）：" in user_content
    assert "🗓 日历提醒：" in user_content
    assert "长鑫科技挂牌（预计）" in user_content
    assert "〔5天后〕" in user_content


def test_mixed_batch_non_playbook_stock_unchanged():
    """混合批次：无档案股票的个股段落与零数据基线逐字一致。"""
    agent = DailyReportAgent()
    watchlist = [_watch_stock(), _watch_stock("600519", "贵州茅台")]
    data = _empty_data(playbook_sections={"688110": _section_fixture()})
    _, user_content = agent.build_prompt(data, _make_context(watchlist=watchlist))

    # 无档案股票段落逐字出现
    assert (
        "### 贵州茅台（600519）\n- 今日：行情数据缺失\n- 相关新闻：暂无"
    ) in user_content
    # 档案股票段落含章节,且章节不会泄漏进贵州茅台段落
    maotai_block = user_content.split("### 贵州茅台（600519）", 1)[1]
    assert "方案档案" not in maotai_block


# --------------------------------------------------------------------------- #
# ③ 档案章节数据构建（内存库）
# --------------------------------------------------------------------------- #


def test_build_playbook_sections_skipped_without_playbook(mem_db):
    """symbol_contexts 无 playbook 摘要 → 不建章节（不查库）。"""
    agent = DailyReportAgent()
    sections = agent._build_playbook_sections(
        _make_context(), {}, {"688110": {"playbook": None}}
    )
    assert sections == {}


def test_build_playbook_sections_full(mem_db):
    """触发器状态/持仓盈亏/日历/流水 全链路（内存库）。"""
    db = mem_db()
    stock = _seed_playbook_stock(db)
    playbook = (
        db.query(M.StockPlaybook).filter(M.StockPlaybook.stock_id == stock.id).one()
    )
    rule = M.PriceAlertRule(
        stock_id=stock.id,
        name="防线98",
        enabled=True,
        condition_group={},
        playbook_id=playbook.id,
    )
    db.add(rule)
    db.flush()
    # 当日命中（trigger_time 存 UTC naive）
    db.add(
        M.PriceAlertHit(
            rule_id=rule.id,
            stock_id=stock.id,
            trigger_time=datetime.now(timezone.utc).replace(tzinfo=None),
            trigger_bucket="x",
            trigger_snapshot={"price": 97.5},
        )
    )
    # 昨日命中不应计入当日状态
    db.add(
        M.PriceAlertHit(
            rule_id=rule.id,
            stock_id=stock.id,
            trigger_time=datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=2),
            trigger_bucket="y",
            trigger_snapshot={},
        )
    )
    # 持仓 + 流水（已实现盈亏聚合源）
    acc = M.Account(name="默认账户", available_funds=150000.0, enabled=True)
    db.add(acc)
    db.flush()
    pos = M.Position(
        account_id=acc.id,
        stock_id=stock.id,
        cost_price=112.572,
        quantity=2150,
        invested_amount=242029.8,
        trading_style="swing",
    )
    db.add(pos)
    db.flush()
    db.add(
        M.PositionTrade(
            position_id=pos.id,
            direction="sell",
            price=130.0,
            quantity=500,
            fee=10.0,
            traded_at=datetime.combine(date.today(), time(13, 5)),
            realized_pnl=8704.0,
            note="做T",
        )
    )
    db.commit()
    stock_id = stock.id
    db.close()

    today_str = date.today().strftime("%Y-%m-%d")
    pos_info = PositionInfo(
        account_id=1,
        account_name="默认账户",
        stock_id=stock_id,
        symbol="688110",
        name="东芯股份",
        market=MarketCode.CN,
        cost_price=112.572,
        quantity=2150,
        invested_amount=242029.8,
        trading_style="swing",
        trades=[
            {
                "date": today_str,
                "direction": "sell",
                "price": 130.0,
                "quantity": 500,
                "fee": 10.0,
                "realized_pnl": 8704.0,
                "note": "做T",
            }
        ],
        trades_text=f"{date.today().month}/{date.today().day} 卖500@130(盈8704)",
    )
    context = _make_context(positions=[pos_info])
    packs = {"688110": SimpleNamespace(quote=SimpleNamespace(current_price=120.0))}
    symbol_contexts = {"688110": {"playbook": "方案:东芯 v3.1\n防线:连续2日收盘<98"}}

    agent = DailyReportAgent()
    sections = agent._build_playbook_sections(context, packs, symbol_contexts)

    assert set(sections.keys()) == {"688110"}
    sec = sections["688110"]

    # ✅ 触发器：当日命中 triggered、昨日命中不计入；方案提示带出
    assert len(sec["triggers"]) == 1
    trig = sec["triggers"][0]
    assert trig["name"] == "防线98"
    assert trig["triggered"] is True
    assert len(trig["times"]) == 1  # 仅当日一笔
    assert "减仓" in trig["hint"]

    # 💰 持仓盈亏：流水精确口径（浮动 + 已实现聚合）
    pnl = sec["position"]
    assert pnl["quantity"] == 2150
    assert pnl["avg_cost"] == pytest.approx(112.572)
    assert pnl["realized_pnl"] == pytest.approx(8704.0)
    assert pnl["floating_pnl"] == pytest.approx((120.0 - 112.572) * 2150)
    assert pnl["has_today_trades"] is True
    assert "卖500@130" in pnl["trades_text"]

    # 🗓 日历：仅未来 30 天;过期/超窗剔除;days_until 正确
    cal = sec["calendar"]
    assert [c["event"] for c in cal] == ["长鑫科技挂牌（预计）"]
    assert cal[0]["days_until"] == 5
    assert cal[0]["bias"] == "中性偏多"
    assert cal[0]["plan"] == "挂牌后板块止跌→情绪底信号"


def test_trigger_status_no_rules_renders_placeholder(mem_db):
    """有档案但无关联告警规则 → triggers=[]，渲染为「未配置关联告警规则」。"""
    db = mem_db()
    _seed_playbook_stock(db)
    db.close()

    agent = DailyReportAgent()
    sections = agent._build_playbook_sections(
        _make_context(), {}, {"688110": {"playbook": "方案:东芯 v3.1"}}
    )
    sec = sections["688110"]
    assert sec["triggers"] == []
    rendered = DailyReportAgent._render_playbook_section(sec)
    assert any("未配置关联告警规则" in line for line in rendered)


def test_build_playbook_sections_db_error_failsoft(monkeypatch):
    """DB 会话创建异常 → 整体容错返回 {}，不中断报告。"""
    monkeypatch.setattr(
        daily_report,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    agent = DailyReportAgent()
    sections = agent._build_playbook_sections(
        _make_context(), {}, {"688110": {"playbook": "方案:东芯 v3.1"}}
    )
    assert sections == {}


# --------------------------------------------------------------------------- #
# ④ 龙虎榜：accessor / 过滤 / 容错
# --------------------------------------------------------------------------- #


def test_md_dragon_tiger_accessor_maps_fields(monkeypatch):
    """marketdata_client.md_dragon_tiger:DragonTigerItem → dict,date 透传。"""
    from marketdata import DragonTigerItem

    calls = {}

    class _FakeMD:
        def dragon_tiger(self, *, date=None, market="CN"):
            calls["date"] = date
            calls["market"] = market
            return [
                DragonTigerItem(
                    trade_date="2026-07-22",
                    symbol="688110",
                    name="东芯股份",
                    reason="日涨幅偏离值达7%",
                    close=120.46,
                    change_pct=20.0,
                    net_buy=1.23e8,
                    buy_amt=2e8,
                    sell_amt=0.77e8,
                    turnover_pct=8.56,
                )
            ]

    monkeypatch.setattr(marketdata_client, "get_market_data", lambda: _FakeMD())
    rows = marketdata_client.md_dragon_tiger(date="2026-07-22")

    assert calls == {"date": "2026-07-22", "market": "CN"}
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "688110"
    assert row["reason"] == "日涨幅偏离值达7%"
    assert row["net_buy"] == pytest.approx(1.23e8)
    assert row["turnover_pct"] == pytest.approx(8.56)


def test_load_dragon_tiger_filters_watchlist_cn(monkeypatch):
    """龙虎榜按 watchlist 中 CN 个股过滤:非自选股/非 CN 市场剔除。"""
    items = [
        {"symbol": "688110", "name": "东芯股份", "reason": "涨幅偏离", "net_buy": 1e8},
        {"symbol": "600519", "name": "贵州茅台", "reason": "跌幅偏离", "net_buy": -5e7},
        {"symbol": "00700", "name": "腾讯控股", "reason": "港股", "net_buy": 1e7},
    ]
    monkeypatch.setattr(daily_report, "md_dragon_tiger", lambda **kw: items)
    watchlist = [
        _watch_stock("688110", "东芯股份", MarketCode.CN),
        _watch_stock("00700", "腾讯控股", MarketCode.HK),
    ]
    agent = DailyReportAgent()
    result = agent._load_dragon_tiger(_make_context(watchlist=watchlist))

    assert [it["symbol"] for it in result] == ["688110"]


def test_load_dragon_tiger_failsoft_on_exception(monkeypatch):
    """接口失败 → 容错返回 []（记日志），不中断报告。"""

    def boom(**kw):
        raise RuntimeError("no datasource")

    monkeypatch.setattr(daily_report, "md_dragon_tiger", boom)
    agent = DailyReportAgent()
    assert agent._load_dragon_tiger(_make_context()) == []


def test_load_dragon_tiger_no_cn_watchlist_skips_call(monkeypatch):
    """watchlist 无 CN 个股 → 不调用接口直接返回 []。"""
    called = []

    def spy(**kw):
        called.append(kw)
        return [{"symbol": "688110"}]

    monkeypatch.setattr(daily_report, "md_dragon_tiger", spy)
    watchlist = [_watch_stock("00700", "腾讯控股", MarketCode.HK)]
    agent = DailyReportAgent()
    assert agent._load_dragon_tiger(_make_context(watchlist=watchlist)) == []
    assert called == []


def test_dragon_tiger_section_rendered_in_prompt():
    """有龙虎榜数据：报告含章节与字段，system prompt 拼接条件段。"""
    agent = DailyReportAgent()
    items = [
        {
            "symbol": "688110",
            "name": "东芯股份",
            "reason": "日涨幅偏离值达7%",
            "close": 120.46,
            "change_pct": 20.0,
            "net_buy": 1.23e8,
            "turnover_pct": 8.56,
        }
    ]
    system_prompt, user_content = agent.build_prompt(
        _empty_data(dragon_tiger=items), _make_context()
    )
    assert daily_report._CONDITIONAL_MARKER in system_prompt
    assert "龙虎榜章节" in system_prompt
    assert "## 龙虎榜（自选股上榜）" in user_content
    assert "东芯股份（688110）：日涨幅偏离值达7%" in user_content
    assert "收盘120.46 +20.00%" in user_content
    assert "龙虎榜净买+12300万" in user_content
    assert "换手率8.6%" in user_content
