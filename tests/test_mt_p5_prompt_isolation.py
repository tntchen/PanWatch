"""MT-P5（docs/20 M8）：LLM prompt 跨租户内容级断言 —— 直接验证 C2 修复。

最高价值用例：用内存库种双租户互斥标记（自选股/持仓/playbook/快照记忆/历史
新闻），驱动 ContextBuilder 全链路组装 + DailyReportAgent.build_prompt 拼装，
再以 recorder 替换 AIClient.chat 捕获最终发给 LLM 的 prompt，断言：

- 租户 B 的 prompt 不出现租户 A 的任何标记（symbol/股数/成本/playbook 文本/
  快照密语/历史新闻标题/可用资金）；
- 租户 B 的 prompt 确实包含 B 自己的标记（防空 prompt 假阳性）；
- 反向对 A 同样成立；
- 快照记忆回读（context_store）与 playbook 解析（Stock 作用域）按租户隔离。

红线遵守：
- 不触碰任何 prompt 快照哈希（本文件只新增用例，不改产品代码/prompt 模板）；
- 外部数据（K线/指数/公告全文/TA verdict）全部打桩，零网络请求。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.agents.base import AccountInfo, AgentContext, PortfolioInfo, PositionInfo
from src.agents.daily_report import DailyReportAgent
from src.config import StockConfig
from src.core import context_builder as cb_mod
from src.core import context_store
from src.core.context_builder import ContextBuilder
from src.models.market import MarketCode
from src.web import models as M
from src.web.database import Base
from src.web.tenant_context import (
    refresh_tenant_column_cache,
    tenant_scope,
)
import src.web.tenant_context as tc

# ---------------------------------------------------------------------------
# 双租户互斥标记（逐一核对无公共子串）
# ---------------------------------------------------------------------------

MARKERS_A = {
    "symbol": "688111",
    "name": "甲股标记",
    "position": "2150股",
    "cost": "112.57",
    "funds": "654321",
    "playbook": "盘古开天甲策略",
    "snapshot": "甲股快照密语七二",
    "news": "甲股昨夜急电",
}
MARKERS_B = {
    "symbol": "688222",
    "name": "乙股标记",
    "position": "999股",
    "cost": "7.31",
    "funds": "88888",
    "playbook": "后羿射日乙策略",
    "snapshot": "乙股快照密语五三",
    "news": "乙股昨夜急电",
}

_TENANT_A = 1
_TENANT_B = 2


class _PromptRecorder:
    """AIClient.chat 替身：记录每次调用的 (system_prompt, user_content)。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, system_prompt: str, user_content: str, **_kw) -> str:
        self.calls.append({"system": system_prompt or "", "user": user_content or ""})
        return "<<recorded>>"

    @property
    def full_text(self) -> str:
        return "\n".join(c["system"] + "\n" + c["user"] for c in self.calls)


