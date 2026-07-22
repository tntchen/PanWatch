"""MT-P5-C 模块级缓存跨租户串扰断言（docs/20 M11 缓存泄漏穷举回销）。

穷举清单（grep `_CACHE|_cache|lru_cache|TTL` over src/）与定性：

A. 缓存 value 含租户私有数据 → key 必须含 tenant 维度（本文件行为断言）：
   1. src/web/api/accounts.py::_PORTFOLIO_RESULT_CACHE —— 组合基准/归因，
      value 由本租户持仓算出（私有）。key = `{tenant}:bench|attr:{days}:{bcode}:{sig}`。
      M11 指认的"双双空仓必撞"两条路径均封闭：
      a) 空仓 sig 为空 → 端点 early return，根本不触缓存；
      b) 双租户持仓指纹相同（最坏情况）→ key 前缀仍按租户隔离。
   2. src/web/api/insights.py::_ANN_CACHE —— AI 公告解读，消耗本租户配额/模型
      （T13），key = `{tenant}:{market}:{symbol}`。
   3. src/web/api/discovery.py::_cache —— 合成板块由本租户 watchlist 参与构建，
      `_scoped_key` 统一加租户前缀（读写两侧一处收口）。
   4. src/web/api/agents.py::_SCAN_CACHE —— 盘中扫描（含 AI 结果与盯盘清单），
      `_build_scan_cache_key` 含租户前缀。
   5. TA `_try_cache_hit` —— 非内存缓存，走 DB 表 analysis_history
      （TENANT_TABLES 成员），由 do_orm_execute 行级过滤兜底（M5 改判后
      唯一约束也带 tenant_id 前缀），按租户库行为断言。

B. 市场级行情缓存 → T7 设计意图共享，key 不含 tenant 属正确（豁免，docstring 记录）：
   - src/collectors/kline_collector.py::_KLINE_CACHE（K线，`market:symbol`）
   - src/collectors/capital_flow_collector.py::_FLOW_CACHE（资金流，`market:symbol`）
   - src/web/api/insights.py::_KLINE_CACHE（K线摘要，`market:symbol`）
   - src/web/api/accounts.py::_hkd_rate_cache/_usd_rate_cache（公开汇率）
   豁免理由：K线/资金流/汇率是交易所级公开数据，任何租户看到的值相同，
   共享缓存不泄露任何租户私有信息，反而避免多租户重复打爆上游数据源。

C. 非模块级/无租户语义的缓存（穷举登记，无需断言）：
   - signal_pack / context_builder / price_alert_engine 的 self._*_cache ——
     实例级，随对象生命周期消亡，非跨请求共享；
   - update_checker._CACHE（版本检查）、stock_list 缓存（股票清单）——
     实例级全局公共数据；
   - prediction_outcome / strategy_engine / entry_candidates 的 kline_cache ——
     函数局部 dict，非模块级。
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.web.tenant_context as tc
from src.web.database import Base
from src.web import models as M
from src.web.api import accounts, agents, discovery, insights
from src.collectors import capital_flow_collector as cfc
from src.collectors import kline_collector as kc
from src.core import analysis_history as ah


@pytest.fixture(autouse=True)
def _clean_module_caches(monkeypatch):
    """每个用例前后清空全部被测模块级缓存，防用例间互相污染。"""
    accounts._PORTFOLIO_RESULT_CACHE.clear()
    insights._ANN_CACHE.clear()
    discovery._cache.clear()
    with agents._SCAN_CACHE_LOCK:
        agents._SCAN_CACHE.clear()
    kc._KLINE_CACHE.clear()
    kc._FAIL_UNTIL.clear()
    cfc._FLOW_CACHE.clear()
    old_kline_summary_cache = insights.__dict__.get("_KLINE_CACHE")
    if old_kline_summary_cache is not None:
        old_kline_summary_cache.clear()
    old_hkd = dict(accounts._hkd_rate_cache)
    old_usd = dict(accounts._usd_rate_cache)
    yield
    accounts._PORTFOLIO_RESULT_CACHE.clear()
    insights._ANN_CACHE.clear()
    discovery._cache.clear()
    with agents._SCAN_CACHE_LOCK:
        agents._SCAN_CACHE.clear()
    kc._KLINE_CACHE.clear()
    kc._FAIL_UNTIL.clear()
    cfc._FLOW_CACHE.clear()
    ksc = insights.__dict__.get("_KLINE_CACHE")
    if ksc is not None:
        ksc.clear()
    accounts._hkd_rate_cache.update(old_hkd)
    accounts._usd_rate_cache.update(old_usd)


# ---------------------------------------------------------------------------
# 0. 租户前缀函数本身：跟随 tenant_scope，无 ctx 兜底 "0"
# ---------------------------------------------------------------------------


def test_tenant_cache_prefix_follows_scope():
    """四处 `_tenant_cache_prefix()` 实现一致：有 ctx 取 tenant_id，无 ctx 兜底 0。"""
    for mod in (accounts, insights, discovery, agents):
        with tc.tenant_scope(7):
            assert mod._tenant_cache_prefix() == "7"
        with tc.tenant_scope(8):
            assert mod._tenant_cache_prefix() == "8"
        assert mod._tenant_cache_prefix() == "0"  # 无 ctx（公开路由/裸脚本）


# ---------------------------------------------------------------------------
# 1. accounts._PORTFOLIO_RESULT_CACHE（M11 主指认）：私有 value → 租户隔离
# ---------------------------------------------------------------------------


def _patch_portfolio_deps(monkeypatch, sig: str):
    """把昂贵的持仓指纹/汇总/K线重建替换为可控替身（同一 sig 模拟 M11 最坏情况）。"""
    monkeypatch.setattr(accounts, "_holdings_signature", lambda db: sig)
    monkeypatch.setattr(accounts, "_gather_holdings", lambda db: [{"fake": True}])
    import src.core.portfolio_benchmark as pb

    def fake_benchmark(holdings, days, benchmark_code):
        # 用当前租户前缀做标记，模拟"按本租户持仓算出的结果"
        return {"owner_tenant": accounts._tenant_cache_prefix()}

    def fake_attribution(holdings, days, benchmark_code):
        return [{"symbol": "X", "owner_tenant": accounts._tenant_cache_prefix()}]

    monkeypatch.setattr(pb, "build_portfolio_benchmark", fake_benchmark)
    monkeypatch.setattr(pb, "build_attribution", fake_attribution)


def test_portfolio_benchmark_cache_isolated_across_tenants(monkeypatch):
    """双租户持仓指纹完全相同（M11 最坏情况）→ B 读不到 A 缓存的基准结果。"""
    _patch_portfolio_deps(monkeypatch, sig="1:100")

    with tc.tenant_scope(1):
        r_a1 = accounts.portfolio_benchmark(days=60, benchmark="000300", db=None)
    assert r_a1["owner_tenant"] == "1"

    with tc.tenant_scope(2):
        r_b = accounts.portfolio_benchmark(days=60, benchmark="000300", db=None)
    # 关键断言：同 days/benchmark/指纹，B 拿不到 A 的结果
    assert r_b["owner_tenant"] == "2"

    with tc.tenant_scope(1):
        r_a2 = accounts.portfolio_benchmark(days=60, benchmark="000300", db=None)
    # A 第二次命中自己的缓存（值仍是 A 的）
    assert r_a2["owner_tenant"] == "1"

    keys = list(accounts._PORTFOLIO_RESULT_CACHE._store.keys())
    assert any(k.startswith("1:bench:") for k in keys)
    assert any(k.startswith("2:bench:") for k in keys)


def test_portfolio_attribution_cache_isolated_across_tenants(monkeypatch):
    """归因缓存同样按租户前缀隔离。"""
    _patch_portfolio_deps(monkeypatch, sig="1:100")

    with tc.tenant_scope(1):
        r_a = accounts.portfolio_attribution(days=60, benchmark="000300", db=None)
    with tc.tenant_scope(2):
        r_b = accounts.portfolio_attribution(days=60, benchmark="000300", db=None)

    assert r_a["items"][0]["owner_tenant"] == "1"
    assert r_b["items"][0]["owner_tenant"] == "2"
    keys = list(accounts._PORTFOLIO_RESULT_CACHE._store.keys())
    assert any(k.startswith("1:attr:") for k in keys)
    assert any(k.startswith("2:attr:") for k in keys)


def test_portfolio_empty_holdings_never_touch_cache(monkeypatch):
    """M11 '双双空仓必撞'：空仓 sig 为空 → early return，不读不写缓存，无从碰撞。"""
    monkeypatch.setattr(accounts, "_holdings_signature", lambda db: "")

    import src.core.portfolio_benchmark as pb

    def _boom(*a, **kw):  # 若被调用说明逻辑错误
        raise AssertionError("空仓路径不应触发组合重建")

    monkeypatch.setattr(pb, "build_portfolio_benchmark", _boom)
    monkeypatch.setattr(pb, "build_attribution", _boom)

    with tc.tenant_scope(1):
        r1 = accounts.portfolio_benchmark(days=60, benchmark="000300", db=None)
        a1 = accounts.portfolio_attribution(days=60, benchmark="000300", db=None)
    with tc.tenant_scope(2):
        r2 = accounts.portfolio_benchmark(days=60, benchmark="000300", db=None)
        a2 = accounts.portfolio_attribution(days=60, benchmark="000300", db=None)

    assert r1 == r2 == {"empty": True, "reason": "no_holdings"}
    assert a1 == a2 == {"items": []}
    assert len(accounts._PORTFOLIO_RESULT_CACHE) == 0


# ---------------------------------------------------------------------------
# 2. insights._ANN_CACHE（M11 补指认）：AI 解读（租户配额 T13）→ 租户隔离
# ---------------------------------------------------------------------------


class _FakeDb:
    """stock 查不到（name=symbol）即可，无需真库。"""

    def query(self, *args):
        return self

    def filter(self, *args):
        return self

    def first(self):
        return None


def _patch_announcement_deps(monkeypatch):
    async def fake_fetch(symbol, name, limit=5):
        return [{"title": "测试公告", "time": "2025-01-01", "content": ""}]

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def chat(self, system_prompt, user_content, temperature=0.2):
            self.calls += 1
            return f"1|利好|租户{insights._tenant_cache_prefix()}解读"

    client = FakeClient()
    monkeypatch.setattr(insights, "_fetch_recent_announcements", fake_fetch)
    monkeypatch.setattr(insights, "_get_ai_client", lambda db, model_id: client)
    return client


def test_announcement_eval_cache_isolated_across_tenants(monkeypatch):
    """同标的同市场，A 的 AI 解读缓存不被 B 命中（配额/模型按租户，T13）。"""
    client = _patch_announcement_deps(monkeypatch)
    req = insights.AnnouncementEvalRequest(symbol="600519", market="CN")

    with tc.tenant_scope(1):
        r_a = asyncio.run(insights.announcement_eval(req, db=_FakeDb()))
    assert r_a["items"][0]["summary"] == "租户1解读"

    with tc.tenant_scope(2):
        r_b = asyncio.run(insights.announcement_eval(req, db=_FakeDb()))
    assert r_b["items"][0]["summary"] == "租户2解读"  # B 未命中 A 的缓存

    with tc.tenant_scope(1):
        r_a2 = asyncio.run(insights.announcement_eval(req, db=_FakeDb()))
    assert r_a2["items"][0]["summary"] == "租户1解读"  # A 命中自己的缓存
    assert client.calls == 2  # 每租户各调一次 AI，没有第三次

    keys = list(insights._ANN_CACHE._store.keys())
    assert "1:CN:600519" in keys
    assert "2:CN:600519" in keys


def test_announcement_eval_no_ctx_fallback_key():
    """无租户 ctx（公开路由兜底）key 前缀为 '0'，与任一真实租户都不冲突。"""
    with tc.tenant_scope(3):
        assert insights._tenant_cache_prefix() == "3"
    assert insights._tenant_cache_prefix() == "0"


# ---------------------------------------------------------------------------
# 3. discovery._cache：合成板块含本租户 watchlist → _scoped_key 租户隔离
# ---------------------------------------------------------------------------


def test_discovery_cache_scoped_per_tenant():
    """A 写入的合成板块缓存，B 用同名 key 读不到。"""
    key = "boards:CN:gainers:12"
    with tc.tenant_scope(1):
        assert discovery._scoped_key(key).startswith("1:")
        discovery._cache_set(key, {"boards": ["租户1合成板块"]})

    with tc.tenant_scope(2):
        assert discovery._scoped_key(key).startswith("2:")
        assert discovery._cache_get(key, ttl_s=60) is None  # B 读不到 A

    with tc.tenant_scope(1):
        assert discovery._cache_get(key, ttl_s=60) == {"boards": ["租户1合成板块"]}

    assert set(discovery._cache.keys()) == {"1:boards:CN:gainers:12"}


# ---------------------------------------------------------------------------
# 4. agents._SCAN_CACHE：盘中扫描（盯盘清单 + AI 结果）→ 租户隔离
# ---------------------------------------------------------------------------


def _watchlist_item(symbol="600519", market="CN"):
    return SimpleNamespace(market=SimpleNamespace(value=market), symbol=symbol)


def test_agents_scan_cache_scoped_per_tenant():
    """同一盯盘清单，扫描缓存 key 按租户区分，B 读不到 A 的扫描结果。"""
    wl = [_watchlist_item()]
    with tc.tenant_scope(1):
        key_a = agents._build_scan_cache_key(False, wl)
        agents._set_scan_cache(key_a, {"scan": "租户1结果"})
    with tc.tenant_scope(2):
        key_b = agents._build_scan_cache_key(False, wl)
        assert key_a != key_b
        assert key_a.startswith("1:intraday_scan:")
        assert key_b.startswith("2:intraday_scan:")
        assert agents._get_scan_cache(key_b, False) is None  # B 不命中 A
    with tc.tenant_scope(1):
        assert agents._get_scan_cache(key_a, False) == {"scan": "租户1结果"}


# ---------------------------------------------------------------------------
# 5. TA 结果缓存 = DB 表 analysis_history（TENANT_TABLE）→ 行级过滤兜底
# ---------------------------------------------------------------------------


@pytest.fixture
def mt_analysis_db(monkeypatch):
    """多租户模式 + 内存库 + do_orm_execute 过滤，替换 analysis_history.SessionLocal。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
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
    monkeypatch.setattr(ah, "SessionLocal", Session)
    yield Session
    tc._tenant_column_cache.clear()
    tc._tenant_column_cache.update(old_cache)


