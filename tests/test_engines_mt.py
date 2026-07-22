"""MT-P3 引擎路由多租户测试（价格提醒 / 模拟盘 / 建议池 / TA key 传递）。

覆盖：
1. 价格提醒：两租户规则按行级 tenant_id 路由通知渠道（T21），命中记录归属租户；
2. 模拟盘：双租户账户独立评估（T9），共享市场信号不互相阻塞，单租户失败不阻断；
3. 建议池：去重键加 tenant 维度，跨租户同内容不互吞（风险 #19）；
4. TA LLM：按调用传 key，进程 env 不被注入（风险 #20）；
5. PANWATCH_SINGLE_TENANT='1'（默认）全部路径行为与单用户等价。
"""

from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M
import src.web.tenant_context as tc
from src.web.database import Base
from src.core import price_alert_engine as pae
from src.core import paper_trading_engine as pte
from src.core import paper_trading_notifier as ptn
from src.core import suggestion_pool as sp


@pytest.fixture
def mt_db(monkeypatch):
    """内存库 + do_orm_execute 事件 + 各引擎 SessionLocal 替换为内存 Session。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    event.listen(Session, "do_orm_execute", tc.apply_tenant_filter)
    old_cache = dict(tc._tenant_column_cache)
    tc.refresh_tenant_column_cache(engine)

    monkeypatch.setattr(pae, "SessionLocal", Session)
    monkeypatch.setattr(pte, "SessionLocal", Session)
    monkeypatch.setattr(ptn, "SessionLocal", Session)
    monkeypatch.setattr(sp, "SessionLocal", Session)
    # paper_trading_bridge 在函数内 from src.web.database import SessionLocal
    monkeypatch.setattr("src.web.database.SessionLocal", Session)

    yield Session

    tc._tenant_column_cache.clear()
    tc._tenant_column_cache.update(old_cache)


class _RecordingNotifier:
    """记录 add_channel 调用的 NotifierManager 替身。"""

    instances: list["_RecordingNotifier"] = []

    def __init__(self, policy=None):
        self.policy = policy
        self.channels: list[tuple[str, dict]] = []
        _RecordingNotifier.instances.append(self)

    def add_channel(self, type_: str, config: dict) -> None:
        self.channels.append((type_, config))

    async def notify_with_result(self, title: str, content: str) -> dict:
        return {"success": True}

    async def notify(self, title: str, content: str) -> None:
        return None


def _seed_alert_tenants(db):
    """两租户同代码股票 + 规则 + 各自默认渠道 + 管理员共享渠道。"""
    s1 = M.Stock(tenant_id=1, symbol="600519", name="贵州茅台", market="CN")
    s2 = M.Stock(tenant_id=2, symbol="600519", name="贵州茅台", market="CN")
    db.add_all([s1, s2])
    db.flush()
    cond = {"op": "and", "items": [{"type": "price", "op": ">", "value": 0}]}
    r1 = M.PriceAlertRule(
        tenant_id=1, stock_id=s1.id, name="r1", enabled=True,
        condition_group=cond, market_hours_mode="always", repeat_mode="repeat",
    )
    db.add(r1)
    ch1 = M.NotifyChannel(
        tenant_id=1, name="c1", type="webhook", config={"url": "t1"},
        enabled=True, is_default=True,
    )
    ch2 = M.NotifyChannel(
        tenant_id=2, name="c2", type="webhook", config={"url": "t2"},
        enabled=True, is_default=True,
    )
    ch_shared = M.NotifyChannel(
        tenant_id=1, name="shared", type="webhook", config={"url": "shared"},
        enabled=True, is_default=False, is_shared=True,
    )
    db.add_all([ch1, ch2, ch_shared])
    db.flush()
    r2 = M.PriceAlertRule(
        tenant_id=2, stock_id=s2.id, name="r2", enabled=True,
        condition_group=dict(cond), market_hours_mode="always",
        repeat_mode="repeat", notify_channel_ids=[ch_shared.id],
    )
    db.add(r2)
    db.commit()
    return s1, s2, r1, r2, ch1, ch2, ch_shared


async def _fake_quotes(self, stocks):
    return {
        ("CN", "600519"): {"current_price": 10.0, "change_pct": 1.0, "volume": 100}
    }


# ---------------------------------------------------------------------------
# 1. 价格提醒：按规则行 tenant_id 路由
# ---------------------------------------------------------------------------


def test_price_alert_routes_channels_by_rule_tenant(mt_db, monkeypatch):
    """多租户：规则→租户→该租户渠道；托管共享渠道可被其他租户引用。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    db = mt_db()
    s1, s2, r1, r2, ch1, ch2, ch_shared = _seed_alert_tenants(db)
    r1_id, r2_id = r1.id, r2.id
    db.close()

    _RecordingNotifier.instances = []
    monkeypatch.setattr(pae, "NotifierManager", _RecordingNotifier)
    monkeypatch.setattr(pae.PriceAlertEngine, "_fetch_quotes_map", _fake_quotes)

    result = asyncio.run(pae.PriceAlertEngine().scan_once())

    assert result["triggered"] == 2, result
    assert len(_RecordingNotifier.instances) == 2
    # 租户 1 规则：默认渠道回退 → 仅本租户默认渠道
    assert _RecordingNotifier.instances[0].channels == [("webhook", {"url": "t1"})]
    # 租户 2 规则：显式引用管理员托管共享渠道 → 可见并路由
    assert _RecordingNotifier.instances[1].channels == [("webhook", {"url": "shared"})]

    # 命中记录按规则归属租户
    db = mt_db()
    hits = {h.rule_id: h for h in db.query(M.PriceAlertHit).all()}
    assert hits[r1_id].tenant_id == 1
    assert hits[r2_id].tenant_id == 2
    db.close()


