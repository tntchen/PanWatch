"""MT-P2 v120–v122 数据层隔离迁移冒烟（新增文件，不改动既有测试）。

覆盖两条路径：
1. create_all 新库 → 完整迁移链跑两遍（幂等），31 表 tenant_id、新表、
   app_settings 三分、v122 对账全过。
2. 旧 schema（无 tenant_id / 旧 UQ / 无 account_id）→ v120 加列回填 +
   v121 __new 重建 + v122 对账，验证断点重跑与行数守恒。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.web import models  # noqa: F401  注册全部 ORM 模型
from src.web.database import Base
from src.web.migrations import (
    MT_TENANT_TABLES_V120,
    run_versioned_migrations,
)


def _make_engine(tmp_path, name: str = "mt.db") -> Engine:
    return create_engine(f"sqlite:///{tmp_path / name}")


def _table_ddl(engine: Engine, table: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table},
        ).first()
    return str(row[0]) if row and row[0] else ""


def test_fresh_db_full_chain_idempotent(tmp_path):
    """全新库：完整迁移链跑两遍，v120–v122 幂等且对账通过。"""
    engine = _make_engine(tmp_path)
    Base.metadata.create_all(engine)
    run_versioned_migrations(engine)
    run_versioned_migrations(engine)  # 第二遍必须全部跳过

    with engine.connect() as conn:
        for table in MT_TENANT_TABLES_V120:
            cols = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            assert "tenant_id" in cols, f"{table} 缺 tenant_id"
        for table in (
            "tenants",
            "tenant_settings",
            "tenant_news_pushed",
            "agent_config_overrides",
        ):
            assert conn.execute(
                text("SELECT COUNT(*) FROM sqlite_master WHERE name=:t"),
                {"t": table},
            ).scalar() == 1
        # J6：override 表禁含 schedule
        override_cols = {
            r[1] for r in conn.execute(text("PRAGMA table_info(agent_config_overrides)"))
        }
        assert "schedule" not in override_cols
        # 单租户回退 flag（T20）
        assert conn.execute(
            text("SELECT COUNT(*) FROM app_settings WHERE key='single_tenant_mode'")
        ).scalar() == 1
        # 默认租户就位
        assert conn.execute(
            text("SELECT COUNT(*) FROM tenants WHERE id=1 AND is_default=1")
        ).scalar() == 1
        # 对账：FK 干净
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
        # 账本 120–123 成功
        rows = conn.execute(
            text(
                "SELECT version, success FROM schema_migrations "
                "WHERE version >= 120 ORDER BY version"
            )
        ).fetchall()
        assert [(int(r[0]), int(r[1])) for r in rows] == [
            (120, 1),
            (121, 1),
            (122, 1),
            (123, 1),
        ]
    engine.dispose()


def test_rebuild_from_legacy_schema(tmp_path):
    """旧库路径：v120 加列回填 → v121 重建 UQ/FK → v122 对账，跑两遍幂等。"""
    engine = _make_engine(tmp_path)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # 回退成旧 schema：stocks 无 tenant_id/无 UQ；analysis_history 旧 UQ；
        # paper 两表无 tenant_id/account_id，且账户表为空（触发默认账户创建）。
        conn.execute(text("DROP TABLE stocks"))
        conn.execute(
            text(
                """
CREATE TABLE stocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol VARCHAR NOT NULL, name VARCHAR NOT NULL, market VARCHAR NOT NULL,
  cost_price FLOAT, quantity INTEGER, invested_amount FLOAT,
  sort_order INTEGER, created_at DATETIME, updated_at DATETIME
)
"""
            )
        )
        conn.execute(
            text("INSERT INTO stocks (symbol, name, market) VALUES ('600519', '贵州茅台', 'CN')")
        )
        conn.execute(text("DROP TABLE analysis_history"))
        conn.execute(
            text(
                """
CREATE TABLE analysis_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_name VARCHAR NOT NULL, stock_symbol VARCHAR NOT NULL,
  analysis_date VARCHAR NOT NULL, title VARCHAR, content VARCHAR NOT NULL,
  raw_data JSON, agent_kind_snapshot VARCHAR,
  created_at DATETIME, updated_at DATETIME,
  CONSTRAINT uq_agent_stock_date UNIQUE (agent_name, stock_symbol, analysis_date)
)
"""
            )
        )
        conn.execute(
            text(
                "INSERT INTO analysis_history (agent_name, stock_symbol, analysis_date, content) "
                "VALUES ('daily_report', '600519', '2026-07-22', 'x')"
            )
        )
        conn.execute(text("DROP TABLE paper_trading_positions"))
        conn.execute(
            text(
                """
CREATE TABLE paper_trading_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_symbol VARCHAR NOT NULL, stock_market VARCHAR NOT NULL DEFAULT 'CN',
  stock_name VARCHAR, quantity INTEGER NOT NULL DEFAULT 100,
  entry_price FLOAT NOT NULL, stop_loss FLOAT, target_price FLOAT,
  current_price FLOAT, highest_price FLOAT,
  unrealized_pnl FLOAT NOT NULL DEFAULT 0.0,
  status VARCHAR NOT NULL DEFAULT 'open',
  signal_run_id INTEGER, signal_snapshot_date VARCHAR,
  signal_action VARCHAR, strategy_code VARCHAR,
  opened_at DATETIME, closed_at DATETIME, updated_at DATETIME
)
"""
            )
        )
        conn.execute(
            text(
                "INSERT INTO paper_trading_positions (stock_symbol, entry_price) "
                "VALUES ('600519', 1400.0)"
            )
        )
        conn.execute(text("DELETE FROM paper_trading_account"))

    run_versioned_migrations(engine)
    run_versioned_migrations(engine)  # 断点重跑幂等

    with engine.connect() as conn:
        # stocks：新 UQ + 行守恒 + tenant 回填
        assert "uq_stocks_tenant_symbol_market" in _table_ddl(engine, "stocks")
        row = conn.execute(
            text("SELECT symbol, tenant_id FROM stocks")
        ).first()
        assert row is not None and int(row[1]) == 1
        # analysis_history：UQ 加 tenant 前缀 + 行守恒
        assert "tenant_id, agent_name" in _table_ddl(
            engine, "analysis_history"
        ).lower().replace("  ", " ")
        assert conn.execute(text("SELECT COUNT(*) FROM analysis_history")).scalar() == 1
        # paper：v120 建默认账户并回填，v121 收紧 NOT NULL + FK
        assert "REFERENCES paper_trading_account" in _table_ddl(
            engine, "paper_trading_positions"
        )
        account_id = conn.execute(
            text("SELECT MIN(id) FROM paper_trading_account")
        ).scalar()
        assert account_id is not None
        pos = conn.execute(
            text("SELECT account_id, tenant_id FROM paper_trading_positions")
        ).first()
        assert pos is not None and int(pos[0]) == int(account_id) and int(pos[1]) == 1
        # 无半成品残留、FK 干净
        assert conn.execute(
            text("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'stocks__new%'")
        ).scalar() == 0
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    engine.dispose()