def test_ta_analysis_cache_rows_isolated_by_orm_filter(mt_analysis_db):
    """同 agent/标的/日期，A 租户的分析结果不会成为 B 租户的缓存命中。"""
    Session = mt_analysis_db
    today = date.today().strftime("%Y-%m-%d")
    s = Session()
    s.add_all(
        [
            M.AnalysisHistory(
                tenant_id=1,
                agent_name="tradingagents",
                stock_symbol="600519",
                analysis_date=today,
                content="租户1的AI分析",
                raw_data={"owner": 1},
            ),
            M.AnalysisHistory(
                tenant_id=2,
                agent_name="tradingagents",
                stock_symbol="600519",
                analysis_date=today,
                content="租户2的AI分析",
                raw_data={"owner": 2},
            ),
        ]
    )
    s.commit()
    s.close()

    with tc.tenant_scope(2):
        hit_b = ah.get_analysis("tradingagents", "600519", date.today())
        assert hit_b is not None and hit_b.content == "租户2的AI分析"
    with tc.tenant_scope(1):
        hit_a = ah.get_analysis("tradingagents", "600519", date.today())
        assert hit_a is not None and hit_a.content == "租户1的AI分析"

    # 仅租户 1 有记录时，租户 3 不得命中（TA _try_cache_hit 的上游）
    with tc.tenant_scope(3):
        assert ah.get_analysis("tradingagents", "600519", date.today()) is None