def test_price_alert_channel_isolation(mt_db, monkeypatch):
    """多租户：租户 2 的默认渠道解析看不到租户 1 的私有渠道。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    db = mt_db()
    _seed_alert_tenants(db)
    engine = pae.PriceAlertEngine()
    r2 = db.query(M.PriceAlertRule).filter_by(tenant_id=2).first()
    # 清掉显式 ids → 走默认渠道回退
    r2.notify_channel_ids = []
    db.commit()
    channels = engine._resolve_channels(db, r2)
    assert {c.name for c in channels} == {"c2"}
    db.close()


def test_price_alert_single_tenant_passthrough(mt_db, monkeypatch):
    """单租户直通：渠道解析不加租户谓词（原行为：全部启用默认渠道）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    db = mt_db()
    _seed_alert_tenants(db)
    engine = pae.PriceAlertEngine()
    r2 = db.query(M.PriceAlertRule).filter_by(tenant_id=2).first()
    r2.notify_channel_ids = []
    db.commit()
    channels = engine._resolve_channels(db, r2)
    assert {c.name for c in channels} == {"c1", "c2"}
    db.close()


def test_price_alert_single_tenant_hit_tenant_default(mt_db, monkeypatch):
    """单租户直通：扫描返回结构不变，命中记录正常落库。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    db = mt_db()
    _seed_alert_tenants(db)
    db.close()
    _RecordingNotifier.instances = []
    monkeypatch.setattr(pae, "NotifierManager", _RecordingNotifier)
    monkeypatch.setattr(pae.PriceAlertEngine, "_fetch_quotes_map", _fake_quotes)

    result = asyncio.run(pae.PriceAlertEngine().scan_once())
    assert result["triggered"] == 2
    assert set(result.keys()) == {
        "total_rules", "triggered", "skipped", "items", "scanned_at",
    }
    db = mt_db()
    assert db.query(M.PriceAlertHit).count() == 2
    db.close()


# ---------------------------------------------------------------------------
# 2. 模拟盘：双租户独立评估
# ---------------------------------------------------------------------------


def _seed_pt_env(db):
    t1 = M.Tenant(id=1, name="默认租户", is_default=True)
    t2 = M.Tenant(id=2, name="租户B")
    sig = M.StrategySignalRun(
        tenant_id=0,  # 市场级哨兵行：所有租户可见
        snapshot_date="2026-07-22",
        stock_symbol="600519", stock_market="CN", stock_name="贵州茅台",
        strategy_code="trend_follow", strategy_name="趋势延续",
        score=80.0, rank_score=80.0, status="active", action="buy",
        entry_low=9.0, entry_high=11.0, holding_days=3,
    )
    db.add_all([t1, t2, sig])
    db.commit()


def _fake_pt_quotes(self, symbols_markets):
    return {("CN", "600519"): {"current_price": 10.0, "change_pct": 0.5}}


def test_paper_trading_two_tenants_independent(mt_db, monkeypatch):
    """多租户：两租户各自建账户、独立评估同一市场信号，互不阻塞。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    db = mt_db()
    _seed_pt_env(db)
    db.close()
    monkeypatch.setattr(
        pte.PaperTradingEngine, "_fetch_quotes_map", _fake_pt_quotes
    )

    result = pte.PaperTradingEngine()._scan_sync()

    assert result["status"] == "ok"
    assert result["opened"] == 2
    assert {t["tenant_id"] for t in result["tenants"]} == {1, 2}
    assert {e["tenant_id"] for e in result["entry_events"]} == {1, 2}

    db = mt_db()
    accounts = {a.tenant_id: a for a in db.query(M.PaperTradingAccount).all()}
    assert set(accounts.keys()) == {1, 2}
    positions = db.query(M.PaperTradingPosition).all()
    assert len(positions) == 2
    for pos in positions:
        # 持仓归属本租户账户（共享信号不串账户）
        assert pos.account_id == accounts[pos.tenant_id].id
        # 两租户各自建仓：同一信号在两个租户命名空间各开一仓
        assert pos.stock_symbol == "600519"
    db.close()