@pytest.fixture
def prompt_mt_env(monkeypatch):
    """内存库 + MT 模式（do_orm_execute 自动过滤激活）+ 双租户种子数据。

    所有外部数据入口打桩；SessionLocal 全量替换为内存 Session。
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    event.listen(Session, "do_orm_execute", tc.apply_tenant_filter)
    old_cache = dict(tc._tenant_column_cache)
    refresh_tenant_column_cache(engine)

    # 多租户模式：自动过滤机制点激活（显式过滤之外的双保险）
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")

    # SessionLocal 替换：builder / context_store / agent 三处
    monkeypatch.setattr(cb_mod, "SessionLocal", Session)
    monkeypatch.setattr(context_store, "SessionLocal", Session)
    monkeypatch.setattr("src.agents.daily_report.SessionLocal", Session)

    # 外部数据全部打桩（零网络）
    monkeypatch.setattr(
        cb_mod, "build_kline_history_context",
        lambda **_kw: {"available": False},
    )
    monkeypatch.setattr(
        ContextBuilder, "_fetch_index_context",
        lambda self, symbol, market: {"available": False},
    )
    monkeypatch.setattr(cb_mod, "fetch_announcement_fulltext", lambda *_a, **_k: "")
    monkeypatch.setattr(cb_mod, "get_latest_ta_verdict", lambda *_a, **_k: None)

    today = date.today().strftime("%Y-%m-%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    db = Session()
    try:
        for tid, mk in ((_TENANT_A, MARKERS_A), (_TENANT_B, MARKERS_B)):
            stock = M.Stock(
                tenant_id=tid, symbol=mk["symbol"], name=mk["name"], market="CN"
            )
            db.add(stock)
            db.flush()
            db.add(
                M.StockPlaybook(
                    tenant_id=tid,
                    stock_id=stock.id,
                    version=1,
                    is_active=True,
                    payload={
                        "schema_version": 1,
                        "meta": {"name": mk["playbook"], "strategy_mode": "波段"},
                        "price_levels": [{"label": "止损", "value": 1.0}],
                    },
                )
            )
            db.add(
                M.AnalysisHistory(
                    tenant_id=tid,
                    agent_name="daily_report",
                    stock_symbol="*",
                    analysis_date=today,
                    title="t",
                    content="c",
                    raw_data={
                        "news": [
                            {
                                "source": "eastmoney",
                                "external_id": f"ex-{tid}",
                                "title": f"{mk['symbol']}{mk['news']}",
                                "content": f"{mk['news']}正文细节",
                                "publish_time": now,
                                "importance": 3,
                                "symbols": [mk["symbol"]],
                                "url": "",
                            }
                        ]
                    },
                )
            )
        db.commit()
    finally:
        db.close()

    # 快照记忆（context_snapshots）：种今天日期 + 本 agent 的 context_type，
    # 确保构建时 memory 回读命中（读取发生在本日快照覆写之前）。
    for tid, mk in ((_TENANT_A, MARKERS_A), (_TENANT_B, MARKERS_B)):
        assert context_store.save_stock_context_snapshot(
            symbol=mk["symbol"],
            market="CN",
            snapshot_date=today,
            context_type="daily_report",
            payload={
                "news": {"history_topic": {"summary": mk["snapshot"]}},
                "kline_history": {},
            },
            quality={"score": 90},
            tenant_id=tid,
        )

    try:
        yield SimpleNamespace(Session=Session)
    finally:
        tc._tenant_column_cache.clear()
        tc._tenant_column_cache.update(old_cache)
        engine.dispose()


def _make_context(tid: int, mk: dict) -> AgentContext:
    """构造某租户的 AgentContext（watchlist + portfolio 均为该租户私有数据）。"""
    qty = 2150 if tid == _TENANT_A else 999
    cost = 112.572 if tid == _TENANT_A else 7.31
    funds = 654321.0 if tid == _TENANT_A else 88888.0
    portfolio = PortfolioInfo(
        accounts=[
            AccountInfo(
                id=tid,
                name=f"账户{tid}",
                available_funds=funds,
                positions=[
                    PositionInfo(
                        account_id=tid,
                        account_name=f"账户{tid}",
                        stock_id=tid,
                        symbol=mk["symbol"],
                        name=mk["name"],
                        market=MarketCode.CN,
                        cost_price=cost,
                        quantity=qty,
                    )
                ],
            )
        ]
    )
    return AgentContext(
        ai_client=_PromptRecorder(),
        notifier=None,
        config=SimpleNamespace(
            watchlist=[
                StockConfig(symbol=mk["symbol"], name=mk["name"], market=MarketCode.CN)
            ]
        ),
        portfolio=portfolio,
        tenant_id=tid,
    )


def _empty_pack():
    return SimpleNamespace(
        quote=None,
        technical={"error": "无技术指标数据"},
        capital_flow={"error": "无资金流数据"},
        news=SimpleNamespace(items=[]),
        events=SimpleNamespace(items=[]),
        position=None,
    )


def _capture_prompt(prompt_mt_env, tid: int, mk: dict) -> _PromptRecorder:
    """走 builder 全链路 + build_prompt，用 recorder 捕获发给 LLM 的 prompt。"""
    agent_ctx = _make_context(tid, mk)
    recorder: _PromptRecorder = agent_ctx.ai_client
    agent = DailyReportAgent()
    pack = _empty_pack()

    async def _drive():
        with tenant_scope(tid):
            builder = ContextBuilder()
            ctx_pack = await builder.build_symbol_contexts(
                agent_name="daily_report",
                context=agent_ctx,
                packs={mk["symbol"]: pack},
                realtime_hours=24,
                extended_hours=72,
                history_days=30,
                kline_days=120,
                persist_snapshot=True,
                tenant_id=tid,
            )
            symbol_contexts = ctx_pack["symbols"]
            playbook_sections = agent._build_playbook_sections(
                agent_ctx, {mk["symbol"]: pack}, symbol_contexts
            )
            data = {
                "indices": [],
                "signal_packs": {mk["symbol"]: pack},
                "symbol_contexts": symbol_contexts,
                "quality_overview": ctx_pack["quality_overview"],
                "playbook_sections": playbook_sections,
                "dragon_tiger": [],
            }
            system_prompt, user_content = agent.build_prompt(data, agent_ctx)
            await agent_ctx.ai_client.chat(system_prompt, user_content)

    asyncio.run(_drive())
    return recorder


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def test_tenant_b_prompt_excludes_all_tenant_a_markers(prompt_mt_env):
    """租户B的prompt不含A的任何标记，且确实包含B自己的标记"""
    rec = _capture_prompt(prompt_mt_env, _TENANT_B, MARKERS_B)
    # 防空调用/空 prompt 假阳性：确实发生了一次 LLM 调用且内容非平凡
    assert len(rec.calls) == 1
    assert len(rec.calls[0]["user"]) > 200
    assert "## 自选股详情" in rec.calls[0]["user"]

    text = rec.full_text
    # B 自己的标记全部在场（symbol/名称/持仓/成本/资金/playbook/快照/历史新闻）
    for key, marker in MARKERS_B.items():
        assert marker in text, f"B 的 prompt 缺少 B 自己的标记 {key}={marker!r}"
    # A 的任何标记一律不得出现
    for key, marker in MARKERS_A.items():
        assert marker not in text, f"跨租户泄漏：B 的 prompt 出现 A 的标记 {key}={marker!r}"


def test_tenant_a_prompt_excludes_all_tenant_b_markers(prompt_mt_env):
    """租户A的prompt不含B的任何标记，且确实包含A自己的标记"""
    rec = _capture_prompt(prompt_mt_env, _TENANT_A, MARKERS_A)
    assert len(rec.calls) == 1
    assert len(rec.calls[0]["user"]) > 200

    text = rec.full_text
    for key, marker in MARKERS_A.items():
        assert marker in text, f"A 的 prompt 缺少 A 自己的标记 {key}={marker!r}"
    for key, marker in MARKERS_B.items():
        assert marker not in text, f"跨租户泄漏：A 的 prompt 出现 B 的标记 {key}={marker!r}"


def test_prompt_isolation_symmetric_in_one_session(prompt_mt_env):
    """同库先后构建 A、B 两次 prompt（含快照回写），交叉断言互不泄漏"""
    rec_a = _capture_prompt(prompt_mt_env, _TENANT_A, MARKERS_A)
    rec_b = _capture_prompt(prompt_mt_env, _TENANT_B, MARKERS_B)
    text_a, text_b = rec_a.full_text, rec_b.full_text
    for key, marker in MARKERS_A.items():
        assert marker not in text_b, f"先建A后建B：B 的 prompt 出现 A 标记 {key}={marker!r}"
    for key, marker in MARKERS_B.items():
        assert marker not in text_a, f"先建A后建B：A 的 prompt 出现 B 标记 {key}={marker!r}"


def test_snapshot_memory_readback_isolated(prompt_mt_env):
    """快照记忆回读隔离：A 写入的快照，B 的 builder 回读不得命中（反之亦然）"""
    # B 视角读 A 的股票快照 → 空；读自己的 → 命中 B 标记
    mem_b_on_a = ContextBuilder._build_snapshot_memory(
        symbol=MARKERS_A["symbol"], market=MarketCode.CN,
        context_type="daily_report", tenant_id=_TENANT_B,
    )
    assert mem_b_on_a == {}, f"B 不应回读到 A 的快照记忆: {mem_b_on_a}"
    mem_b = ContextBuilder._build_snapshot_memory(
        symbol=MARKERS_B["symbol"], market=MarketCode.CN,
        context_type="daily_report", tenant_id=_TENANT_B,
    )
    assert mem_b.get("latest_history_topic") == MARKERS_B["snapshot"]
    # A 视角镜像断言
    mem_a_on_b = ContextBuilder._build_snapshot_memory(
        symbol=MARKERS_B["symbol"], market=MarketCode.CN,
        context_type="daily_report", tenant_id=_TENANT_A,
    )
    assert mem_a_on_b == {}, f"A 不应回读到 B 的快照记忆: {mem_a_on_b}"
    # 不显式传 tenant_id 时走 tenant_scope ctx 解析，结果一致
    with tenant_scope(_TENANT_B):
        mem_ctx = ContextBuilder._build_snapshot_memory(
            symbol=MARKERS_B["symbol"], market=MarketCode.CN,
            context_type="daily_report",
        )
    assert mem_ctx.get("latest_history_topic") == MARKERS_B["snapshot"]


def test_playbook_resolution_scoped_to_tenant(prompt_mt_env):
    """playbook 摘要注入按租户解析：B 解析 A 的股票得 None，解析自己得 B 文本"""
    # B 作用域解析 A 的股票（Stock 双保险：显式 tenant 过滤 + 自动过滤）
    with tenant_scope(_TENANT_B):
        summary_cross = ContextBuilder._load_playbook_summary(
            MARKERS_A["symbol"], MarketCode.CN
        )
        summary_own = ContextBuilder._load_playbook_summary(
            MARKERS_B["symbol"], MarketCode.CN
        )
    assert summary_cross is None, f"B 不应解析到 A 的 playbook: {summary_cross!r}"
    assert summary_own is not None and MARKERS_B["playbook"] in summary_own
    assert MARKERS_A["playbook"] not in (summary_own or "")
    # A 作用域镜像
    with tenant_scope(_TENANT_A):
        summary_a = ContextBuilder._load_playbook_summary(
            MARKERS_A["symbol"], MarketCode.CN
        )
    assert summary_a is not None and MARKERS_A["playbook"] in summary_a
    assert MARKERS_B["playbook"] not in summary_a


def test_history_news_readback_isolated(prompt_mt_env):
    """历史新闻（analysis_history）回读按租户隔离：B 读不到 A 的新闻标记"""
    rows_b = ContextBuilder._load_history_news(
        MARKERS_B["symbol"], MARKERS_B["name"], days=30, tenant_id=_TENANT_B
    )
    titles_b = " ".join(str(r.get("title") or "") for r in rows_b)
    assert MARKERS_B["news"] in titles_b
    assert MARKERS_A["news"] not in titles_b
    assert MARKERS_A["symbol"] not in titles_b
    # B 视角直接查 A 的股票 → 查不到任何行
    rows_cross = ContextBuilder._load_history_news(
        MARKERS_A["symbol"], MARKERS_A["name"], days=30, tenant_id=_TENANT_B
    )
    assert rows_cross == [], f"B 不应回读到 A 的历史新闻: {rows_cross}"

