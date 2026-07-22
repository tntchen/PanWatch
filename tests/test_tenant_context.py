"""MT-P1 租户穿透机制点测试：do_orm_execute 自动过滤、哨兵表、直通与未登记告警。

使用独立内存引擎 + 临时 ORM 模型复现事件注册，不污染全局 SessionLocal；
全局反射缓存 / 告警集合在用例结束后还原。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import Column, Integer, String, create_engine, delete, event, select, update
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import tenant_context as tc


class TBase(DeclarativeBase):
    pass


class TStock(TBase):
    """已登记租户私有表（非哨兵）。"""

    __tablename__ = "stocks"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, default=1)
    name = Column(String, default="")


class TCandidate(TBase):
    """哨兵表：谓词为 tenant_id IN (:ctx, 0)。"""

    __tablename__ = "entry_candidates"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, default=1)
    name = Column(String, default="")


class TGhost(TBase):
    """未登记但带 tenant_id 列的表：MT-P1 仅告警不过滤。"""

    __tablename__ = "ghost_unregistered_mt"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, default=1)
    name = Column(String, default="")


@pytest.fixture
def tdb():
    """独立内存引擎 + do_orm_execute 事件注册；用例后还原全局缓存。"""
    old_cache = dict(tc._tenant_column_cache)
    old_warned = set(tc._warned_unregistered)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TBase.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    event.listen(Session, "do_orm_execute", tc.apply_tenant_filter)
    tc.refresh_tenant_column_cache(engine)

    yield Session

    tc._tenant_column_cache.clear()
    tc._tenant_column_cache.update(old_cache)
    tc._warned_unregistered.clear()
    tc._warned_unregistered.update(old_warned)


def _seed(Session, model, rows):
    """rows: [(tenant_id, name), ...]"""
    s = Session()
    for tenant_id, name in rows:
        s.add(model(tenant_id=tenant_id, name=name))
    s.commit()
    s.close()


def _names(Session, model):
    s = Session()
    result = sorted(r.name for r in s.execute(select(model)).scalars().all())
    s.close()
    return result


# ---------------------------------------------------------------------------
# 上下文本体
# ---------------------------------------------------------------------------


def test_tenant_scope_ctx_lifecycle():
    """tenant_scope 正确设置/复位当前租户上下文。"""
    assert tc.current_tenant() is None
    with tc.tenant_scope(2) as ctx:
        assert ctx.tenant_id == 2
        assert tc.current_tenant() is not None
        assert tc.current_tenant().tenant_id == 2
    assert tc.current_tenant() is None


# ---------------------------------------------------------------------------
# 单租户直通（默认）
# ---------------------------------------------------------------------------


def test_single_tenant_passthrough(tdb, monkeypatch):
    """PANWATCH_SINGLE_TENANT=1（默认）时查询不带租户过滤。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    _seed(tdb, TStock, [(1, "a"), (2, "b"), (3, "c")])
    with tc.tenant_scope(2):
        assert _names(tdb, TStock) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# 多租户自动过滤
# ---------------------------------------------------------------------------


def test_select_filtered_by_tenant(tdb, monkeypatch):
    """多租户模式下 SELECT 自动注入 tenant_id 谓词。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    _seed(tdb, TStock, [(1, "a"), (2, "b"), (2, "c")])
    with tc.tenant_scope(2):
        assert _names(tdb, TStock) == ["b", "c"]


def test_bulk_update_filtered(tdb, monkeypatch):
    """bulk UPDATE 只影响当前租户的行。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    _seed(tdb, TStock, [(1, "a"), (2, "b")])

    with tc.tenant_scope(2):
        s = tdb()
        s.execute(update(TStock).values(name="x"))
        s.commit()
        s.close()

    # 无 ctx 放行，可见全量：租户1行未动，租户2行已更新
    assert _names(tdb, TStock) == ["a", "x"]


def test_bulk_delete_filtered(tdb, monkeypatch):
    """bulk DELETE 只删除当前租户的行。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    _seed(tdb, TStock, [(1, "a"), (2, "b"), (2, "c")])

    with tc.tenant_scope(2):
        s = tdb()
        s.execute(delete(TStock))
        s.commit()
        s.close()

    assert _names(tdb, TStock) == ["a"]


def test_sentinel_table_includes_market_rows(tdb, monkeypatch):
    """哨兵表 entry_candidates：可见 tenant_id IN (当前租户, 0) 的行。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    _seed(tdb, TCandidate, [(0, "mkt"), (1, "t1"), (2, "t2")])

    with tc.tenant_scope(2):
        assert _names(tdb, TCandidate) == ["mkt", "t2"]


def test_no_ctx_passthrough(tdb, monkeypatch):
    """无租户上下文（公开路由/裸脚本）MT-P1 放行不过滤。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    _seed(tdb, TStock, [(1, "a"), (2, "b")])
    assert tc.current_tenant() is None
    assert _names(tdb, TStock) == ["a", "b"]


def test_unregistered_table_warns_not_filters(tdb, monkeypatch, caplog):
    """未登记表带 tenant_id 列：warning 一次、不 raise、不过滤。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    _seed(tdb, TGhost, [(1, "a"), (2, "b")])

    with caplog.at_level(logging.WARNING, logger="src.web.tenant_context"):
        with tc.tenant_scope(2):
            assert _names(tdb, TGhost) == ["a", "b"]
            # 第二次查询同表不重复告警
            assert _names(tdb, TGhost) == ["a", "b"]

    hits = [r for r in caplog.records if "ghost_unregistered_mt" in r.getMessage()]
    assert len(hits) == 1
    assert hits[0].levelno == logging.WARNING


def test_shared_table_not_filtered(tdb, monkeypatch):
    """SHARED_TABLES（如 app_settings 形态）不做行级过滤。

    用 users 表（身份表，登记在 SHARED_TABLES）验证：多租户 + ctx 下仍可见全部行。
    """
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")

    class TUser(TBase):
        __tablename__ = "users"
        __table_args__ = {"extend_existing": True}
        id = Column(Integer, primary_key=True)
        tenant_id = Column(Integer, nullable=False, default=1)
        name = Column(String, default="")

    # users 表未随 TBase.metadata 创建，需临时建表并刷新缓存
    engine = tdb().get_bind()
    TUser.__table__.create(engine)
    tc.refresh_tenant_column_cache(engine)

    _seed(tdb, TUser, [(1, "a"), (2, "b")])
    with tc.tenant_scope(2):
        assert _names(tdb, TUser) == ["a", "b"]