def test_paper_trading_tenant_failure_isolated(mt_db, monkeypatch):
    """多租户：租户 2 账户禁用不阻断租户 1 评估（租户粒度容错）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    db = mt_db()
    _seed_pt_env(db)
    db.add(
        M.PaperTradingAccount(
            tenant_id=2, initial_capital=100.0, current_capital=100.0,
            peak_capital=100.0, enabled=False,
        )
    )
    db.commit()
    db.close()
    monkeypatch.setattr(
        pte.PaperTradingEngine, "_fetch_quotes_map", _fake_pt_quotes
    )

    result = pte.PaperTradingEngine()._scan_sync()

    assert result["opened"] == 1
    by_tenant = {t["tenant_id"]: t["status"] for t in result["tenants"]}
    assert by_tenant == {1: "ok", 2: "disabled"}
    db = mt_db()
    positions = db.query(M.PaperTradingPosition).all()
    assert len(positions) == 1
    assert positions[0].tenant_id == 1
    db.close()


def test_paper_trading_single_tenant_equivalence(mt_db, monkeypatch):
    """单租户直通：返回结构原样（无 tenants 键），禁用账户返回 disabled。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    db = mt_db()
    _seed_pt_env(db)
    db.close()
    monkeypatch.setattr(
        pte.PaperTradingEngine, "_fetch_quotes_map", _fake_pt_quotes
    )
    engine = pte.PaperTradingEngine()

    result = engine._scan_sync()
    assert result["status"] == "ok"
    assert result["opened"] == 1
    assert "tenants" not in result
    assert {e.get("tenant_id") for e in result["entry_events"]} == {None}

    # 禁用账户 → 原样 {"status": "disabled"}
    db = mt_db()
    acc = db.query(M.PaperTradingAccount).first()
    assert acc.tenant_id == 1
    acc.enabled = False
    db.commit()
    db.close()
    assert engine._scan_sync() == {"status": "disabled"}


