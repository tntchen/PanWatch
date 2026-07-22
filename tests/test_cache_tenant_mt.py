"""MT-P2 缓存租户化与排他 is_default 收口（docs/22 §2 / docs/26-J11）。

验证点：
1. 四处进程内缓存（accounts 组合分析 / agents 盘中扫描 / insights 公告解读 /
   discovery 发现服务）的 key 统一编入 tenant_id；无 ctx 兜底 ``0``，
   单租户直通模式行为不变。
2. providers/channels 排他 ``is_default`` 复位在模型未映射 tenant_id
   （迁移双轨窗口期）时退化为原全表 update，行为等价。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.models.market import MarketCode
from src.web import models as M
from src.web.api import accounts as accounts_api
from src.web.api import agents as agents_api
from src.web.api import channels as channels_api
from src.web.api import discovery as discovery_api
from src.web.api import insights as insights_api
from src.web.api import providers as providers_api
from src.web.database import Base
from src.web.tenant_context import tenant_scope


class _WatchItem:
    """agents._build_scan_cache_key 需要的最小 watchlist 条目。"""

    def __init__(self, market: MarketCode, symbol: str) -> None:
        self.market = market
        self.symbol = symbol


# ── 缓存 key 租户前缀 ────────────────────────────────────────────────────


def test_prefix_fallback_zero_without_ctx():
    """无 ctx（裸脚本/公开路由）各模块前缀统一兜底 '0'。"""
    for mod in (accounts_api, agents_api, discovery_api, insights_api):
        assert mod._tenant_cache_prefix() == "0"


def test_prefix_uses_tenant_id_with_ctx():
    with tenant_scope(42):
        for mod in (accounts_api, agents_api, discovery_api, insights_api):
            assert mod._tenant_cache_prefix() == "42"
    # 退出 scope 后恢复兜底
    assert accounts_api._tenant_cache_prefix() == "0"


def test_scan_cache_key_includes_tenant():
    watchlist = [_WatchItem(MarketCode.CN, "600519"), _WatchItem(MarketCode.HK, "00700")]
    key_no_ctx = agents_api._build_scan_cache_key(False, watchlist)
    assert key_no_ctx.startswith("0:intraday_scan:0:")
    with tenant_scope(2):
        key_t2 = agents_api._build_scan_cache_key(False, watchlist)
    with tenant_scope(3):
        key_t3 = agents_api._build_scan_cache_key(False, watchlist)
    assert key_t2.startswith("2:intraday_scan:0:")
    assert key_t3.startswith("3:intraday_scan:0:")
    assert key_t2 != key_t3


def test_discovery_cache_isolated_by_tenant():
    discovery_api._cache.clear()
    try:
        with tenant_scope(1):
            discovery_api._cache_set("boards:CN:gainers:12", [{"code": "A"}])
        # 同租户命中
        with tenant_scope(1):
            assert discovery_api._cache_get("boards:CN:gainers:12", ttl_s=60) == [
                {"code": "A"}
            ]
        # 其他租户不命中（合成板块含本租户 watchlist，必须隔离）
        with tenant_scope(2):
            assert discovery_api._cache_get("boards:CN:gainers:12", ttl_s=60) is None
        # 无 ctx（兜底 0）也不命中租户 1 的条目
        assert discovery_api._cache_get("boards:CN:gainers:12", ttl_s=60) is None
    finally:
        discovery_api._cache.clear()


def test_scoped_key_prefix_format():
    with tenant_scope(7):
        assert discovery_api._scoped_key("stocks:CN:turnover:20").startswith(
            "7:stocks:CN:turnover:20"
        )


# ── 排他 is_default 复位（迁移窗口期无 tenant_id 列 → 全表 update 等价）──


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def test_reset_default_models_clears_all(db_session):
    db_session.add(M.AIModel(name="m1", service_id=1, model="gpt-a", is_default=True))
    db_session.add(M.AIModel(name="m2", service_id=1, model="gpt-b", is_default=True))
    db_session.commit()

    providers_api._reset_default_models(db_session)
    db_session.commit()

    remaining = db_session.query(M.AIModel).filter_by(is_default=True).count()
    assert remaining == 0


def test_reset_default_channels_clears_all(db_session):
    db_session.add(
        M.NotifyChannel(name="c1", type="telegram", config={}, is_default=True)
    )
    db_session.add(
        M.NotifyChannel(name="c2", type="webhook", config={}, is_default=True)
    )
    db_session.commit()

    channels_api._reset_default_channels(db_session)
    db_session.commit()

    remaining = db_session.query(M.NotifyChannel).filter_by(is_default=True).count()
    assert remaining == 0