# ---------------------------------------------------------------------------
# 6. 市场级行情缓存：T7 设计共享豁免（key 无 tenant 维度属正确，docstring 记录）
# ---------------------------------------------------------------------------


def _fake_klines(n=5):
    return [
        kc.KlineData(
            date=f"2025-01-0{i}", open=1.0, close=1.1, high=1.2, low=0.9, volume=100.0
        )
        for i in range(1, n + 1)
    ]


def test_kline_collector_cache_shared_by_design(monkeypatch):
    """豁免（T7）：K线是交易所公开数据，任何租户值相同。

    断言 1：缓存 key 为 `market:symbol`，无 tenant 维度——这是设计意图而非泄漏，
    因为 value（OHLCV）不含任何租户私有信息；
    断言 2：跨租户共享命中使上游只被拉取一次（多租户不打爆数据源）。
    """
    fetch_calls = {"n": 0}

    def fake_fetch(self, symbol, days):
        fetch_calls["n"] += 1
        return _fake_klines(5)

    monkeypatch.setattr(kc.KlineCollector, "_fetch_all_sources", fake_fetch)

    with tc.tenant_scope(1):
        bars_a = kc.KlineCollector(kc.MarketCode.CN).get_klines("600519", days=3)
    with tc.tenant_scope(2):
        bars_b = kc.KlineCollector(kc.MarketCode.CN).get_klines("600519", days=3)

    assert fetch_calls["n"] == 1  # B 命中 A 写入的市场级缓存，未重复联网
    assert [b.date for b in bars_a] == [b.date for b in bars_b]
    assert set(kc._KLINE_CACHE.keys()) == {"CN:600519"}  # key 无租户维度