def test_paper_trading_notifier_routes_by_tenant(mt_db, monkeypatch):
    """多租户：模拟盘通知按账户租户路由渠道；单租户直通为原全量行为。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    db = mt_db()
    db.add(M.AppSettings(key="pt_notify_enabled", value="true"))
    db.add(M.NotifyChannel(
        tenant_id=1, name="c1", type="webhook", config={"url": "t1"},
        enabled=True, is_default=True,
    ))
    db.add(M.NotifyChannel(
        tenant_id=2, name="c2", type="webhook", config={"url": "t2"},
        enabled=True, is_default=True,
    ))
    db.commit()
    db.close()

    _RecordingNotifier.instances = []
    monkeypatch.setattr(ptn, "NotifierManager", _RecordingNotifier)

    mgr2 = ptn._build_notifier(2)
    assert mgr2 is not None
    assert mgr2.channels == [("webhook", {"url": "t2"})]

    mgr1 = ptn._build_notifier(1)
    assert mgr1 is not None
    assert mgr1.channels == [("webhook", {"url": "t1"})]

    # 单租户直通：不过滤（原行为）
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    mgr = ptn._build_notifier()
    assert mgr is not None
    assert {c[1]["url"] for c in mgr.channels} == {"t1", "t2"}


def test_paper_trading_notifier_tenant_settings_override(mt_db, monkeypatch):
    """多租户：pt_notify_* 租户级 tenant_settings 优先，缺省回退 app_settings。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    db = mt_db()
    db.add(M.AppSettings(key="pt_notify_enabled", value="true"))
    db.add(M.AppSettings(key="pt_notify_realtime", value="true"))
    db.add(M.TenantSettings(tenant_id=2, key="pt_notify_realtime", value="false"))
    db.commit()
    db.close()

    assert ptn._is_mode_enabled("pt_notify_realtime", 1) is True
    # 租户 2 覆盖了 realtime 开关
    assert ptn._is_mode_enabled("pt_notify_realtime", 2) is False
    # 未覆盖的键回退 app_settings
    assert ptn._is_enabled(2) is True

    # 单租户直通：只读 app_settings，tenant_settings 不生效（原行为）
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    assert ptn._is_mode_enabled("pt_notify_realtime") is True


# ---------------------------------------------------------------------------
# 3. 建议池：去重键加 tenant 维度
# ---------------------------------------------------------------------------


def _save_buy(**overrides):
    params = dict(
        stock_symbol="600519", stock_name="贵州茅台", action="buy",
        action_label="买入", agent_name="daily_report", signal="放量突破",
        reason="测试",
    )
    params.update(overrides)
    return sp.save_suggestion(**params)


def _seed_suggestion(db, tenant_id: int) -> M.StockSuggestion:
    """直接插一行 expires_at=None 的建议（dedupe 合并分支可触发的形态）。"""
    row = M.StockSuggestion(
        tenant_id=tenant_id, stock_symbol="600519", stock_market="CN",
        stock_name="贵州茅台", action="buy", action_label="买入",
        signal="放量突破", reason="测试", agent_name="daily_report",
        agent_label="收盘复盘", expires_at=None,
    )
    db.add(row)
    db.commit()
    return row


