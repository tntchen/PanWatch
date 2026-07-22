"""MT-P3 事件门分层 + 盘中监测租户穿透验收测试（docs/23 §3/§4、docs/17 R3/T7）。

覆盖（T7 验收口径）：
1. 状态文件 schema_version=2 分层：市场观测态（last_price/tech_sig/观测记录）
   收 ``market.{symbol}`` 单份；pb_fired 冷却收 ``tenants.{t}.{symbol}``；
2. 双租户同一标的、相同冷却键字符串（方向:位名@价位），冷却互不影响；
3. v1→v2 旧文件一次性幂等迁移（原子写 + .v1.bak 备份，重入跳过）；
4. _load_playbook_levels 按 tenant 过滤 playbook（机制点 tenant_scope）；
5. morning_brief / daily_report 的 symbol 解析点在 tenant_scope 下自动过滤；
6. intraday 个股节流键 ``{tenant}:{symbol}``，跨租户互不吞节流；
7. 默认 tenant_id=1 + PANWATCH_SINGLE_TENANT='1' 行为与改造前等价。

一律内存 sqlite + 临时 DATA_DIR，不碰真实库、不做网络调用。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.agents.intraday_monitor import IntradayMonitorAgent
from src.core import intraday_event_gate as gate
from src.core.intraday_event_gate import (
    PlaybookLevel,
    check_and_update,
    migrate_state_file_to_v2,
)
from src.models.market import MarketCode
from src.web import models as M
from src.web.database import Base
from src.web.tenant_context import (
    apply_tenant_filter,
    refresh_tenant_column_cache,
    tenant_scope,
)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@pytest.fixture
def gate_state_dir(tmp_path, monkeypatch):
    """事件门状态重定向到临时目录（不碰真实 DATA_DIR）。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return tmp_path


def _state_file(tmp_path) -> str:
    return str(tmp_path / "state" / "intraday_monitor_state.json")


def _read_state(tmp_path) -> dict:
    with open(_state_file(tmp_path), encoding="utf-8") as f:
        return json.load(f)


def _gate(symbol: str, tenant_id: int = 1, **kwargs):
    defaults = dict(
        change_pct=0.5,
        volume_ratio=1.0,
        kline_summary=None,
        price_threshold=3.0,
        volume_threshold=2.0,
    )
    defaults.update(kwargs)
    return check_and_update(symbol=symbol, tenant_id=tenant_id, **defaults)