def test_capital_flow_cache_shared_by_design(monkeypatch):
    """豁免（T7）：资金流向为市场公开数据，key `market:symbol` 无 tenant 维度。"""
    md_cf = SimpleNamespace(
        symbol="600519",
        name="贵州茅台",
        main_net_inflow=1.0,
        main_net_inflow_pct=0.1,
        super_net_inflow=0.5,
        big_net_inflow=0.5,
        mid_net_inflow=0.0,
        small_net_inflow=-1.0,
        main_net_5d=2.0,
    )
    monkeypatch.setattr(
        cfc, "get_market_data", lambda: SimpleNamespace(capital_flow=lambda s, market: md_cf)
    )

    with tc.tenant_scope(1):
        flow_a = cfc.CapitalFlowCollector(cfc.MarketCode.CN).get_capital_flow("600519")
    with tc.tenant_scope(2):
        flow_b = cfc.CapitalFlowCollector(cfc.MarketCode.CN).get_capital_flow("600519")

    assert flow_a is flow_b  # 同一缓存对象：公开数据共享
    assert set(cfc._FLOW_CACHE._store.keys()) == {"CN:600519"}


def test_insights_kline_summary_cache_shared_by_design(monkeypatch):
    """豁免（T7）：insights 批量接口的 K线摘要缓存 key 为 `market:symbol`，
    摘要值是公开行情衍生（涨跌幅/均线等），无租户私有性，跨租户共享属设计。"""
    monkeypatch.setattr(insights, "md_quote_rows", lambda symbols, market: [])
    monkeypatch.setattr(
        insights,
        "KlineCollector",
        lambda market: SimpleNamespace(
            get_kline_summary=lambda symbol: {"summary": "公开行情摘要"}
        ),
    )
    monkeypatch.setattr(
        insights, "get_latest_suggestions", lambda stock_keys, include_expired: {}
    )

    payload = insights.InsightsBatchRequest(
        items=[insights.InsightItem(symbol="600519", market="CN")]
    )
    with tc.tenant_scope(1):
        r_a = insights.insights_batch(payload)
    with tc.tenant_scope(2):
        r_b = insights.insights_batch(payload)

    assert r_a[0]["kline_summary"] == r_b[0]["kline_summary"] == {
        "summary": "公开行情摘要"
    }
    kline_cache = insights.__dict__["_KLINE_CACHE"]
    assert set(kline_cache.keys()) == {"CN:600519"}  # 无租户维度，且未因双租户分裂


def test_fx_rate_caches_global_public_data():
    """豁免：港币/美元汇率为公开宏观数据，模块级全局缓存无租户语义。"""
    accounts._hkd_rate_cache.update({"rate": 0.9123, "ts": time.time()})
    with tc.tenant_scope(1):
        r1 = accounts.get_hkd_cny_rate()
    with tc.tenant_scope(2):
        r2 = accounts.get_hkd_cny_rate()
    assert r1 == r2 == 0.9123