def test_suggestion_pool_dedupe_not_cross_tenant(mt_db, monkeypatch):
    """多租户：去重键含 tenant 维度，跨租户同内容建议不互吞（风险 #19）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    db = mt_db()
    _seed_suggestion(db, tenant_id=1)
    db.close()

    # 租户 2 保存同内容建议：去重查询scoped到租户 2 → 看不到租户 1 的行 → 成行
    with tc.tenant_scope(2):
        assert _save_buy() is True
    db = mt_db()
    rows = db.query(M.StockSuggestion).all()
    assert len(rows) == 2
    assert {r.tenant_id for r in rows} == {1, 2}
    db.close()

    # 租户 1 保存同内容建议：命中本租户去重 → 合并，不新增行
    with tc.tenant_scope(1):
        assert _save_buy() is True
    db = mt_db()
    assert db.query(M.StockSuggestion).count() == 2
    db.close()


def test_suggestion_pool_single_tenant_equivalence(mt_db, monkeypatch):
    """单租户直通：去重行为原样（同内容窗口内合并），行归属租户 1。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    db = mt_db()
    _seed_suggestion(db, tenant_id=1)
    db.close()

    assert _save_buy() is True  # 命中去重 → 合并，不新增

    db = mt_db()
    rows = db.query(M.StockSuggestion).all()
    assert len(rows) == 1
    assert rows[0].tenant_id == 1
    db.close()


def test_suggestion_pool_dedupe_with_naive_expires_at(mt_db, monkeypatch):
    """回归：SQLite 读回的 expires_at 为 naive 时去重不得静默失效。

    修复前：dedupe 合并分支比较 naive expires_at 与 aware expires_at 抛
    TypeError，被外层 except 吞掉 → 每次相同建议都新增行（现网去重失效）。
    """
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")

    # 第一次保存：由 save_suggestion 自己写入（expires_at 必为非空）
    assert _save_buy() is True
    db = mt_db()
    assert db.query(M.StockSuggestion).count() == 1
    first = db.query(M.StockSuggestion).first()
    first_expiry = first.expires_at
    assert first_expiry is not None
    db.close()

    # 窗口内同内容建议：必须命中去重合并，不新增行，且有效期被延长
    assert _save_buy() is True
    db = mt_db()
    rows = db.query(M.StockSuggestion).all()
    assert len(rows) == 1
    assert rows[0].expires_at >= first_expiry
    db.close()


def test_suggestion_pool_stability_with_naive_expires_at(mt_db, monkeypatch):
    """回归：降级稳定分支同样不得被 naive/aware 比较炸掉。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")

    # 先有一条更严重的建议（sell  rank=4）
    assert _save_buy(action="sell", action_label="卖出") is True
    # 窗口内降级建议（buy rank=2）→ 保持稳定，不新增行
    assert _save_buy() is True

    db = mt_db()
    rows = db.query(M.StockSuggestion).all()
    assert len(rows) == 1
    assert rows[0].action == "sell"  # 保持上一条更严重建议
    db.close()


# ---------------------------------------------------------------------------
# 4. TA：按调用传 key，不注进程 env
# ---------------------------------------------------------------------------


def test_ta_api_key_passed_per_call_not_env(monkeypatch):
    """风险 #20：ContextVar 按调用传 key；进程 env 全程不被注入。"""
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    from src.agents.tradingagents import llm_adapter

    assert llm_adapter.apply_ta_api_key_patch() is True
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    fake_self = SimpleNamespace(config={})
    with llm_adapter.ta_api_key_context("sk-per-call"):
        kwargs = TradingAgentsGraph._get_provider_kwargs(fake_self)
        assert kwargs["api_key"] == "sk-per-call"
        # env 在调用窗口内也不被注入
        assert os.environ.get("OPENROUTER_API_KEY") is None
        assert os.environ.get("OPENAI_API_KEY") is None
        assert os.environ.get("DEEPSEEK_API_KEY") is None

    # 上下文外不带 key，且 env 依旧干净
    assert "api_key" not in TradingAgentsGraph._get_provider_kwargs(fake_self)
    assert os.environ.get("OPENROUTER_API_KEY") is None