@pytest.fixture
def mt_db(monkeypatch):
    """多租户模式内存库：挂 do_orm_execute 机制点 + 反射缓存 + 关直通。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    event.listen(Session, "do_orm_execute", apply_tenant_filter)
    monkeypatch.setattr("src.web.database.SessionLocal", Session)
    refresh_tenant_column_cache(engine)
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    try:
        yield engine, Session
    finally:
        engine.dispose()


def _seed_stock_with_playbook(
    db, *, tenant_id: int, symbol: str, defense_price: int
):
    """造 (tenant, symbol) 股票 + 激活 playbook（防线 <defense_price）。"""
    stock = M.Stock(
        tenant_id=tenant_id, symbol=symbol, name=f"测试股{tenant_id}", market="CN"
    )
    db.add(stock)
    db.flush()
    payload = {
        "schema_version": 1,
        "meta": {"name": f"方案T{tenant_id}", "version_label": "v1"},
        "defense": {"rule": f"连续2日收盘<{defense_price}", "action": "减仓"},
    }
    playbook = M.StockPlaybook(
        tenant_id=tenant_id,
        stock_id=stock.id,
        version=1,
        is_active=True,
        payload=payload,
        summary="",
        note="",
    )
    db.add(playbook)
    db.commit()
    return stock, playbook


# --------------------------------------------------------------------------- #
# ① 状态分层与冷却隔离（T7 核心验收）
# --------------------------------------------------------------------------- #


def test_two_tenants_same_cool_key_isolated(gate_state_dir):
    """两租户同标的、同一冷却键字符串（下穿:防线@98），冷却互不影响。

    共享行情基线下的价格流（单 job 串行扇出语义，docs/23 §4.5）：
    t1@110 建基线 → t2@110 同价不判 → t1@97 下穿（t1 记录冷却）
    → t2@110 上穿 → t2@97 下穿：与 t1 冷却键字符串完全相同，但必须照常触发
    → t1@110 上穿触发（t1 尚无此键）→ t1@97 下穿被 t1 自己的冷却吞掉
    → t2@110 上穿被 t2 自己的冷却吞掉。
    """
    levels = [PlaybookLevel("防线", 98.0)]

    assert _gate("MT1", tenant_id=1, current_price=110.0, playbook_levels=levels).reasons == []
    assert _gate("MT1", tenant_id=2, current_price=110.0, playbook_levels=levels).reasons == []

    d = _gate("MT1", tenant_id=1, current_price=97.0, playbook_levels=levels)
    assert d.reasons == ["playbook_cross:下穿防线@98"]

    d = _gate("MT1", tenant_id=2, current_price=110.0, playbook_levels=levels)
    assert d.reasons == ["playbook_cross:上穿防线@98"]

    # 与租户 1 完全相同的冷却键字符串，仍在 1800s 冷却期内——但租户 2 必须触发
    d = _gate("MT1", tenant_id=2, current_price=97.0, playbook_levels=levels)
    assert d.reasons == ["playbook_cross:下穿防线@98"]

    d = _gate("MT1", tenant_id=1, current_price=110.0, playbook_levels=levels)
    assert d.reasons == ["playbook_cross:上穿防线@98"]

    # 各租户自己的冷却仍然生效
    assert _gate("MT1", tenant_id=1, current_price=97.0, playbook_levels=levels).reasons == []
    assert _gate("MT1", tenant_id=2, current_price=110.0, playbook_levels=levels).reasons == []

    # 落盘结构：冷却按租户分节，键字符串一致但互不影响
    state = _read_state(gate_state_dir)
    fired_t1 = state["tenants"]["1"]["MT1"]["pb_fired"]
    fired_t2 = state["tenants"]["2"]["MT1"]["pb_fired"]
    assert set(fired_t1) == {"下穿:防线@98", "上穿:防线@98"}
    assert set(fired_t2) == {"下穿:防线@98", "上穿:防线@98"}


def test_two_tenants_different_playbook_levels(gate_state_dir):
    """两租户同标的不同 playbook 价位：各自穿越各自报，互不干扰。"""
    lv_t1 = [PlaybookLevel("防线", 98.0)]
    lv_t2 = [PlaybookLevel("防线", 100.0)]

    _gate("MT2", tenant_id=1, current_price=110.0, playbook_levels=lv_t1)
    _gate("MT2", tenant_id=2, current_price=110.0, playbook_levels=lv_t2)

    # 110 → 99：只穿越租户 2 的 100，不穿越租户 1 的 98
    d2 = _gate("MT2", tenant_id=2, current_price=99.0, playbook_levels=lv_t2)
    assert d2.reasons == ["playbook_cross:下穿防线@100"]
    d1 = _gate("MT2", tenant_id=1, current_price=99.0, playbook_levels=lv_t1)
    assert d1.reasons == []

    # 99 → 97：穿越租户 1 的 98
    d1 = _gate("MT2", tenant_id=1, current_price=97.0, playbook_levels=lv_t1)
    assert d1.reasons == ["playbook_cross:下穿防线@98"]
    d2 = _gate("MT2", tenant_id=2, current_price=97.0, playbook_levels=lv_t2)
    assert d2.reasons == []


def test_market_observation_state_single_copy(gate_state_dir):
    """市场观测态全租户共享单份：last_price 基线 / tech_sig 不按租户复制。"""
    levels = [PlaybookLevel("防线", 98.0)]
    sig_a = {"trend": "up"}
    sig_b = {"trend": "down"}

    _gate("MT3", tenant_id=1, current_price=110.0, kline_summary=sig_a)
    # 租户 2 首次观测即共享租户 1 写入的市场基线：110→105 无穿越，
    # 且技术态相对共享 tech_sig 变更 → tech_state_changed（市场级语义）
    d = _gate("MT3", tenant_id=2, current_price=105.0, kline_summary=sig_b,
              playbook_levels=levels)
    assert "tech_state_changed" in d.reasons
    assert not any(r.startswith("playbook_cross") for r in d.reasons)

    state = _read_state(gate_state_dir)
    assert state["version"] == 2
    market_rec = state["market"]["MT3"]
    # 单份观测态，不含任何租户冷却键
    assert market_rec["last_price"] == 105.0
    assert market_rec["tech_sig"] is not None
    assert "pb_fired" not in market_rec
    # market 节每标的一条记录，不按租户复制
    assert list(state["market"].keys()) == ["MT3"]


def test_default_tenant_state_goes_to_tenant_1(gate_state_dir):
    """默认 tenant_id=1（单租户等价）：冷却落 tenants."1"，判定语义与 v1 一致。"""
    levels = [PlaybookLevel("做T卖出区下沿", 125.0)]
    assert _gate("MT4", current_price=120.0, playbook_levels=levels).reasons == []
    d = _gate("MT4", current_price=126.0, playbook_levels=levels)
    assert d.reasons == ["playbook_cross:上穿做T卖出区下沿@125"]
    # 冷却期内重复穿越被吞（与 v1 行为一致）
    assert _gate("MT4", current_price=124.0, playbook_levels=levels).reasons == [
        "playbook_cross:下穿做T卖出区下沿@125"
    ]
    assert _gate("MT4", current_price=126.0, playbook_levels=levels).reasons == []

    state = _read_state(gate_state_dir)
    assert set(state["tenants"].keys()) == {"1"}


# --------------------------------------------------------------------------- #
# ② v1 → v2 一次性幂等迁移
# --------------------------------------------------------------------------- #


_V1_STATE = {
    "688110": {
        "last_price": 112.57,
        "tech_sig": {"trend": "up"},
        "pb_fired": {"上穿:防线@100": "2026-07-22T01:00:00+00:00"},
        "last_seen_at": "2026-07-22T01:00:00+00:00",
        "change_pct": 1.2,
        "volume_ratio": 2.3,
    },
    "00700": {
        "last_seen_at": "2026-07-22T01:00:00+00:00",
        "change_pct": None,
        "volume_ratio": None,
        "tech_sig": {"trend": "flat"},
    },
}


def test_v1_to_v2_migration_lazy_on_read(gate_state_dir):
    """v1 旧文件首次读取时惰性迁移：行情字段归 market，pb_fired 归租户 1。"""
    import os

    os.makedirs(os.path.dirname(_state_file(gate_state_dir)), exist_ok=True)
    with open(_state_file(gate_state_dir), "w", encoding="utf-8") as f:
        json.dump(_V1_STATE, f)

    # 显式迁移完成结构转换
    assert migrate_state_file_to_v2(_state_file(gate_state_dir)) is True

    state = _read_state(gate_state_dir)
    assert state["version"] == 2
    # 行情观测态归 market 节
    assert state["market"]["688110"]["last_price"] == 112.57
    assert state["market"]["688110"]["tech_sig"] == {"trend": "up"}
    assert "pb_fired" not in state["market"]["688110"]
    # pb_fired 归默认租户 1
    assert state["tenants"]["1"]["688110"]["pb_fired"] == {
        "上穿:防线@100": "2026-07-22T01:00:00+00:00"
    }
    # 无 pb_fired 的标的不产生租户节
    assert "00700" not in state["tenants"]["1"]
    assert state["market"]["00700"]["tech_sig"] == {"trend": "flat"}
    # 迁移后 last_price 基线保留 → 直接可判穿越（语义延续 v1）
    levels = [PlaybookLevel("防线", 100.0)]
    d = _gate("688110", current_price=99.0, playbook_levels=levels)
    assert "playbook_cross:下穿防线@100" in d.reasons


def test_v1_lazy_migration_on_gate_call(gate_state_dir):
    """v1 文件无需显式迁移：首次 check_and_update 读取时惰性自愈为 v2。"""
    import os

    os.makedirs(os.path.dirname(_state_file(gate_state_dir)), exist_ok=True)
    with open(_state_file(gate_state_dir), "w", encoding="utf-8") as f:
        json.dump(_V1_STATE, f)

    d = _gate("688110", change_pct=0.1)
    assert isinstance(d.should_analyze, bool)
    state = _read_state(gate_state_dir)
    assert state["version"] == 2
    assert state["tenants"]["1"]["688110"]["pb_fired"]
    assert os.path.exists(_state_file(gate_state_dir) + ".v1.bak")


def test_v1_migration_backup_and_idempotent(gate_state_dir):
    """显式迁移入口：备份 .v1.bak 保留现场；重入返回 False 且文件不再变化。"""
    import os

    os.makedirs(os.path.dirname(_state_file(gate_state_dir)), exist_ok=True)
    path = _state_file(gate_state_dir)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_V1_STATE, f)

    assert migrate_state_file_to_v2(path) is True
    # 备份存在且内容为原 v1
    with open(path + ".v1.bak", encoding="utf-8") as f:
        assert json.load(f) == _V1_STATE

    with open(path, encoding="utf-8") as f:
        migrated = json.load(f)
    # 幂等重入：不再迁移，文件不变，备份不被覆盖
    assert migrate_state_file_to_v2(path) is False
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == migrated
    with open(path + ".v1.bak", encoding="utf-8") as f:
        assert json.load(f) == _V1_STATE


def test_v1_migration_preserves_tenant1_cooldown(gate_state_dir):
    """v1 冷却记录迁移后仍吞重复触发（默认租户 1 行为与 v1 完全延续）。"""
    import os

    from datetime import datetime, timezone

    os.makedirs(os.path.dirname(_state_file(gate_state_dir)), exist_ok=True)
    fresh = datetime.now(timezone.utc).isoformat()
    v1 = {
        "MT5": {
            "last_price": 99.0,
            "pb_fired": {"上穿:防线@100": fresh},
        }
    }
    with open(_state_file(gate_state_dir), "w", encoding="utf-8") as f:
        json.dump(v1, f)

    levels = [PlaybookLevel("防线", 100.0)]
    # 99 → 101 上穿，但 v1 迁移来的冷却在 1800s 内 → 吞掉（与 v1 行为一致）
    d = _gate("MT5", current_price=101.0, playbook_levels=levels)
    assert d.reasons == []


# --------------------------------------------------------------------------- #
# ③ playbook 价位解析按租户过滤（机制点 tenant_scope）
# --------------------------------------------------------------------------- #


def test_load_playbook_levels_filtered_by_tenant(mt_db):
    """同 symbol 双租户各持档案：_load_playbook_levels 只读本租户 is_active 档案。"""
    _, Session = mt_db
    db = Session()
    _seed_stock_with_playbook(db, tenant_id=1, symbol="MTS", defense_price=98)
    _seed_stock_with_playbook(db, tenant_id=2, symbol="MTS", defense_price=90)
    db.close()

    lv1 = gate._load_playbook_levels("MTS", tenant_id=1)
    lv2 = gate._load_playbook_levels("MTS", tenant_id=2)
    assert [(lv.name, lv.price) for lv in lv1] == [("防线", 98.0)]
    assert [(lv.name, lv.price) for lv in lv2] == [("防线", 90.0)]


def test_load_playbook_levels_cross_tenant_invisible(mt_db):
    """标的只属于租户 1：租户 2 视角读不到档案，fail-soft 返回空。"""
    _, Session = mt_db
    db = Session()
    _seed_stock_with_playbook(db, tenant_id=1, symbol="ONLY1", defense_price=98)
    db.close()

    assert gate._load_playbook_levels("ONLY1", tenant_id=2) == []
    assert gate._load_playbook_levels("ONLY1", tenant_id=1) == [
        PlaybookLevel("防线", 98.0)
    ]


def test_gate_auto_load_uses_caller_tenant(gate_state_dir, mt_db):
    """check_and_update 自动加载价位时按传入 tenant_id 过滤（端到端）。"""
    _, Session = mt_db
    db = Session()
    _seed_stock_with_playbook(db, tenant_id=1, symbol="E2E", defense_price=98)
    _seed_stock_with_playbook(db, tenant_id=2, symbol="E2E", defense_price=90)
    db.close()

    # 租户 1：110→99 不穿越其 98 防线
    _gate("E2E", tenant_id=1, current_price=110.0)
    d1 = _gate("E2E", tenant_id=1, current_price=99.0)
    assert not any(r.startswith("playbook_cross") for r in d1.reasons)
    # 租户 2：110→99 穿越其 90？不穿越（99>90）；穿越判据用各自档案
    _gate("E2E", tenant_id=2, current_price=110.0, kline_summary=None)
    d2 = _gate("E2E", tenant_id=2, current_price=89.0)
    assert "playbook_cross:下穿防线@90" in d2.reasons


# --------------------------------------------------------------------------- #
# ④ morning_brief / daily_report symbol 解析点机制点验证（docs/23 P16/P17）
# --------------------------------------------------------------------------- #


def test_morning_brief_playbook_summary_tenant_scoped(mt_db):
    """morning_brief._load_playbook_summary：tenant_scope 下机制点自动过滤。"""
    from src.agents.morning_brief import _load_playbook_summary

    _, Session = mt_db
    db = Session()
    _seed_stock_with_playbook(db, tenant_id=1, symbol="MB", defense_price=98)
    _seed_stock_with_playbook(db, tenant_id=2, symbol="MB", defense_price=90)
    db.close()

    with tenant_scope(1):
        s1 = _load_playbook_summary("MB", MarketCode.CN)
    with tenant_scope(2):
        s2 = _load_playbook_summary("MB", MarketCode.CN)
    assert s1 and "收盘<98" in s1 and "收盘<90" not in s1
    assert s2 and "收盘<90" in s2 and "收盘<98" not in s2

    # 仅租户 1 持有的标的：租户 2 解析为 None（机制点过滤，不串租户）
    db = Session()
    _seed_stock_with_playbook(db, tenant_id=1, symbol="MB1", defense_price=98)
    db.close()
    with tenant_scope(2):
        assert _load_playbook_summary("MB1", MarketCode.CN) is None
    with tenant_scope(1):
        assert _load_playbook_summary("MB1", MarketCode.CN) is not None


def test_daily_report_playbook_section_tenant_scoped(mt_db):
    """daily_report._build_one_playbook_section：Stock 解析（:272）+ 告警规则
    经 tenant_scope 机制点过滤，租户 2 不读到租户 1 的档案与规则。"""
    from src.agents.daily_report import DailyReportAgent

    _, Session = mt_db
    db = Session()
    stock1, playbook1 = _seed_stock_with_playbook(
        db, tenant_id=1, symbol="DR", defense_price=98
    )
    db.add(
        M.PriceAlertRule(
            tenant_id=1,
            stock_id=stock1.id,
            name="防线98",
            enabled=True,
            condition_group={},
            playbook_id=playbook1.id,
        )
    )
    # 租户 2：同 symbol，有档案但无告警规则
    _seed_stock_with_playbook(db, tenant_id=2, symbol="DR", defense_price=90)
    db.commit()
    db.close()

    agent = DailyReportAgent()
    w = SimpleNamespace(symbol="DR", market=MarketCode.CN)
    context = SimpleNamespace(portfolio=SimpleNamespace(all_positions=[]))
    symbol_contexts = {"DR": {"playbook": "摘要"}}

    with tenant_scope(1):
        db = Session()
        s1 = agent._build_one_playbook_section(db, w, context, {}, symbol_contexts)
        db.close()
    with tenant_scope(2):
        db = Session()
        s2 = agent._build_one_playbook_section(db, w, context, {}, symbol_contexts)
        db.close()

    assert s1 is not None and [t["name"] for t in s1["triggers"]] == ["防线98"]
    assert s2 is not None and s2["triggers"] == []


# --------------------------------------------------------------------------- #
# ⑤ intraday 个股节流键 {tenant}:{symbol}
# --------------------------------------------------------------------------- #


@pytest.fixture
def plain_db(monkeypatch):
    """单租户直通（默认）内存库：验证节流键格式与行为等价。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr("src.web.database.SessionLocal", Session)
    try:
        yield engine, Session
    finally:
        engine.dispose()


