"""MT-P3：Agent 基类与上下文链租户化回归测试。

覆盖：
- notify 去重 scope 编入 tenant 前缀（docs/26-J4，UQ 不重建）；
- 去重表按租户隔离（两租户同内容不互吞）；
- AgentContext.tenant_id 默认 1（单租户直通等价）；
- context_store 快照按租户读写隔离 + 默认落租户 1；
- log_handler emit 时点捕获 tenant_id。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import context_store, notify_dedupe
from src.web.log_handler import DBLogHandler
from src.web.models import (
    Base,
    LogEntry,
    NewsTopicSnapshot,
    NotifyThrottle,
    StockContextSnapshot,
)
from src.web.tenant_context import tenant_scope


@pytest.fixture
def mem_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(notify_dedupe, "SessionLocal", Session)
    monkeypatch.setattr(context_store, "SessionLocal", Session)
    try:
        yield Session
    finally:
        engine.dispose()


def test_notify_scope_carries_tenant_prefix():
    assert notify_dedupe.build_notify_scope(1, "abc") == "__notify__:1:abc"
    assert notify_dedupe.build_notify_scope(2, "abc") == "__notify__:2:abc"
    # 兜底：0/None 归默认租户 1
    assert notify_dedupe.build_notify_scope(0, "abc") == "__notify__:1:abc"


def test_dedupe_isolated_per_tenant(mem_db):
    key = notify_dedupe.build_notify_dedupe_key("daily_report", "t", "c")
    scope1 = notify_dedupe.build_notify_scope(1, key)
    scope2 = notify_dedupe.build_notify_scope(2, key)
    # 租户 1 标记后，租户 1 被去重、租户 2 不受影响
    assert notify_dedupe.check_and_mark_notify(
        agent_name="daily_report", scope=scope1, ttl_minutes=60, mark=True, tenant_id=1
    )
    assert not notify_dedupe.check_and_mark_notify(
        agent_name="daily_report", scope=scope1, ttl_minutes=60, mark=False, tenant_id=1
    )
    assert notify_dedupe.check_and_mark_notify(
        agent_name="daily_report", scope=scope2, ttl_minutes=60, mark=False, tenant_id=2
    )
    # 行级 tenant_id 归因正确
    db = mem_db()
    try:
        row = db.query(NotifyThrottle).filter_by(stock_symbol=scope1).one()
        assert row.tenant_id == 1
    finally:
        db.close()


def test_agent_context_default_tenant():
    from src.agents.base import AgentContext

    ctx = AgentContext(ai_client=None, notifier=None, config=None)
    assert ctx.tenant_id == 1


def test_snapshot_read_write_tenant_isolation(mem_db):
    for tid in (1, 2):
        assert context_store.save_stock_context_snapshot(
            symbol="600519",
            market="CN",
            snapshot_date="2026-07-22",
            context_type="daily_report",
            payload={"tenant": tid},
            tenant_id=tid,
        )
    rows1 = context_store.get_recent_stock_context_snapshots(
        symbol="600519", market="CN", tenant_id=1
    )
    rows2 = context_store.get_recent_stock_context_snapshots(
        symbol="600519", market="CN", tenant_id=2
    )
    assert len(rows1) == len(rows2) == 1
    assert rows1[0].payload["tenant"] == 1
    assert rows2[0].payload["tenant"] == 2
    # 同 UQ 键不同租户各一行，不互覆
    db = mem_db()
    try:
        assert db.query(StockContextSnapshot).count() == 2
    finally:
        db.close()


def test_news_topic_snapshot_tenant_isolation(mem_db):
    for tid in (1, 2):
        assert context_store.save_news_topic_snapshot(
            snapshot_date="2026-07-22",
            window_days=7,
            symbols=["600519"],
            summary=f"t{tid}",
            topics=[],
            sentiment="neutral",
            tenant_id=tid,
        )
    assert context_store.get_latest_news_topic_snapshot(
        window_days=7, tenant_id=1
    ).summary == "t1"
    assert context_store.get_latest_news_topic_snapshot(
        window_days=7, tenant_id=2
    ).summary == "t2"
    db = mem_db()
    try:
        assert db.query(NewsTopicSnapshot).count() == 2
    finally:
        db.close()


def test_context_store_falls_back_to_tenant_scope(mem_db):
    with tenant_scope(2):
        assert context_store.save_agent_context_run(
            agent_name="daily_report",
            stock_symbol="*",
            analysis_date="2026-07-22",
            context_payload={},
        )
        runs = context_store.list_recent_agent_context_runs(agent_name="daily_report")
    assert len(runs) == 1
    assert runs[0].tenant_id == 2
    # 无 ctx 无显式参数 → 默认租户 1
    assert context_store.list_recent_agent_context_runs(agent_name="daily_report") == []


def test_cleanup_scoped_to_one_tenant(mem_db):
    for tid in (1, 2):
        assert context_store.save_stock_context_snapshot(
            symbol="600519",
            market="CN",
            snapshot_date="2020-01-01",
            context_type="daily_report",
            payload={},
            tenant_id=tid,
        )
    deleted = context_store.cleanup_context_data(snapshot_days=180, tenant_id=1)
    assert deleted["stock_context_snapshots"] == 1
    db = mem_db()
    try:
        remaining = db.query(StockContextSnapshot).all()
        assert len(remaining) == 1 and remaining[0].tenant_id == 2
    finally:
        db.close()


def test_log_handler_captures_tenant_at_emit(mem_db, monkeypatch):
    import src.web.log_handler as lh

    monkeypatch.setattr(lh, "SessionLocal", mem_db)
    handler = DBLogHandler()
    try:
        rec = logging.LogRecord(
            "test", logging.INFO, __file__, 1, "hello", (), None
        )
        handler.emit(rec)  # 无 ctx → 系统级 tenant_id=0
        with tenant_scope(2):
            rec2 = logging.LogRecord(
                "test", logging.INFO, __file__, 1, "scoped", (), None
            )
            handler.emit(rec2)
        with handler._lock:
            handler._flush_unlocked()
        db = mem_db()
        try:
            rows = db.query(LogEntry).order_by(LogEntry.id).all()
            assert [r.tenant_id for r in rows] == [0, 2]
        finally:
            db.close()
    finally:
        handler.close()