def test_ta_openrouter_client_builds_without_env(monkeypatch):
    """端到端：env 为空时 LLM client 仍拿到按调用传入的 key。"""
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    from src.agents.tradingagents import llm_adapter

    assert llm_adapter.apply_ta_api_key_patch() is True
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.llm_clients import create_llm_client

    fake_self = SimpleNamespace(config={"llm_provider": "openrouter"})
    with llm_adapter.ta_api_key_context("sk-xyz"):
        kwargs = TradingAgentsGraph._get_provider_kwargs(fake_self)
        client = create_llm_client(
            provider="openrouter",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            **kwargs,
        )
        llm = client.get_llm()

    secret = getattr(llm, "openai_api_key", None) or getattr(llm, "api_key", None)
    assert secret is not None
    value = getattr(secret, "get_secret_value", lambda: str(secret))()
    assert value == "sk-xyz"
    assert os.environ.get("OPENROUTER_API_KEY") is None


def test_ta_agent_no_longer_injects_env():
    """agent.py 生产链路不再调用 inject_api_key_env。"""
    from src.agents.tradingagents import agent as ta_agent

    assert "inject_api_key_env(" not in inspect.getsource(ta_agent)


# ---------------------------------------------------------------------------
# 5. TA 信号桥：tenant 归属
# ---------------------------------------------------------------------------


def test_ta_bridge_signal_tenant_attribution(mt_db, monkeypatch):
    """多租户：TA 写入的模拟盘信号归属当前 tenant_scope；单租户归 1。"""
    from src.agents.tradingagents.paper_trading_bridge import (
        maybe_emit_paper_trading_signal,
    )

    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    with tc.tenant_scope(2):
        ok = maybe_emit_paper_trading_signal(
            stock_symbol="600519", stock_market="CN", stock_name="贵州茅台",
            decision="buy", confidence=7.0, signal_text="s", reason="r",
            current_price=10.0, enabled=True,
        )
    assert ok is True
    db = mt_db()
    rows = db.query(M.StrategySignalRun).all()
    assert len(rows) == 1
    assert rows[0].tenant_id == 2
    assert rows[0].source_pool == "watchlist"
    db.close()

    # 单租户直通：无 ctx → 归属默认租户 1（与原 server_default 等价）
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    db = mt_db()
    db.query(M.StrategySignalRun).delete()
    db.commit()
    db.close()
    ok = maybe_emit_paper_trading_signal(
        stock_symbol="000001", stock_market="CN", stock_name="平安银行",
        decision="buy", confidence=7.0, signal_text="s", reason="r",
        current_price=10.0, enabled=True,
    )
    assert ok is True
    db = mt_db()
    row = db.query(M.StrategySignalRun).filter_by(stock_symbol="000001").one()
    assert row.tenant_id == 1
    db.close()


def test_build_ai_client_env_fallback_admin_tenant_only(monkeypatch):
    """F6（docs/23 P10，docs/26-J11 裁决）：env 凭证回退仅限管理员租户。

    普通租户可见集为空时不得回退 env（否则等于全员共享管理员 key，违背 T13）。
    """
    import server
    from src.web.tenant_context import DEFAULT_TENANT_ID

    monkeypatch.setenv("AI_API_KEY", "env-secret-key")  # Settings 读取兜底
    # 管理员租户（含单租户直通，tenant_id 恒为 1）：允许 env 回退
    c_admin = server._build_ai_client(None, None, "", tenant_id=DEFAULT_TENANT_ID)
    assert c_admin.api_key != "panwatch-unconfigured"
    # 普通租户：不回退 env，返回未配置占位客户端
    c_user = server._build_ai_client(None, None, "", tenant_id=2)
    assert c_user.api_key == "panwatch-unconfigured"
    assert c_user.base_url == ""
    # 显式解析到模型时不受影响
    svc = SimpleNamespace(base_url="https://x", api_key="k", name="s")
    mdl = SimpleNamespace(model="m")
    c_cfg = server._build_ai_client(mdl, svc, "", tenant_id=2)
    assert c_cfg.api_key == "k"