def test_throttle_key_default_tenant_format(plain_db):
    """默认 tenant_id=1：节流键 '1:{symbol}'（与 v120 旧行回填格式一致）。"""
    _, Session = plain_db
    agent = IntradayMonitorAgent(throttle_minutes=30)

    assert agent._check_throttle("688110") is True
    agent._update_throttle("688110")
    assert agent._check_throttle("688110") is False

    db = Session()
    row = (
        db.query(M.NotifyThrottle)
        .filter(M.NotifyThrottle.agent_name == "intraday_monitor")
        .one()
    )
    assert row.stock_symbol == "1:688110"
    assert row.tenant_id == 1
    db.close()


def test_throttle_isolated_between_tenants(plain_db):
    """同 symbol 跨租户节流互不影响（键前缀隔离）。"""
    _, Session = plain_db
    agent = IntradayMonitorAgent(throttle_minutes=30)

    agent._update_throttle("688110", tenant_id=2)
    # 租户 2 被节流；租户 1 同 symbol 不受影响
    assert agent._check_throttle("688110", tenant_id=2) is False
    assert agent._check_throttle("688110", tenant_id=1) is True

    db = Session()
    row = (
        db.query(M.NotifyThrottle)
        .filter(M.NotifyThrottle.agent_name == "intraday_monitor")
        .one()
    )
    assert row.stock_symbol == "2:688110"
    assert row.tenant_id == 2
    db.close()


def test_should_notify_threads_tenant_from_raw_data(plain_db, monkeypatch):
    """should_notify 从 result.raw_data['tenant_id'] 取租户做节流（缺省 1）。"""
    from src.agents.base import AnalysisResult

    agent = IntradayMonitorAgent(throttle_minutes=30)
    agent._update_throttle("688110", tenant_id=2)

    result = AnalysisResult(
        agent_name="intraday_monitor",
        title="t",
        content="c",
        raw_data={
            "tenant_id": 2,
            "should_alert": True,
            "stock": {"symbol": "688110"},
        },
    )
    import asyncio

    assert asyncio.run(agent.should_notify(result)) is False
    result.raw_data["tenant_id"] = 1
    assert asyncio.run(agent.should_notify(result)) is True
