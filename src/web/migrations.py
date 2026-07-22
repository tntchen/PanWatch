"""Versioned database migrations for PanWatch."""

from __future__ import annotations

import hashlib
import inspect
import logging
import json
from dataclasses import dataclass
from datetime import date
from typing import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    runner: Callable[[Connection], None]
    # transactional=True（默认）：runner 在 engine.begin() 事务内执行，行为与
    # v101-119 完全一致。False：runner 在独立连接上执行并自行管理逐表事务，
    # 供 v121 __new 重建在事务外切换 PRAGMA foreign_keys（docs/17 R4）。
    transactional: bool = True

    @property
    def checksum(self) -> str:
        try:
            body = inspect.getsource(self.runner)
        except Exception:
            body = self.name
        raw = f"{self.version}:{self.name}:{body}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def _has_table(conn: Connection, table: str) -> bool:
    row = conn.execute(
        text(
            """
SELECT name
FROM sqlite_master
WHERE type='table' AND name=:table
LIMIT 1
"""
        ),
        {"table": table},
    ).first()
    return bool(row)


def _has_column(conn: Connection, table: str, column: str) -> bool:
    if not _has_table(conn, table):
        return False
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    for r in rows:
        # PRAGMA table_info schema: cid, name, type, notnull, dflt_value, pk
        if len(r) > 1 and str(r[1]) == column:
            return True
    return False


def _add_column_if_missing(conn: Connection, table: str, column: str, sql: str) -> None:
    if not _has_table(conn, table):
        return
    if not _has_column(conn, table, column):
        conn.execute(text(sql))


def _create_index_if_missing(conn: Connection, name: str, sql: str) -> None:
    row = conn.execute(
        text(
            """
SELECT name
FROM sqlite_master
WHERE type='index' AND name=:name
LIMIT 1
"""
        ),
        {"name": name},
    ).first()
    if not row:
        conn.execute(text(sql))


def _ensure_schema_table(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  checksum TEXT NOT NULL,
  applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  success INTEGER NOT NULL DEFAULT 0,
  error TEXT DEFAULT ''
)
"""
        )
    )


def _m101_agent_config_kind(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "agent_configs",
        "kind",
        "ALTER TABLE agent_configs ADD COLUMN kind TEXT DEFAULT 'workflow'",
    )
    _add_column_if_missing(
        conn,
        "agent_configs",
        "visible",
        "ALTER TABLE agent_configs ADD COLUMN visible INTEGER DEFAULT 1",
    )
    _add_column_if_missing(
        conn,
        "agent_configs",
        "lifecycle_status",
        "ALTER TABLE agent_configs ADD COLUMN lifecycle_status TEXT DEFAULT 'active'",
    )
    _add_column_if_missing(
        conn,
        "agent_configs",
        "replaced_by",
        "ALTER TABLE agent_configs ADD COLUMN replaced_by TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "agent_configs",
        "display_order",
        "ALTER TABLE agent_configs ADD COLUMN display_order INTEGER DEFAULT 0",
    )


def _m102_backfill_agent_kind(conn: Connection) -> None:
    if not _has_table(conn, "agent_configs"):
        return

    conn.execute(
        text(
            """
UPDATE agent_configs
SET kind = 'workflow'
WHERE kind IS NULL OR TRIM(kind) = ''
"""
        )
    )
    conn.execute(
        text(
            """
UPDATE agent_configs
SET kind = 'capability',
    visible = 0,
    lifecycle_status = 'deprecated',
    replaced_by = CASE
      WHEN name = 'news_digest' THEN 'premarket_outlook,daily_report,intraday_monitor'
      WHEN name = 'chart_analyst' THEN 'intraday_monitor,daily_report,premarket_outlook'
      ELSE replaced_by
    END,
    enabled = 0,
    schedule = ''
WHERE name IN ('news_digest', 'chart_analyst')
"""
        )
    )
    conn.execute(
        text(
            """
UPDATE agent_configs
SET kind = 'workflow',
    visible = 1,
    lifecycle_status = 'active',
    replaced_by = ''
WHERE name IN ('premarket_outlook', 'intraday_monitor', 'daily_report')
"""
        )
    )
    conn.execute(
        text(
            """
UPDATE agent_configs
SET display_name = '收盘复盘'
WHERE name = 'daily_report'
  AND (display_name IS NULL OR TRIM(display_name) = '' OR display_name = '盘后日报')
"""
        )
    )
    conn.execute(
        text(
            """
UPDATE agent_configs
SET display_order = CASE name
  WHEN 'premarket_outlook' THEN 10
  WHEN 'intraday_monitor' THEN 20
  WHEN 'daily_report' THEN 30
  WHEN 'news_digest' THEN 110
  WHEN 'chart_analyst' THEN 120
  ELSE display_order
END
"""
        )
    )


def _m103_agent_run_observability(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "agent_runs",
        "trace_id",
        "ALTER TABLE agent_runs ADD COLUMN trace_id TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "agent_runs",
        "trigger_source",
        "ALTER TABLE agent_runs ADD COLUMN trigger_source TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "agent_runs",
        "notify_attempted",
        "ALTER TABLE agent_runs ADD COLUMN notify_attempted INTEGER DEFAULT 0",
    )
    _add_column_if_missing(
        conn,
        "agent_runs",
        "notify_sent",
        "ALTER TABLE agent_runs ADD COLUMN notify_sent INTEGER DEFAULT 0",
    )
    _add_column_if_missing(
        conn,
        "agent_runs",
        "context_chars",
        "ALTER TABLE agent_runs ADD COLUMN context_chars INTEGER DEFAULT 0",
    )
    _add_column_if_missing(
        conn,
        "agent_runs",
        "model_label",
        "ALTER TABLE agent_runs ADD COLUMN model_label TEXT DEFAULT ''",
    )


def _m104_history_kind_snapshot(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "analysis_history",
        "agent_kind_snapshot",
        "ALTER TABLE analysis_history ADD COLUMN agent_kind_snapshot TEXT DEFAULT ''",
    )

    if not _has_table(conn, "analysis_history"):
        return

    conn.execute(
        text(
            """
UPDATE analysis_history
SET agent_kind_snapshot = CASE
  WHEN agent_name IN ('news_digest', 'chart_analyst') THEN 'capability'
  ELSE 'workflow'
END
WHERE agent_kind_snapshot IS NULL OR TRIM(agent_kind_snapshot) = ''
"""
        )
    )


def _m105_indexes(conn: Connection) -> None:
    if _has_table(conn, "agent_configs"):
        _create_index_if_missing(
            conn,
            "ix_agent_configs_kind_visible",
            "CREATE INDEX ix_agent_configs_kind_visible ON agent_configs(kind, visible)",
        )
        _create_index_if_missing(
            conn,
            "ix_agent_configs_order",
            "CREATE INDEX ix_agent_configs_order ON agent_configs(display_order, name)",
        )
    if _has_table(conn, "agent_runs"):
        _create_index_if_missing(
            conn,
            "ix_agent_runs_agent_created",
            "CREATE INDEX ix_agent_runs_agent_created ON agent_runs(agent_name, created_at)",
        )
    if _has_table(conn, "analysis_history"):
        _create_index_if_missing(
            conn,
            "ix_analysis_history_kind_date",
            "CREATE INDEX ix_analysis_history_kind_date ON analysis_history(agent_kind_snapshot, analysis_date)",
        )
        _create_index_if_missing(
            conn,
            "ix_analysis_history_agent_updated",
            "CREATE INDEX ix_analysis_history_agent_updated ON analysis_history(agent_name, updated_at)",
        )


def _m106_log_observability(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "log_entries",
        "trace_id",
        "ALTER TABLE log_entries ADD COLUMN trace_id TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "run_id",
        "ALTER TABLE log_entries ADD COLUMN run_id TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "agent_name",
        "ALTER TABLE log_entries ADD COLUMN agent_name TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "event",
        "ALTER TABLE log_entries ADD COLUMN event TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "tags",
        "ALTER TABLE log_entries ADD COLUMN tags TEXT DEFAULT '{}'",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "notify_status",
        "ALTER TABLE log_entries ADD COLUMN notify_status TEXT DEFAULT ''",
    )
    _add_column_if_missing(
        conn,
        "log_entries",
        "notify_reason",
        "ALTER TABLE log_entries ADD COLUMN notify_reason TEXT DEFAULT ''",
    )

    if _has_table(conn, "log_entries"):
        _create_index_if_missing(
            conn,
            "ix_log_entries_time_id",
            "CREATE INDEX ix_log_entries_time_id ON log_entries(timestamp, id)",
        )
        _create_index_if_missing(
            conn,
            "ix_log_entries_trace",
            "CREATE INDEX ix_log_entries_trace ON log_entries(trace_id)",
        )
        _create_index_if_missing(
            conn,
            "ix_log_entries_agent_event",
            "CREATE INDEX ix_log_entries_agent_event ON log_entries(agent_name, event)",
        )


def _m107_suggestion_market_dimension(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "stock_suggestions",
        "stock_market",
        "ALTER TABLE stock_suggestions ADD COLUMN stock_market TEXT DEFAULT 'CN'",
    )

    if not _has_table(conn, "stock_suggestions"):
        return

    # 历史数据平滑回填：优先从 stocks 里推断 market，否则回退 CN。
    conn.execute(
        text(
            """
UPDATE stock_suggestions
SET stock_market = COALESCE(
    (
      SELECT s.market
      FROM stocks s
      WHERE s.symbol = stock_suggestions.stock_symbol
      ORDER BY CASE WHEN s.market='CN' THEN 0 ELSE 1 END, s.id ASC
      LIMIT 1
    ),
    'CN'
)
WHERE stock_market IS NULL OR TRIM(stock_market) = ''
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_suggestion_market_symbol_time",
        "CREATE INDEX ix_suggestion_market_symbol_time ON stock_suggestions(stock_market, stock_symbol, created_at)",
    )
    _create_index_if_missing(
        conn,
        "ix_suggestion_market_expires",
        "CREATE INDEX ix_suggestion_market_expires ON stock_suggestions(stock_market, expires_at)",
    )


def _m108_entry_candidates_table(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS entry_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  stock_name TEXT DEFAULT '',
  snapshot_date TEXT NOT NULL,
  status TEXT DEFAULT 'active',
  score REAL NOT NULL DEFAULT 0,
  confidence REAL,
  action TEXT NOT NULL DEFAULT 'watch',
  action_label TEXT NOT NULL DEFAULT '观望',
  signal TEXT DEFAULT '',
  reason TEXT DEFAULT '',
  entry_low REAL,
  entry_high REAL,
  stop_loss REAL,
  target_price REAL,
  invalidation TEXT DEFAULT '',
  source_agent TEXT DEFAULT '',
  source_suggestion_id INTEGER,
  source_trace_id TEXT DEFAULT '',
  evidence TEXT DEFAULT '[]',
  plan TEXT DEFAULT '{}',
  meta TEXT DEFAULT '{}',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_entry_candidate_stock_date UNIQUE(stock_symbol, stock_market, snapshot_date)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_entry_candidate_score_date",
        "CREATE INDEX ix_entry_candidate_score_date ON entry_candidates(snapshot_date, score)",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_candidate_status_updated",
        "CREATE INDEX ix_entry_candidate_status_updated ON entry_candidates(status, updated_at)",
    )

    # 历史平滑迁移：将每个市场/股票最新建议回填为“今日候选”基线记录。
    today = date.today().strftime("%Y-%m-%d")
    conn.execute(
        text(
            """
INSERT OR IGNORE INTO entry_candidates (
  stock_symbol, stock_market, stock_name, snapshot_date,
  status, score, action, action_label, signal, reason,
  source_agent, source_suggestion_id, evidence, plan, meta
)
SELECT
  s.stock_symbol,
  COALESCE(NULLIF(TRIM(s.stock_market), ''), 'CN') AS stock_market,
  COALESCE(s.stock_name, ''),
  :today AS snapshot_date,
  CASE
    WHEN s.action IN ('buy', 'add', 'hold', 'watch') THEN 'active'
    ELSE 'inactive'
  END AS status,
  CASE
    WHEN s.action = 'buy' THEN 78
    WHEN s.action = 'add' THEN 72
    WHEN s.action = 'hold' THEN 58
    WHEN s.action = 'watch' THEN 50
    ELSE 30
  END AS score,
  COALESCE(s.action, 'watch'),
  COALESCE(s.action_label, '观望'),
  COALESCE(s.signal, ''),
  COALESCE(s.reason, ''),
  COALESCE(s.agent_name, ''),
  s.id,
  '[]',
  '{}',
  COALESCE(s.meta, '{}')
FROM stock_suggestions s
JOIN (
  SELECT stock_symbol, COALESCE(NULLIF(TRIM(stock_market), ''), 'CN') AS stock_market, MAX(id) AS max_id
  FROM stock_suggestions
  GROUP BY stock_symbol, COALESCE(NULLIF(TRIM(stock_market), ''), 'CN')
) latest
ON latest.max_id = s.id
"""
        ),
        {"today": today},
    )


def _m109_entry_candidate_upgrade(conn: Connection) -> None:
    _add_column_if_missing(
        conn,
        "entry_candidates",
        "candidate_source",
        "ALTER TABLE entry_candidates ADD COLUMN candidate_source TEXT DEFAULT 'watchlist'",
    )
    _add_column_if_missing(
        conn,
        "entry_candidates",
        "strategy_tags",
        "ALTER TABLE entry_candidates ADD COLUMN strategy_tags TEXT DEFAULT '[]'",
    )
    _add_column_if_missing(
        conn,
        "entry_candidates",
        "is_holding_snapshot",
        "ALTER TABLE entry_candidates ADD COLUMN is_holding_snapshot INTEGER DEFAULT 0",
    )
    _add_column_if_missing(
        conn,
        "entry_candidates",
        "plan_quality",
        "ALTER TABLE entry_candidates ADD COLUMN plan_quality INTEGER DEFAULT 0",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_candidate_source_score",
        "CREATE INDEX ix_entry_candidate_source_score ON entry_candidates(candidate_source, score)",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_candidate_market_status",
        "CREATE INDEX ix_entry_candidate_market_status ON entry_candidates(stock_market, status)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS entry_candidate_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_date TEXT DEFAULT '',
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  candidate_source TEXT NOT NULL DEFAULT 'watchlist',
  strategy_tags TEXT DEFAULT '[]',
  useful INTEGER DEFAULT 1,
  reason TEXT DEFAULT '',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_entry_feedback_time",
        "CREATE INDEX ix_entry_feedback_time ON entry_candidate_feedback(created_at)",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_feedback_symbol_day",
        "CREATE INDEX ix_entry_feedback_symbol_day ON entry_candidate_feedback(stock_market, stock_symbol, snapshot_date)",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_feedback_source",
        "CREATE INDEX ix_entry_feedback_source ON entry_candidate_feedback(candidate_source)",
    )


def _m110_entry_candidate_outcomes(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS entry_candidate_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL REFERENCES entry_candidates(id) ON DELETE CASCADE,
  snapshot_date TEXT DEFAULT '',
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  candidate_source TEXT NOT NULL DEFAULT 'watchlist',
  strategy_tags TEXT DEFAULT '[]',
  horizon_days INTEGER NOT NULL DEFAULT 1,
  target_date TEXT DEFAULT '',
  base_price REAL,
  outcome_price REAL,
  outcome_return_pct REAL,
  hit_target INTEGER,
  hit_stop INTEGER,
  outcome_status TEXT DEFAULT 'pending',
  meta TEXT DEFAULT '{}',
  evaluated_at DATETIME,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_entry_outcome_candidate_horizon UNIQUE(candidate_id, horizon_days)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_entry_outcome_status_horizon",
        "CREATE INDEX ix_entry_outcome_status_horizon ON entry_candidate_outcomes(outcome_status, horizon_days)",
    )
    _create_index_if_missing(
        conn,
        "ix_entry_outcome_symbol_day",
        "CREATE INDEX ix_entry_outcome_symbol_day ON entry_candidate_outcomes(stock_market, stock_symbol, snapshot_date)",
    )


def _m111_strategy_layer(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_catalog (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  description TEXT DEFAULT '',
  version TEXT DEFAULT 'v1',
  enabled INTEGER DEFAULT 1,
  market_scope TEXT DEFAULT 'ALL',
  risk_level TEXT DEFAULT 'medium',
  params TEXT DEFAULT '{}',
  default_weight REAL DEFAULT 1.0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_catalog_enabled",
        "CREATE INDEX ix_strategy_catalog_enabled ON strategy_catalog(enabled)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_signal_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_date TEXT NOT NULL,
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  stock_name TEXT DEFAULT '',
  strategy_code TEXT NOT NULL,
  strategy_name TEXT DEFAULT '',
  strategy_version TEXT DEFAULT 'v1',
  risk_level TEXT DEFAULT 'medium',
  source_pool TEXT DEFAULT 'watchlist',
  score REAL NOT NULL DEFAULT 0,
  rank_score REAL NOT NULL DEFAULT 0,
  confidence REAL,
  status TEXT DEFAULT 'active',
  action TEXT DEFAULT 'watch',
  action_label TEXT DEFAULT '观望',
  signal TEXT DEFAULT '',
  reason TEXT DEFAULT '',
  evidence TEXT DEFAULT '[]',
  holding_days INTEGER DEFAULT 3,
  entry_low REAL,
  entry_high REAL,
  stop_loss REAL,
  target_price REAL,
  invalidation TEXT DEFAULT '',
  plan_quality INTEGER DEFAULT 0,
  source_agent TEXT DEFAULT '',
  source_suggestion_id INTEGER,
  source_candidate_id INTEGER,
  trace_id TEXT DEFAULT '',
  is_holding_snapshot INTEGER DEFAULT 0,
  context_quality_score REAL,
  payload TEXT DEFAULT '{}',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_strategy_signal_daily_unique UNIQUE(snapshot_date, stock_symbol, stock_market, strategy_code, source_candidate_id)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_signal_snapshot_rank",
        "CREATE INDEX ix_strategy_signal_snapshot_rank ON strategy_signal_runs(snapshot_date, rank_score)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_signal_strategy_market",
        "CREATE INDEX ix_strategy_signal_strategy_market ON strategy_signal_runs(strategy_code, stock_market)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_signal_status",
        "CREATE INDEX ix_strategy_signal_status ON strategy_signal_runs(status, updated_at)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_run_id INTEGER NOT NULL REFERENCES strategy_signal_runs(id) ON DELETE CASCADE,
  strategy_code TEXT NOT NULL,
  snapshot_date TEXT DEFAULT '',
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  source_pool TEXT DEFAULT 'watchlist',
  horizon_days INTEGER NOT NULL DEFAULT 1,
  target_date TEXT DEFAULT '',
  base_price REAL,
  outcome_price REAL,
  outcome_return_pct REAL,
  hit_target INTEGER,
  hit_stop INTEGER,
  outcome_status TEXT DEFAULT 'pending',
  meta TEXT DEFAULT '{}',
  evaluated_at DATETIME,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_strategy_outcome_signal_horizon UNIQUE(signal_run_id, horizon_days)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_outcome_strategy_horizon",
        "CREATE INDEX ix_strategy_outcome_strategy_horizon ON strategy_outcomes(strategy_code, horizon_days)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_outcome_market_date",
        "CREATE INDEX ix_strategy_outcome_market_date ON strategy_outcomes(stock_market, target_date)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_outcome_status",
        "CREATE INDEX ix_strategy_outcome_status ON strategy_outcomes(outcome_status, evaluated_at)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_weights (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_code TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'ALL',
  regime TEXT NOT NULL DEFAULT 'default',
  weight REAL NOT NULL DEFAULT 1.0,
  reason TEXT DEFAULT '',
  meta TEXT DEFAULT '{}',
  effective_from DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_strategy_weight_key UNIQUE(strategy_code, market, regime)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_weight_effective",
        "CREATE INDEX ix_strategy_weight_effective ON strategy_weights(effective_from)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_weight_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy_code TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'ALL',
  regime TEXT NOT NULL DEFAULT 'default',
  old_weight REAL NOT NULL DEFAULT 1.0,
  new_weight REAL NOT NULL DEFAULT 1.0,
  reason TEXT DEFAULT '',
  window_days INTEGER DEFAULT 45,
  sample_size INTEGER DEFAULT 0,
  meta TEXT DEFAULT '{}',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_weight_history_time",
        "CREATE INDEX ix_strategy_weight_history_time ON strategy_weight_history(created_at)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_weight_history_strategy_market",
        "CREATE INDEX ix_strategy_weight_history_strategy_market ON strategy_weight_history(strategy_code, market)",
    )

    # Seed strategy catalog (parameterized to avoid ':' bind parsing in JSON literals)
    seed_sql = text(
        """
INSERT OR IGNORE INTO strategy_catalog(
  code, name, description, version, enabled, market_scope, risk_level, params, default_weight
)
VALUES(
  :code, :name, :description, :version, :enabled, :market_scope, :risk_level, :params, :default_weight
)
"""
    )
    seed_rows = [
        {
            "code": "trend_follow",
            "name": "趋势延续",
            "description": "顺势跟随，优先均线多头且动量延续",
            "version": "v1",
            "enabled": 1,
            "market_scope": "ALL",
            "risk_level": "medium",
            "params": '{"horizon_days":5}',
            "default_weight": 1.15,
        },
        {
            "code": "macd_golden",
            "name": "MACD金叉",
            "description": "MACD 金叉确认，偏中短线",
            "version": "v1",
            "enabled": 1,
            "market_scope": "ALL",
            "risk_level": "medium",
            "params": '{"horizon_days":3}',
            "default_weight": 1.10,
        },
        {
            "code": "volume_breakout",
            "name": "放量突破",
            "description": "放量突破关键位，偏进攻",
            "version": "v1",
            "enabled": 1,
            "market_scope": "ALL",
            "risk_level": "high",
            "params": '{"horizon_days":3}',
            "default_weight": 1.18,
        },
        {
            "code": "pullback",
            "name": "回踩确认",
            "description": "回踩支撑后二次启动",
            "version": "v1",
            "enabled": 1,
            "market_scope": "ALL",
            "risk_level": "low",
            "params": '{"horizon_days":5}',
            "default_weight": 1.05,
        },
        {
            "code": "rebound",
            "name": "超跌反弹",
            "description": "超跌后的反弹交易",
            "version": "v1",
            "enabled": 1,
            "market_scope": "ALL",
            "risk_level": "high",
            "params": '{"horizon_days":3}',
            "default_weight": 0.95,
        },
        {
            "code": "watchlist_agent",
            "name": "Agent建议",
            "description": "来自既有 Agent 的综合建议映射",
            "version": "v1",
            "enabled": 1,
            "market_scope": "ALL",
            "risk_level": "medium",
            "params": '{"horizon_days":3}',
            "default_weight": 1.00,
        },
        {
            "code": "market_scan",
            "name": "市场扫描",
            "description": "市场池扫描策略（热门与活跃）",
            "version": "v1",
            "enabled": 1,
            "market_scope": "ALL",
            "risk_level": "medium",
            "params": '{"horizon_days":3}',
            "default_weight": 1.08,
        },
    ]
    for row in seed_rows:
        conn.execute(seed_sql, row)

    # Legacy smooth migration: entry_candidates -> strategy_signal_runs
    if _has_table(conn, "entry_candidates"):
        conn.execute(
            text(
                """
INSERT OR IGNORE INTO strategy_signal_runs (
  snapshot_date, stock_symbol, stock_market, stock_name,
  strategy_code, strategy_name, strategy_version, risk_level, source_pool,
  score, rank_score, confidence, status, action, action_label, signal, reason,
  evidence, holding_days, entry_low, entry_high, stop_loss, target_price, invalidation,
  plan_quality, source_agent, source_suggestion_id, source_candidate_id, trace_id,
  is_holding_snapshot, payload, created_at, updated_at
)
SELECT
  ec.snapshot_date,
  ec.stock_symbol,
  ec.stock_market,
  ec.stock_name,
  CASE
    WHEN ec.strategy_tags LIKE '%trend_follow%' THEN 'trend_follow'
    WHEN ec.strategy_tags LIKE '%macd_golden%' THEN 'macd_golden'
    WHEN ec.strategy_tags LIKE '%volume_breakout%' THEN 'volume_breakout'
    WHEN ec.strategy_tags LIKE '%pullback%' THEN 'pullback'
    WHEN ec.strategy_tags LIKE '%rebound%' THEN 'rebound'
    WHEN ec.candidate_source = 'market_scan' THEN 'market_scan'
    ELSE 'watchlist_agent'
  END AS strategy_code,
  CASE
    WHEN ec.strategy_tags LIKE '%trend_follow%' THEN '趋势延续'
    WHEN ec.strategy_tags LIKE '%macd_golden%' THEN 'MACD金叉'
    WHEN ec.strategy_tags LIKE '%volume_breakout%' THEN '放量突破'
    WHEN ec.strategy_tags LIKE '%pullback%' THEN '回踩确认'
    WHEN ec.strategy_tags LIKE '%rebound%' THEN '超跌反弹'
    WHEN ec.candidate_source = 'market_scan' THEN '市场扫描'
    ELSE 'Agent建议'
  END AS strategy_name,
  'v1' AS strategy_version,
  CASE
    WHEN ec.action IN ('buy', 'add') AND ec.score >= 80 THEN 'high'
    WHEN ec.action IN ('watch', 'hold') THEN 'low'
    ELSE 'medium'
  END AS risk_level,
  COALESCE(ec.candidate_source, 'watchlist') AS source_pool,
  ec.score,
  ec.score,
  ec.confidence,
  ec.status,
  ec.action,
  ec.action_label,
  COALESCE(ec.signal, ''),
  COALESCE(ec.reason, ''),
  COALESCE(ec.evidence, '[]'),
  3,
  ec.entry_low,
  ec.entry_high,
  ec.stop_loss,
  ec.target_price,
  COALESCE(ec.invalidation, ''),
  COALESCE(ec.plan_quality, 0),
  COALESCE(ec.source_agent, ''),
  ec.source_suggestion_id,
  ec.id,
  COALESCE(ec.source_trace_id, ''),
  COALESCE(ec.is_holding_snapshot, 0),
  COALESCE(ec.meta, '{}'),
  ec.created_at,
  ec.updated_at
FROM entry_candidates ec
"""
            )
        )

    # Legacy smooth migration: entry_candidate_outcomes -> strategy_outcomes
    if _has_table(conn, "entry_candidate_outcomes"):
        conn.execute(
            text(
                """
INSERT OR IGNORE INTO strategy_outcomes (
  signal_run_id, strategy_code, snapshot_date, stock_symbol, stock_market, source_pool,
  horizon_days, target_date, base_price, outcome_price, outcome_return_pct,
  hit_target, hit_stop, outcome_status, meta, evaluated_at, created_at
)
SELECT
  sr.id,
  sr.strategy_code,
  eco.snapshot_date,
  eco.stock_symbol,
  eco.stock_market,
  COALESCE(eco.candidate_source, 'watchlist'),
  eco.horizon_days,
  eco.target_date,
  eco.base_price,
  eco.outcome_price,
  eco.outcome_return_pct,
  eco.hit_target,
  eco.hit_stop,
  eco.outcome_status,
  COALESCE(eco.meta, '{}'),
  eco.evaluated_at,
  eco.created_at
FROM entry_candidate_outcomes eco
JOIN strategy_signal_runs sr ON sr.source_candidate_id = eco.candidate_id
"""
            )
        )


def _m112_strategy_analytics_snapshots(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS market_regime_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_date TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  regime TEXT NOT NULL DEFAULT 'neutral',
  regime_score REAL NOT NULL DEFAULT 0.0,
  confidence REAL NOT NULL DEFAULT 0.0,
  breadth_up_pct REAL,
  avg_change_pct REAL,
  volatility_pct REAL,
  active_ratio REAL,
  sample_size INTEGER DEFAULT 0,
  meta TEXT DEFAULT '{}',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_market_regime_day_market UNIQUE(snapshot_date, market)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_market_regime_snapshot",
        "CREATE INDEX ix_market_regime_snapshot ON market_regime_snapshots(snapshot_date, market)",
    )
    _create_index_if_missing(
        conn,
        "ix_market_regime_type",
        "CREATE INDEX ix_market_regime_type ON market_regime_snapshots(regime)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS strategy_factor_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signal_run_id INTEGER NOT NULL REFERENCES strategy_signal_runs(id) ON DELETE CASCADE,
  snapshot_date TEXT NOT NULL,
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  strategy_code TEXT NOT NULL,
  alpha_score REAL DEFAULT 0.0,
  catalyst_score REAL DEFAULT 0.0,
  quality_score REAL DEFAULT 0.0,
  risk_penalty REAL DEFAULT 0.0,
  crowd_penalty REAL DEFAULT 0.0,
  source_bonus REAL DEFAULT 0.0,
  regime_multiplier REAL DEFAULT 1.0,
  final_score REAL NOT NULL DEFAULT 0.0,
  factor_payload TEXT DEFAULT '{}',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_strategy_factor_signal UNIQUE(signal_run_id)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_factor_snapshot_score",
        "CREATE INDEX ix_strategy_factor_snapshot_score ON strategy_factor_snapshots(snapshot_date, final_score)",
    )
    _create_index_if_missing(
        conn,
        "ix_strategy_factor_strategy_market",
        "CREATE INDEX ix_strategy_factor_strategy_market ON strategy_factor_snapshots(strategy_code, stock_market)",
    )

    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS portfolio_risk_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_date TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'CN',
  total_signals INTEGER DEFAULT 0,
  active_signals INTEGER DEFAULT 0,
  held_signals INTEGER DEFAULT 0,
  unheld_signals INTEGER DEFAULT 0,
  high_risk_ratio REAL,
  concentration_top5 REAL,
  avg_rank_score REAL,
  risk_level TEXT DEFAULT 'medium',
  meta TEXT DEFAULT '{}',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_portfolio_risk_day_market UNIQUE(snapshot_date, market)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_portfolio_risk_snapshot",
        "CREATE INDEX ix_portfolio_risk_snapshot ON portfolio_risk_snapshots(snapshot_date, market)",
    )

    if not _has_table(conn, "strategy_signal_runs"):
        return

    rows = conn.execute(
        text(
            """
SELECT
  id,
  snapshot_date,
  stock_symbol,
  stock_market,
  strategy_code,
  status,
  risk_level,
  rank_score,
  is_holding_snapshot,
  payload
FROM strategy_signal_runs
ORDER BY snapshot_date DESC, stock_market ASC, rank_score DESC
"""
        )
    ).fetchall()
    if not rows:
        return

    factor_insert = text(
        """
INSERT OR IGNORE INTO strategy_factor_snapshots(
  signal_run_id, snapshot_date, stock_symbol, stock_market, strategy_code,
  alpha_score, catalyst_score, quality_score, risk_penalty, crowd_penalty, source_bonus,
  regime_multiplier, final_score, factor_payload
)
VALUES(
  :signal_run_id, :snapshot_date, :stock_symbol, :stock_market, :strategy_code,
  :alpha_score, :catalyst_score, :quality_score, :risk_penalty, :crowd_penalty, :source_bonus,
  :regime_multiplier, :final_score, :factor_payload
)
"""
    )

    bucket: dict[tuple[str, str], dict] = {}
    for r in rows:
        signal_id = int(r[0])
        snapshot_date = str(r[1] or "")
        stock_symbol = str(r[2] or "")
        stock_market = str(r[3] or "CN")
        strategy_code = str(r[4] or "")
        status = str(r[5] or "")
        risk_level = str(r[6] or "medium")
        rank_score = float(r[7] or 0.0)
        is_holding = bool(r[8] or 0)
        payload_raw = r[9]

        payload_obj = {}
        if isinstance(payload_raw, str) and payload_raw.strip():
            try:
                payload_obj = json.loads(payload_raw)
            except Exception:
                payload_obj = {}
        elif isinstance(payload_raw, dict):
            payload_obj = payload_raw

        change_pct = None
        source_meta = payload_obj.get("source_meta") if isinstance(payload_obj, dict) else None
        if isinstance(source_meta, dict):
            quote = source_meta.get("quote") if isinstance(source_meta.get("quote"), dict) else {}
            try:
                if quote.get("change_pct") is not None:
                    change_pct = float(quote.get("change_pct"))
            except Exception:
                change_pct = None

        # Backfill factor snapshot with conservative decomposition.
        conn.execute(
            factor_insert,
            {
                "signal_run_id": signal_id,
                "snapshot_date": snapshot_date,
                "stock_symbol": stock_symbol,
                "stock_market": stock_market,
                "strategy_code": strategy_code,
                "alpha_score": round(rank_score * 0.35, 4),
                "catalyst_score": 0.0,
                "quality_score": round(rank_score * 0.15, 4),
                "risk_penalty": 0.0,
                "crowd_penalty": 0.0,
                "source_bonus": 0.0,
                "regime_multiplier": 1.0,
                "final_score": round(rank_score, 4),
                "factor_payload": json.dumps(
                    {
                        "backfilled": True,
                        "change_pct": change_pct,
                    },
                    ensure_ascii=False,
                ),
            },
        )

        key = (snapshot_date, stock_market)
        agg = bucket.setdefault(
            key,
            {
                "scores": [],
                "changes": [],
                "total": 0,
                "active": 0,
                "held": 0,
                "high_risk": 0,
            },
        )
        agg["total"] += 1
        if status == "active":
            agg["active"] += 1
        if is_holding:
            agg["held"] += 1
        if risk_level == "high":
            agg["high_risk"] += 1
        agg["scores"].append(rank_score)
        if change_pct is not None:
            agg["changes"].append(change_pct)

    regime_insert = text(
        """
INSERT OR REPLACE INTO market_regime_snapshots(
  snapshot_date, market, regime, regime_score, confidence, breadth_up_pct,
  avg_change_pct, volatility_pct, active_ratio, sample_size, meta, updated_at
)
VALUES(
  :snapshot_date, :market, :regime, :regime_score, :confidence, :breadth_up_pct,
  :avg_change_pct, :volatility_pct, :active_ratio, :sample_size, :meta, CURRENT_TIMESTAMP
)
"""
    )
    risk_insert = text(
        """
INSERT OR REPLACE INTO portfolio_risk_snapshots(
  snapshot_date, market, total_signals, active_signals, held_signals, unheld_signals,
  high_risk_ratio, concentration_top5, avg_rank_score, risk_level, meta, updated_at
)
VALUES(
  :snapshot_date, :market, :total_signals, :active_signals, :held_signals, :unheld_signals,
  :high_risk_ratio, :concentration_top5, :avg_rank_score, :risk_level, :meta, CURRENT_TIMESTAMP
)
"""
    )

    for (snap, market), agg in bucket.items():
        total = int(agg["total"] or 0)
        active = int(agg["active"] or 0)
        held = int(agg["held"] or 0)
        unheld = max(0, total - held)
        scores = sorted([float(x) for x in agg["scores"] if x is not None], reverse=True)
        score_sum = sum(scores)
        avg_score = (score_sum / total) if total else 0.0
        top5 = sum(scores[:5])
        concentration = (top5 / score_sum) if score_sum > 0 else 0.0
        high_risk_ratio = (float(agg["high_risk"]) / total) if total else 0.0

        changes = [float(x) for x in agg["changes"] if x is not None]
        breadth_up_pct = (
            sum(1 for c in changes if c > 0) / len(changes) * 100.0 if changes else None
        )
        avg_change_pct = (sum(changes) / len(changes)) if changes else None
        volatility_pct = None
        if len(changes) >= 2:
            mean = sum(changes) / len(changes)
            variance = sum((c - mean) ** 2 for c in changes) / (len(changes) - 1)
            volatility_pct = variance ** 0.5

        active_ratio = (active / total) if total else 0.0
        breadth_norm = (
            max(-1.0, min(1.0, ((breadth_up_pct or 50.0) - 50.0) / 50.0))
            if breadth_up_pct is not None
            else 0.0
        )
        change_norm = (
            max(-1.0, min(1.0, (avg_change_pct or 0.0) / 3.0))
            if avg_change_pct is not None
            else 0.0
        )
        active_norm = max(-1.0, min(1.0, (active_ratio - 0.5) / 0.5))
        regime_score = 0.45 * breadth_norm + 0.30 * change_norm + 0.25 * active_norm
        if regime_score >= 0.20:
            regime = "bullish"
        elif regime_score <= -0.20:
            regime = "bearish"
        else:
            regime = "neutral"
        confidence = min(
            1.0,
            max(0.0, abs(regime_score) * 1.4 + min(0.4, total / 250.0)),
        )

        if high_risk_ratio >= 0.45 or concentration >= 0.65:
            risk_level = "high"
        elif high_risk_ratio >= 0.28 or concentration >= 0.48:
            risk_level = "medium"
        else:
            risk_level = "low"

        conn.execute(
            regime_insert,
            {
                "snapshot_date": snap,
                "market": market,
                "regime": regime,
                "regime_score": round(regime_score, 4),
                "confidence": round(confidence, 4),
                "breadth_up_pct": round(breadth_up_pct, 4) if breadth_up_pct is not None else None,
                "avg_change_pct": round(avg_change_pct, 4) if avg_change_pct is not None else None,
                "volatility_pct": round(volatility_pct, 4) if volatility_pct is not None else None,
                "active_ratio": round(active_ratio, 4),
                "sample_size": total,
                "meta": json.dumps(
                    {
                        "from_strategy_runs": True,
                        "active_signals": active,
                    },
                    ensure_ascii=False,
                ),
            },
        )
        conn.execute(
            risk_insert,
            {
                "snapshot_date": snap,
                "market": market,
                "total_signals": total,
                "active_signals": active,
                "held_signals": held,
                "unheld_signals": unheld,
                "high_risk_ratio": round(high_risk_ratio, 4),
                "concentration_top5": round(concentration, 4),
                "avg_rank_score": round(avg_score, 4),
                "risk_level": risk_level,
                "meta": json.dumps(
                    {
                        "from_strategy_runs": True,
                        "score_sum": round(score_sum, 4),
                    },
                    ensure_ascii=False,
                ),
            },
        )


def _m113_market_scan_snapshot_and_mixed_source(conn: Connection) -> None:
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS market_scan_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_date TEXT NOT NULL,
  stock_symbol TEXT NOT NULL,
  stock_market TEXT NOT NULL DEFAULT 'CN',
  stock_name TEXT DEFAULT '',
  source TEXT NOT NULL DEFAULT 'market_scan',
  score_seed REAL NOT NULL DEFAULT 0.0,
  quote TEXT DEFAULT '{}',
  meta TEXT DEFAULT '{}',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_market_scan_snapshot_symbol UNIQUE(snapshot_date, stock_symbol, stock_market)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_market_scan_snapshot_day_market",
        "CREATE INDEX ix_market_scan_snapshot_day_market ON market_scan_snapshots(snapshot_date, stock_market)",
    )
    _create_index_if_missing(
        conn,
        "ix_market_scan_snapshot_source",
        "CREATE INDEX ix_market_scan_snapshot_source ON market_scan_snapshots(snapshot_date, source)",
    )

    if _has_table(conn, "entry_candidates"):
        conn.execute(
            text(
                """
UPDATE entry_candidates
SET candidate_source = 'mixed'
WHERE candidate_source = 'market_scan'
  AND source_suggestion_id IS NOT NULL
"""
            )
        )
    if _has_table(conn, "strategy_signal_runs"):
        conn.execute(
            text(
                """
UPDATE strategy_signal_runs
SET source_pool = 'mixed'
WHERE source_pool = 'market_scan'
  AND source_suggestion_id IS NOT NULL
"""
            )
        )


def _m114_paper_trading_tables(conn: Connection) -> None:
    """创建模拟盘三张表。"""
    if not _has_table(conn, "paper_trading_account"):
        conn.execute(
            text(
                """
CREATE TABLE paper_trading_account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    initial_capital REAL NOT NULL DEFAULT 1000000.0,
    current_capital REAL NOT NULL DEFAULT 1000000.0,
    total_pnl REAL NOT NULL DEFAULT 0.0,
    total_trades INTEGER NOT NULL DEFAULT 0,
    winning_trades INTEGER NOT NULL DEFAULT 0,
    max_drawdown_pct REAL NOT NULL DEFAULT 0.0,
    peak_capital REAL NOT NULL DEFAULT 1000000.0,
    enabled BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""
            )
        )
    if not _has_table(conn, "paper_trading_positions"):
        conn.execute(
            text(
                """
CREATE TABLE paper_trading_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_symbol TEXT NOT NULL,
    stock_market TEXT NOT NULL DEFAULT 'CN',
    stock_name TEXT DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 100,
    entry_price REAL NOT NULL,
    stop_loss REAL,
    target_price REAL,
    current_price REAL,
    unrealized_pnl REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'open',
    signal_run_id INTEGER,
    signal_snapshot_date TEXT DEFAULT '',
    signal_action TEXT DEFAULT '',
    strategy_code TEXT DEFAULT '',
    opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    closed_at DATETIME,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""
            )
        )
        conn.execute(text("CREATE INDEX ix_paper_pos_status ON paper_trading_positions(status)"))
        conn.execute(text("CREATE INDEX ix_paper_pos_symbol_market ON paper_trading_positions(stock_symbol, stock_market)"))
    if not _has_table(conn, "paper_trading_trades"):
        conn.execute(
            text(
                """
CREATE TABLE paper_trading_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_symbol TEXT NOT NULL,
    stock_market TEXT NOT NULL DEFAULT 'CN',
    stock_name TEXT DEFAULT '',
    quantity INTEGER NOT NULL DEFAULT 100,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    pnl REAL NOT NULL DEFAULT 0.0,
    pnl_pct REAL NOT NULL DEFAULT 0.0,
    exit_reason TEXT NOT NULL DEFAULT '',
    signal_run_id INTEGER,
    signal_snapshot_date TEXT DEFAULT '',
    strategy_code TEXT DEFAULT '',
    holding_days INTEGER DEFAULT 0,
    opened_at DATETIME,
    closed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    meta TEXT DEFAULT '{}'
)
"""
            )
        )
        conn.execute(text("CREATE INDEX ix_paper_trade_closed ON paper_trading_trades(closed_at)"))
        conn.execute(text("CREATE INDEX ix_paper_trade_symbol ON paper_trading_trades(stock_symbol, stock_market)"))


def _m115_paper_trading_excluded_markets(conn: Connection) -> None:
    """模拟盘账户新增 excluded_markets 字段。"""
    _add_column_if_missing(
        conn,
        "paper_trading_account",
        "excluded_markets",
        "ALTER TABLE paper_trading_account ADD COLUMN excluded_markets TEXT DEFAULT '[]'",
    )


def _m116_chat_tables(conn: Connection) -> None:
    """AI 对话表。"""
    conn.execute(
        text("""
        CREATE TABLE IF NOT EXISTS chat_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT DEFAULT '',
            stock_symbol TEXT,
            stock_market TEXT,
            ai_model_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    )
    conn.execute(
        text("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            content TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    )
    _create_index_if_missing(
        conn, "ix_chat_conv_updated",
        "CREATE INDEX ix_chat_conv_updated ON chat_conversations(updated_at)",
    )
    _create_index_if_missing(
        conn, "ix_chat_conv_stock",
        "CREATE INDEX ix_chat_conv_stock ON chat_conversations(stock_symbol, stock_market)",
    )
    _create_index_if_missing(
        conn, "ix_chat_msg_conv",
        "CREATE INDEX ix_chat_msg_conv ON chat_messages(conversation_id, created_at)",
    )


def _m117_chat_initial_context(conn: Connection) -> None:
    """Add initial_context column to chat_conversations."""
    try:
        conn.execute(text("ALTER TABLE chat_conversations ADD COLUMN initial_context TEXT"))
    except Exception:
        pass  # column already exists


def _m118_paper_trading_market_allocations(conn: Connection) -> None:
    """模拟盘账户新增 market_allocations（各市场投资比例），并由 excluded_markets 回填。"""
    _add_column_if_missing(
        conn,
        "paper_trading_account",
        "market_allocations",
        "ALTER TABLE paper_trading_account ADD COLUMN market_allocations TEXT DEFAULT '{}'",
    )
    if not _has_table(conn, "paper_trading_account"):
        return

    # 复用引擎的纯函数推导比例（函数内导入，避免模块级循环依赖）
    from src.core.paper_trading_engine import allocations_from_excluded

    rows = conn.execute(
        text("SELECT id, excluded_markets, market_allocations FROM paper_trading_account")
    ).fetchall()
    for r in rows:
        row_id = r[0]

        # 已有非空比例则跳过，避免覆盖用户配置
        raw_alloc = r[2]
        has_alloc = False
        if isinstance(raw_alloc, str) and raw_alloc.strip() and raw_alloc.strip() not in ("{}", "null"):
            try:
                has_alloc = bool(json.loads(raw_alloc))
            except Exception:
                has_alloc = False
        elif isinstance(raw_alloc, dict):
            has_alloc = bool(raw_alloc)
        if has_alloc:
            continue

        excluded: list[str] = []
        raw_excluded = r[1]
        if isinstance(raw_excluded, str) and raw_excluded.strip():
            try:
                parsed = json.loads(raw_excluded)
                if isinstance(parsed, list):
                    excluded = [str(x) for x in parsed]
            except Exception:
                excluded = []
        elif isinstance(raw_excluded, list):
            excluded = [str(x) for x in raw_excluded]

        alloc = allocations_from_excluded(excluded)
        conn.execute(
            text("UPDATE paper_trading_account SET market_allocations = :alloc WHERE id = :id"),
            {"alloc": json.dumps(alloc, ensure_ascii=False), "id": row_id},
        )


def _m119_price_alert_rule_playbook_id(conn: Connection) -> None:
    """price_alert_rules 新增 playbook_id（关联个股方案档案，可空）。"""
    _add_column_if_missing(
        conn,
        "price_alert_rules",
        "playbook_id",
        "ALTER TABLE price_alert_rules ADD COLUMN playbook_id INTEGER",
    )


# ---------------------------------------------------------------------------
# MT-P2 数据层隔离（多租户）
#
# 蓝本：docs/21-MT-P0-schema变更清单.md §3–§5；裁决：docs/26-MT-P0-汇总与裁决.md
#   v120：建表 + 加列 + 回填（§3，幂等可重入）
#   v121：7 张表 __new 约束重建（§4，清半成品 + 行数对账）
#   v122：对账（§5，独立连接，失配即 raise）
# 要点：
#   - tenant_id 一律 INTEGER NOT NULL DEFAULT 1（T18），裸列不加 FK（SQLite
#     ADD COLUMN 限制，docs/21 §3.1）；FK 仅出现在 __new 重建表内。
#   - agent_config_overrides 禁含 schedule 字段（docs/26-J6）。
#   - notify_throttle 不重建 UQ，旧行 scope 回填 __notify__:1:{hash}（docs/26-J4、
#     docs/23 §3.3）。
#   - entry_candidates / strategy_signal_runs 允许 tenant_id=0 市场级哨兵
#     （docs/26-J2/J3）；log_entries 系统日志允许 tenant_id=0（docs/26-J11）。
# ---------------------------------------------------------------------------

#: v120 需要加 tenant_id 的 A 类私有表（docs/21 §3.1 适用表清单，31 张）。
MT_TENANT_TABLES_V120: tuple[str, ...] = (
    "ai_services",
    "ai_models",
    "notify_channels",
    "accounts",
    "stocks",
    "positions",
    "position_trades",
    "stock_agents",
    "agent_runs",
    "log_entries",
    "notify_throttle",
    "analysis_history",
    "stock_context_snapshots",
    "news_topic_snapshots",
    "agent_context_runs",
    "agent_prediction_outcomes",
    "stock_suggestions",
    "entry_candidates",
    "entry_candidate_feedback",
    "entry_candidate_outcomes",
    "suggestion_feedback",
    "price_alert_rules",
    "price_alert_hits",
    "stock_playbooks",
    "paper_trading_account",
    "paper_trading_positions",
    "paper_trading_trades",
    "chat_conversations",
    "chat_messages",
    "strategy_signal_runs",
    "strategy_outcomes",
)

#: 允许 tenant_id=0（市场级哨兵 / 系统日志）的表（docs/26-J2/J3/J11）。
MT_SENTINEL_ZERO_TABLES: frozenset[str] = frozenset(
    {
        "entry_candidates",
        "strategy_signal_runs",
        "strategy_outcomes",
        "log_entries",
    }
)

#: app_settings → tenant_settings(tenant_id=1) 复制的租户级键（T20，docs/21 §7.1
#: + docs/26-J11 补 stock_link_platform；复制不删，读路径切换归 MT-P3/4）。
_MT_TENANT_SETTING_KEYS: tuple[str, ...] = (
    "ui_avatar",
    "notify_quiet_hours",
    "notify_retry_attempts",
    "notify_retry_backoff_seconds",
    "notify_dedupe_ttl_overrides",
    "stock_link_platform",
)


def _m120_tenant_foundation(conn: Connection) -> None:
    """v120：建表 + 加列 + 回填（docs/21 §3，全部幂等可重入）。"""
    # 1. tenants / users 自包含 DDL（M3：防 create_all 未覆盖场景）。
    #    与 models.py Tenant/User（MT-P1 已上线）逐列一致。
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS tenants (
  id INTEGER NOT NULL,
  name VARCHAR NOT NULL,
  is_default BOOLEAN NOT NULL,
  status VARCHAR NOT NULL,
  max_users INTEGER NOT NULL,
  invite_code VARCHAR,
  invite_expires_at DATETIME,
  registration_enabled BOOLEAN NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
)
"""
        )
    )
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER NOT NULL,
  tenant_id INTEGER NOT NULL,
  username VARCHAR NOT NULL,
  password_hash VARCHAR NOT NULL,
  password_algo VARCHAR NOT NULL,
  role VARCHAR NOT NULL,
  quota_shared_with_admin BOOLEAN NOT NULL,
  is_active BOOLEAN NOT NULL,
  pwd_changed_at DATETIME,
  invited_by INTEGER,
  last_login_at DATETIME,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  FOREIGN KEY(tenant_id) REFERENCES tenants (id) ON DELETE CASCADE,
  UNIQUE (username),
  FOREIGN KEY(invited_by) REFERENCES users (id) ON DELETE SET NULL
)
"""
        )
    )
    _create_index_if_missing(
        conn, "ix_users_tenant", "CREATE INDEX ix_users_tenant ON users(tenant_id)"
    )

    # 2. 回填默认租户 id=1（T18；与 src/web/bootstrap.py 口径一致）。
    conn.execute(
        text(
            """
INSERT INTO tenants (id, name, is_default, status, max_users, invite_code, registration_enabled)
SELECT 1, '默认租户', 1, 'active', 5, '', 0
WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE id = 1)
"""
        )
    )
    conn.execute(
        text("UPDATE tenants SET is_default = 1 WHERE id = 1 AND is_default <> 1")
    )
    # 初始管理员由 MT-P1 bootstrap（src/web/bootstrap.py）负责创建，本迁移不重复
    # 实现凭证解析/哈希逻辑；下方 app_settings auth_* 旧键仅在 admin 已存在时删除。

    # 3. 新表：tenant_settings / tenant_news_pushed / agent_config_overrides。
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS tenant_settings (
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT DEFAULT '',
  description TEXT DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, key)
)
"""
        )
    )
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS tenant_news_pushed (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  news_id INTEGER NOT NULL REFERENCES news_cache(id) ON DELETE CASCADE,
  channel_id INTEGER,
  pushed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_tenant_news_pushed UNIQUE (tenant_id, news_id)
)
"""
        )
    )
    _create_index_if_missing(
        conn,
        "ix_tenant_news_pushed_news",
        "CREATE INDEX ix_tenant_news_pushed_news ON tenant_news_pushed(news_id)",
    )
    # docs/26-J6：override 禁含 schedule（调度 cadence 全实例唯一，T17 单 job）。
    conn.execute(
        text(
            """
CREATE TABLE IF NOT EXISTS agent_config_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  agent_name VARCHAR NOT NULL,
  enabled INTEGER,
  execution_mode VARCHAR,
  ai_model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
  notify_channel_ids JSON,
  config JSON,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_agent_override_tenant_name UNIQUE (tenant_id, agent_name)
)
"""
        )
    )

    # 4. 31 张 A 类表加 tenant_id 裸列（NOT NULL DEFAULT 1，无 FK）+ 租户索引。
    for table in MT_TENANT_TABLES_V120:
        _add_column_if_missing(
            conn,
            table,
            "tenant_id",
            f"ALTER TABLE {table} ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1",
        )
        _create_index_if_missing(
            conn,
            f"ix_{table}_tenant_id",
            f"CREATE INDEX ix_{table}_tenant_id ON {table}(tenant_id)",
        )

    # 5. paper_trading 补 account_id（T9 前置，docs/21 §3.3）。
    if _has_table(conn, "paper_trading_positions"):
        _add_column_if_missing(
            conn,
            "paper_trading_positions",
            "account_id",
            "ALTER TABLE paper_trading_positions ADD COLUMN account_id INTEGER",
        )
    if _has_table(conn, "paper_trading_trades"):
        _add_column_if_missing(
            conn,
            "paper_trading_trades",
            "account_id",
            "ALTER TABLE paper_trading_trades ADD COLUMN account_id INTEGER",
        )
    if _has_table(conn, "paper_trading_account"):
        pending = 0
        if _has_column(conn, "paper_trading_positions", "account_id"):
            pending += int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM paper_trading_positions WHERE account_id IS NULL"
                    )
                ).scalar()
                or 0
            )
        if _has_column(conn, "paper_trading_trades", "account_id"):
            pending += int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM paper_trading_trades WHERE account_id IS NULL"
                    )
                ).scalar()
                or 0
            )
        account_id = conn.execute(
            text("SELECT MIN(id) FROM paper_trading_account")
        ).scalar()
        if account_id is None and pending > 0:
            # 有持仓/流水但无账户：建默认账户承接回填（docs/21 §3.3 任务要求）。
            conn.execute(
                text(
                    """
INSERT INTO paper_trading_account (
  initial_capital, current_capital, total_pnl, total_trades, winning_trades,
  max_drawdown_pct, peak_capital, enabled, excluded_markets, market_allocations
) VALUES (1000000.0, 1000000.0, 0.0, 0, 0, 0.0, 1000000.0, 1, '[]', '{}')
"""
                )
            )
        if _has_column(conn, "paper_trading_positions", "account_id"):
            conn.execute(
                text(
                    """
UPDATE paper_trading_positions
SET account_id = (SELECT MIN(id) FROM paper_trading_account)
WHERE account_id IS NULL
"""
                )
            )
        if _has_column(conn, "paper_trading_trades", "account_id"):
            conn.execute(
                text(
                    """
UPDATE paper_trading_trades
SET account_id = (SELECT MIN(id) FROM paper_trading_account)
WHERE account_id IS NULL
"""
                )
            )
        _create_index_if_missing(
            conn,
            "uq_paper_account_tenant",
            "CREATE UNIQUE INDEX uq_paper_account_tenant ON paper_trading_account(tenant_id)",
        )

    # 6. notify_channels.is_shared（T21 托管渠道引用，docs/21 §3.4）。
    _add_column_if_missing(
        conn,
        "notify_channels",
        "is_shared",
        "ALTER TABLE notify_channels ADD COLUMN is_shared INTEGER NOT NULL DEFAULT 0",
    )

    # 7. app_settings 三分（T20，docs/21 §7.1）。
    #    a) 租户级键复制（不删）到 tenant_settings(tenant_id=1)。
    keys_csv = ", ".join(f"'{k}'" for k in _MT_TENANT_SETTING_KEYS)
    conn.execute(
        text(
            f"""
INSERT OR IGNORE INTO tenant_settings (tenant_id, key, value, description)
SELECT 1, key, value, description FROM app_settings
WHERE key IN ({keys_csv})
"""
        )
    )
    #    b) 单租户回退 flag（实例级，默认 '1'）。
    conn.execute(
        text(
            """
INSERT INTO app_settings (key, value, description)
SELECT 'single_tenant_mode', '1', '单租户直通模式（T20 回退 flag）：1=单租户'
WHERE NOT EXISTS (SELECT 1 FROM app_settings WHERE key = 'single_tenant_mode')
"""
        )
    )
    #    c) auth_username/auth_password_hash 已搬入 users（MT-P1 bootstrap）→
    #       仅在 admin 已存在时删除旧行；jwt_secret/http_proxy/panwatch_base_url
    #       保留原位（实例级）。
    conn.execute(
        text(
            """
DELETE FROM app_settings
WHERE key IN ('auth_username', 'auth_password_hash')
  AND EXISTS (SELECT 1 FROM users WHERE role = 'admin')
"""
        )
    )

    # 8. notify_throttle 旧行 scope 回填（docs/23 §3.3，幂等 UPDATE；不重建 UQ）。
    if _has_table(conn, "notify_throttle"):
        conn.execute(
            text(
                """
UPDATE notify_throttle
SET stock_symbol = '__notify__:1:' || substr(stock_symbol, 12)
WHERE stock_symbol LIKE '__notify__:%'
  AND stock_symbol NOT LIKE '__notify__:1:%'
"""
            )
        )
        conn.execute(
            text(
                """
UPDATE notify_throttle
SET stock_symbol = '1:' || stock_symbol
WHERE stock_symbol NOT LIKE '__notify__:%'
  AND stock_symbol NOT LIKE '%:%'
"""
            )
        )


@dataclass(frozen=True)
class _RebuildSpec:
    """v121 单表 __new 重建规格（docs/21 §4 模板）。"""

    table: str
    create_sql: str  # CREATE TABLE {table}__new 完整 DDL
    columns: tuple[str, ...]  # INSERT SELECT 显式列清单（含 tenant_id）
    done_marker: str  # 已重建判定：规范化后的 DDL 子串
    indexes: tuple[tuple[str, str], ...]  # (索引名, CREATE INDEX SQL) 全量清单


def _normalize_sql(sql: str) -> str:
    return " ".join(str(sql).split()).lower()


def _table_ddl(conn: Connection, table: str) -> str:
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:table"),
        {"table": table},
    ).first()
    return str(row[0]) if row and row[0] else ""


def _rebuild_insert_sql(spec: _RebuildSpec) -> str:
    cols = ", ".join(spec.columns)
    exprs = ", ".join(
        "COALESCE(tenant_id, 1)" if col == "tenant_id" else col
        for col in spec.columns
    )
    return (
        f"INSERT INTO {spec.table}__new ({cols}) "
        f"SELECT {exprs} FROM {spec.table}"
    )


def _apply_rebuild(conn: Connection, spec: _RebuildSpec) -> None:
    """按 docs/21 §4 统一工序重建一张表（须在逐表事务内调用，幂等）。"""
    table = spec.table
    # 0. 清半成品（M3 断点重跑要求）。
    conn.execute(text(f"DROP TABLE IF EXISTS {table}__new"))

    if not _has_table(conn, table):
        # 防御：基表不存在时直接以新 DDL 建正式表。
        conn.execute(text(spec.create_sql.replace(f"{table}__new", table)))
    elif _normalize_sql(spec.done_marker) not in _normalize_sql(
        _table_ddl(conn, table)
    ):
        # 2. 建新表 → 3. 显式列拷贝 → 4. 行数对账 → 5. 换名。
        conn.execute(text(spec.create_sql))
        conn.execute(text(_rebuild_insert_sql(spec)))
        old_count = int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
        )
        new_count = int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}__new")).scalar() or 0
        )
        if old_count != new_count:
            raise RuntimeError(
                f"v121 重建对账失败: {table} 旧 {old_count} 行 != 新 {new_count} 行"
            )
        conn.execute(text(f"DROP TABLE {table}"))
        conn.execute(text(f"ALTER TABLE {table}__new RENAME TO {table}"))
    # 6. 重建索引（全量声明，IF NOT EXISTS 幂等；旧表 DROP 会连带删索引）。
    for name, sql in spec.indexes:
        _create_index_if_missing(conn, name, sql)


_V121_REBUILD_SPECS: tuple[_RebuildSpec, ...] = (
    _RebuildSpec(
        table="analysis_history",
        create_sql="""
CREATE TABLE analysis_history__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  agent_name VARCHAR NOT NULL,
  stock_symbol VARCHAR NOT NULL,
  analysis_date VARCHAR NOT NULL,
  title VARCHAR DEFAULT '',
  content VARCHAR NOT NULL,
  raw_data JSON DEFAULT '{}',
  agent_kind_snapshot VARCHAR DEFAULT 'workflow',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_agent_stock_date UNIQUE (tenant_id, agent_name, stock_symbol, analysis_date)
)
""",
        columns=(
            "id",
            "tenant_id",
            "agent_name",
            "stock_symbol",
            "analysis_date",
            "title",
            "content",
            "raw_data",
            "agent_kind_snapshot",
            "created_at",
            "updated_at",
        ),
        done_marker="UNIQUE (tenant_id, agent_name, stock_symbol, analysis_date)",
        indexes=(
            (
                "ix_analysis_history_kind_date",
                "CREATE INDEX ix_analysis_history_kind_date ON analysis_history(agent_kind_snapshot, analysis_date)",
            ),
            (
                "ix_analysis_history_agent_updated",
                "CREATE INDEX ix_analysis_history_agent_updated ON analysis_history(agent_name, updated_at)",
            ),
            (
                "ix_analysis_history_tenant_id",
                "CREATE INDEX ix_analysis_history_tenant_id ON analysis_history(tenant_id)",
            ),
        ),
    ),
    _RebuildSpec(
        table="stock_context_snapshots",
        create_sql="""
CREATE TABLE stock_context_snapshots__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  symbol VARCHAR NOT NULL,
  market VARCHAR NOT NULL,
  snapshot_date VARCHAR NOT NULL,
  context_type VARCHAR NOT NULL,
  payload JSON DEFAULT '{}',
  quality JSON DEFAULT '{}',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_stock_context_snapshot UNIQUE (tenant_id, symbol, market, snapshot_date, context_type)
)
""",
        columns=(
            "id",
            "tenant_id",
            "symbol",
            "market",
            "snapshot_date",
            "context_type",
            "payload",
            "quality",
            "created_at",
        ),
        done_marker="UNIQUE (tenant_id, symbol, market, snapshot_date, context_type)",
        indexes=(
            (
                "ix_stock_context_symbol_date",
                "CREATE INDEX ix_stock_context_symbol_date ON stock_context_snapshots(symbol, market, snapshot_date)",
            ),
            (
                "ix_stock_context_snapshots_tenant_id",
                "CREATE INDEX ix_stock_context_snapshots_tenant_id ON stock_context_snapshots(tenant_id)",
            ),
        ),
    ),
    _RebuildSpec(
        table="news_topic_snapshots",
        create_sql="""
CREATE TABLE news_topic_snapshots__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  snapshot_date VARCHAR NOT NULL,
  window_days INTEGER NOT NULL DEFAULT 7,
  symbols JSON DEFAULT '[]',
  summary VARCHAR DEFAULT '',
  topics JSON DEFAULT '[]',
  sentiment VARCHAR DEFAULT 'neutral',
  coverage JSON DEFAULT '{}',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_news_topic_snapshot_date_window UNIQUE (tenant_id, snapshot_date, window_days)
)
""",
        columns=(
            "id",
            "tenant_id",
            "snapshot_date",
            "window_days",
            "symbols",
            "summary",
            "topics",
            "sentiment",
            "coverage",
            "created_at",
        ),
        done_marker="UNIQUE (tenant_id, snapshot_date, window_days)",
        indexes=(
            (
                "ix_news_topic_snapshot_date",
                "CREATE INDEX ix_news_topic_snapshot_date ON news_topic_snapshots(snapshot_date)",
            ),
            (
                "ix_news_topic_snapshots_tenant_id",
                "CREATE INDEX ix_news_topic_snapshots_tenant_id ON news_topic_snapshots(tenant_id)",
            ),
        ),
    ),
    _RebuildSpec(
        table="entry_candidates",
        create_sql="""
CREATE TABLE entry_candidates__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  stock_symbol VARCHAR NOT NULL,
  stock_market VARCHAR NOT NULL DEFAULT 'CN',
  stock_name VARCHAR DEFAULT '',
  snapshot_date VARCHAR NOT NULL,
  status VARCHAR DEFAULT 'active',
  score FLOAT NOT NULL DEFAULT 0,
  confidence FLOAT,
  action VARCHAR NOT NULL DEFAULT 'watch',
  action_label VARCHAR NOT NULL DEFAULT '观望',
  signal VARCHAR DEFAULT '',
  reason VARCHAR DEFAULT '',
  candidate_source VARCHAR NOT NULL DEFAULT 'watchlist',
  strategy_tags JSON DEFAULT '[]',
  is_holding_snapshot BOOLEAN DEFAULT 0,
  plan_quality INTEGER DEFAULT 0,
  entry_low FLOAT,
  entry_high FLOAT,
  stop_loss FLOAT,
  target_price FLOAT,
  invalidation VARCHAR DEFAULT '',
  source_agent VARCHAR DEFAULT '',
  source_suggestion_id INTEGER,
  source_trace_id VARCHAR DEFAULT '',
  evidence JSON DEFAULT '[]',
  "plan" JSON DEFAULT '{}',
  meta JSON DEFAULT '{}',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_entry_candidate_stock_date UNIQUE (tenant_id, stock_symbol, stock_market, snapshot_date)
)
""",
        columns=(
            "id",
            "tenant_id",
            "stock_symbol",
            "stock_market",
            "stock_name",
            "snapshot_date",
            "status",
            "score",
            "confidence",
            "action",
            "action_label",
            "signal",
            "reason",
            "candidate_source",
            "strategy_tags",
            "is_holding_snapshot",
            "plan_quality",
            "entry_low",
            "entry_high",
            "stop_loss",
            "target_price",
            "invalidation",
            "source_agent",
            "source_suggestion_id",
            "source_trace_id",
            "evidence",
            '"plan"',
            "meta",
            "created_at",
            "updated_at",
        ),
        done_marker="UNIQUE (tenant_id, stock_symbol, stock_market, snapshot_date)",
        indexes=(
            (
                "ix_entry_candidate_score_date",
                "CREATE INDEX ix_entry_candidate_score_date ON entry_candidates(snapshot_date, score)",
            ),
            (
                "ix_entry_candidate_status_updated",
                "CREATE INDEX ix_entry_candidate_status_updated ON entry_candidates(status, updated_at)",
            ),
            (
                "ix_entry_candidate_source_score",
                "CREATE INDEX ix_entry_candidate_source_score ON entry_candidates(candidate_source, score)",
            ),
            (
                "ix_entry_candidate_market_status",
                "CREATE INDEX ix_entry_candidate_market_status ON entry_candidates(stock_market, status)",
            ),
            (
                "ix_entry_candidates_tenant_id",
                "CREATE INDEX ix_entry_candidates_tenant_id ON entry_candidates(tenant_id)",
            ),
        ),
    ),
    _RebuildSpec(
        table="stocks",
        create_sql="""
CREATE TABLE stocks__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  symbol VARCHAR NOT NULL,
  name VARCHAR NOT NULL,
  market VARCHAR NOT NULL,
  cost_price FLOAT,
  quantity INTEGER,
  invested_amount FLOAT,
  sort_order INTEGER DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_stocks_tenant_symbol_market UNIQUE (tenant_id, symbol, market)
)
""",
        columns=(
            "id",
            "tenant_id",
            "symbol",
            "name",
            "market",
            "cost_price",
            "quantity",
            "invested_amount",
            "sort_order",
            "created_at",
            "updated_at",
        ),
        done_marker="uq_stocks_tenant_symbol_market",
        indexes=(
            (
                "ix_stocks_tenant_id",
                "CREATE INDEX ix_stocks_tenant_id ON stocks(tenant_id)",
            ),
        ),
    ),
    _RebuildSpec(
        table="paper_trading_positions",
        create_sql="""
CREATE TABLE paper_trading_positions__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  account_id INTEGER NOT NULL REFERENCES paper_trading_account(id) ON DELETE CASCADE,
  stock_symbol VARCHAR NOT NULL,
  stock_market VARCHAR NOT NULL DEFAULT 'CN',
  stock_name VARCHAR DEFAULT '',
  quantity INTEGER NOT NULL DEFAULT 100,
  entry_price FLOAT NOT NULL,
  stop_loss FLOAT,
  target_price FLOAT,
  current_price FLOAT,
  highest_price FLOAT,
  unrealized_pnl FLOAT NOT NULL DEFAULT 0.0,
  status VARCHAR NOT NULL DEFAULT 'open',
  signal_run_id INTEGER,
  signal_snapshot_date VARCHAR DEFAULT '',
  signal_action VARCHAR DEFAULT '',
  strategy_code VARCHAR DEFAULT '',
  opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  closed_at DATETIME,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
""",
        columns=(
            "id",
            "tenant_id",
            "account_id",
            "stock_symbol",
            "stock_market",
            "stock_name",
            "quantity",
            "entry_price",
            "stop_loss",
            "target_price",
            "current_price",
            "highest_price",
            "unrealized_pnl",
            "status",
            "signal_run_id",
            "signal_snapshot_date",
            "signal_action",
            "strategy_code",
            "opened_at",
            "closed_at",
            "updated_at",
        ),
        done_marker="REFERENCES paper_trading_account",
        indexes=(
            (
                "ix_paper_pos_status",
                "CREATE INDEX ix_paper_pos_status ON paper_trading_positions(status)",
            ),
            (
                "ix_paper_pos_symbol_market",
                "CREATE INDEX ix_paper_pos_symbol_market ON paper_trading_positions(stock_symbol, stock_market)",
            ),
            (
                "ix_paper_pos_account_status",
                "CREATE INDEX ix_paper_pos_account_status ON paper_trading_positions(account_id, status)",
            ),
            (
                "ix_paper_trading_positions_tenant_id",
                "CREATE INDEX ix_paper_trading_positions_tenant_id ON paper_trading_positions(tenant_id)",
            ),
        ),
    ),
    _RebuildSpec(
        table="paper_trading_trades",
        create_sql="""
CREATE TABLE paper_trading_trades__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  account_id INTEGER NOT NULL REFERENCES paper_trading_account(id) ON DELETE CASCADE,
  stock_symbol VARCHAR NOT NULL,
  stock_market VARCHAR NOT NULL DEFAULT 'CN',
  stock_name VARCHAR DEFAULT '',
  quantity INTEGER NOT NULL DEFAULT 100,
  entry_price FLOAT NOT NULL,
  exit_price FLOAT NOT NULL,
  pnl FLOAT NOT NULL DEFAULT 0.0,
  pnl_pct FLOAT NOT NULL DEFAULT 0.0,
  exit_reason VARCHAR NOT NULL DEFAULT '',
  signal_run_id INTEGER,
  signal_snapshot_date VARCHAR DEFAULT '',
  strategy_code VARCHAR DEFAULT '',
  holding_days INTEGER DEFAULT 0,
  opened_at DATETIME,
  closed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  meta JSON DEFAULT '{}'
)
""",
        columns=(
            "id",
            "tenant_id",
            "account_id",
            "stock_symbol",
            "stock_market",
            "stock_name",
            "quantity",
            "entry_price",
            "exit_price",
            "pnl",
            "pnl_pct",
            "exit_reason",
            "signal_run_id",
            "signal_snapshot_date",
            "strategy_code",
            "holding_days",
            "opened_at",
            "closed_at",
            "meta",
        ),
        done_marker="REFERENCES paper_trading_account",
        indexes=(
            (
                "ix_paper_trade_closed",
                "CREATE INDEX ix_paper_trade_closed ON paper_trading_trades(closed_at)",
            ),
            (
                "ix_paper_trade_symbol",
                "CREATE INDEX ix_paper_trade_symbol ON paper_trading_trades(stock_symbol, stock_market)",
            ),
            (
                "ix_paper_trade_account_closed",
                "CREATE INDEX ix_paper_trade_account_closed ON paper_trading_trades(account_id, closed_at)",
            ),
            (
                "ix_paper_trading_trades_tenant_id",
                "CREATE INDEX ix_paper_trading_trades_tenant_id ON paper_trading_trades(tenant_id)",
            ),
        ),
    ),
)


def _m121_tenant_constraint_rebuild(conn: Connection) -> None:
    """v121：7 张表 __new 约束重建（docs/21 §4）。

    本迁移以 transactional=False 注册（独立连接执行）：PRAGMA foreign_keys
    在事务内是 no-op（docs/17 R4），必须先关闭再重建，否则 DROP 父表
    （stocks/entry_candidates）会级联误删子表；每表独立事务，断点重跑安全。
    """
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.commit()
    try:
        for spec in _V121_REBUILD_SPECS:
            with conn.begin():
                _apply_rebuild(conn, spec)
    finally:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()


#: v122 子行 tenant 仅父派生不变量（docs/21 §5.2；末条为 docs/26-J2 父派生补充）。
_V122_INVARIANTS: tuple[tuple[str, str], ...] = (
    (
        "positions.tenant != accounts.tenant",
        "SELECT COUNT(*) FROM positions p JOIN accounts a ON p.account_id=a.id "
        "WHERE p.tenant_id <> a.tenant_id",
    ),
    (
        "position_trades.tenant != positions.tenant",
        "SELECT COUNT(*) FROM position_trades t JOIN positions p ON t.position_id=p.id "
        "WHERE t.tenant_id <> p.tenant_id",
    ),
    (
        "positions.tenant != stocks.tenant",
        "SELECT COUNT(*) FROM positions p JOIN stocks s ON p.stock_id=s.id "
        "WHERE p.tenant_id <> s.tenant_id",
    ),
    (
        "stock_agents.tenant != stocks.tenant",
        "SELECT COUNT(*) FROM stock_agents x JOIN stocks s ON x.stock_id=s.id "
        "WHERE x.tenant_id <> s.tenant_id",
    ),
    (
        "price_alert_rules.tenant != stocks.tenant",
        "SELECT COUNT(*) FROM price_alert_rules r JOIN stocks s ON r.stock_id=s.id "
        "WHERE r.tenant_id <> s.tenant_id",
    ),
    (
        "price_alert_hits.tenant != price_alert_rules.tenant",
        "SELECT COUNT(*) FROM price_alert_hits h JOIN price_alert_rules r ON h.rule_id=r.id "
        "WHERE h.tenant_id <> r.tenant_id",
    ),
    (
        "stock_playbooks.tenant != stocks.tenant",
        "SELECT COUNT(*) FROM stock_playbooks b JOIN stocks s ON b.stock_id=s.id "
        "WHERE b.tenant_id <> s.tenant_id",
    ),
    (
        "ai_models.tenant != ai_services.tenant",
        "SELECT COUNT(*) FROM ai_models m JOIN ai_services s ON m.service_id=s.id "
        "WHERE m.tenant_id <> s.tenant_id",
    ),
    (
        "entry_candidate_outcomes.tenant != entry_candidates.tenant",
        "SELECT COUNT(*) FROM entry_candidate_outcomes o JOIN entry_candidates c ON o.candidate_id=c.id "
        "WHERE o.tenant_id <> c.tenant_id",
    ),
    (
        "suggestion_feedback.tenant != stock_suggestions.tenant",
        "SELECT COUNT(*) FROM suggestion_feedback f JOIN stock_suggestions s ON f.suggestion_id=s.id "
        "WHERE f.tenant_id <> s.tenant_id",
    ),
    (
        "paper_trading_positions.tenant != paper_trading_account.tenant",
        "SELECT COUNT(*) FROM paper_trading_positions p JOIN paper_trading_account a ON p.account_id=a.id "
        "WHERE p.tenant_id <> a.tenant_id",
    ),
    (
        "paper_trading_trades.tenant != paper_trading_account.tenant",
        "SELECT COUNT(*) FROM paper_trading_trades t JOIN paper_trading_account a ON t.account_id=a.id "
        "WHERE t.tenant_id <> a.tenant_id",
    ),
    (
        "chat_messages.tenant != chat_conversations.tenant",
        "SELECT COUNT(*) FROM chat_messages m JOIN chat_conversations c ON m.conversation_id=c.id "
        "WHERE m.tenant_id <> c.tenant_id",
    ),
    (
        "users.tenant_id 指向不存在租户",
        "SELECT COUNT(*) FROM users u LEFT JOIN tenants t ON u.tenant_id=t.id "
        "WHERE t.id IS NULL",
    ),
    (
        "strategy_outcomes.tenant != strategy_signal_runs.tenant",
        "SELECT COUNT(*) FROM strategy_outcomes o JOIN strategy_signal_runs r ON o.signal_run_id=r.id "
        "WHERE o.tenant_id <> r.tenant_id",
    ),
)


def _m122_tenant_reconciliation(conn: Connection) -> None:
    """v122：对账（docs/21 §5）。独立连接执行，任一检查失配即 raise 阻断启动。

    数据锚点（2150 股@112.572 / 5 笔流水 / playbook id=1）为生产库验收锚点
    （C5/M7）：仅在对应基表非空（即升级自含该数据的存量库）时强制核对，
    全新/测试库自动跳过；schema 级不变量（FK、tenant 归因、孤儿租户）无条件强制。
    """
    failures: list[str] = []
    engine = conn.engine
    with engine.connect() as chk:
        # 5.1 外键一致性
        fk_rows = chk.execute(text("PRAGMA foreign_key_check")).fetchall()
        if fk_rows:
            failures.append(f"foreign_key_check 返回 {len(fk_rows)} 行: {fk_rows[:5]}")

        # 5.2 子行 tenant 仅父派生不变量
        for label, sql in _V122_INVARIANTS:
            count = int(chk.execute(text(sql)).scalar() or 0)
            if count:
                failures.append(f"不变量违背[{label}]: {count} 行")

        # 锚点 4：31 张 A 类表 tenant_id 无 NULL 且无孤儿租户
        # （哨兵表允许 tenant_id=0，docs/26-J2/J3/J11）。
        for table in MT_TENANT_TABLES_V120:
            if not _has_table(chk, table):
                continue
            nulls = int(
                chk.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id IS NULL")
                ).scalar()
                or 0
            )
            if nulls:
                failures.append(f"{table}: {nulls} 行 tenant_id IS NULL")
            if table in MT_SENTINEL_ZERO_TABLES:
                orphans = int(
                    chk.execute(
                        text(
                            f"SELECT COUNT(*) FROM {table} "
                            "WHERE tenant_id <> 0 "
                            "AND tenant_id NOT IN (SELECT id FROM tenants)"
                        )
                    ).scalar()
                    or 0
                )
            else:
                orphans = int(
                    chk.execute(
                        text(
                            f"SELECT COUNT(*) FROM {table} "
                            "WHERE tenant_id NOT IN (SELECT id FROM tenants)"
                        )
                    ).scalar()
                    or 0
                )
            if orphans:
                failures.append(f"{table}: {orphans} 行 tenant_id 无对应租户")

        # 5.3 一致性锚点（C5/M7，存量库强制、全新库跳过）
        if int(chk.execute(text("SELECT COUNT(*) FROM positions")).scalar() or 0):
            anchor1 = int(
                chk.execute(
                    text(
                        "SELECT COUNT(*) FROM positions "
                        "WHERE quantity = 2150 AND ABS(cost_price - 112.572) < 0.001"
                    )
                ).scalar()
                or 0
            )
            if anchor1 < 1:
                failures.append("锚点1: 默认账户 2150 股@112.572 持仓不存在")
            else:
                anchor2 = int(
                    chk.execute(
                        text(
                            "SELECT COUNT(*) FROM position_trades t "
                            "JOIN positions p ON t.position_id = p.id "
                            "WHERE p.quantity = 2150 "
                            "AND ABS(p.cost_price - 112.572) < 0.001"
                        )
                    ).scalar()
                    or 0
                )
                if anchor2 != 5:
                    failures.append(f"锚点2: 锚点持仓流水 {anchor2} 笔 != 5 笔")
        playbook = chk.execute(
            text("SELECT is_active FROM stock_playbooks WHERE id = 1")
        ).first()
        if playbook is not None and int(playbook[0] or 0) != 1:
            failures.append("锚点3: playbook id=1 非激活态")
        if not int(
            chk.execute(text("SELECT COUNT(*) FROM tenants WHERE id = 1")).scalar()
            or 0
        ):
            failures.append("锚点5: tenants 缺默认租户 id=1")
        if int(chk.execute(text("SELECT COUNT(*) FROM users")).scalar() or 0):
            admins = int(
                chk.execute(
                    text(
                        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND tenant_id = 1"
                    )
                ).scalar()
                or 0
            )
            if admins < 1:
                failures.append("锚点5: users 无 tenant_id=1 的 admin")

    if failures:
        raise RuntimeError("v122 多租户对账失败: " + "；".join(failures))


#: v123 重建规格：strategy_signal_runs 的 UQ 补 tenant_id 前缀（MT-P3 集成裁决，
#: docs/26-J2 落地时遗漏——两租户同日同标的同策略信号会在
#: uq_strategy_signal_daily_unique 上冲突丢行）。v121 已发布不可改（checksum 钉死）。
_V123_REBUILD_SPEC = _RebuildSpec(
    table="strategy_signal_runs",
    create_sql="""
CREATE TABLE strategy_signal_runs__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  snapshot_date VARCHAR NOT NULL,
  stock_symbol VARCHAR NOT NULL,
  stock_market VARCHAR NOT NULL DEFAULT 'CN',
  stock_name VARCHAR DEFAULT '',
  strategy_code VARCHAR NOT NULL,
  strategy_name VARCHAR DEFAULT '',
  strategy_version VARCHAR DEFAULT 'v1',
  risk_level VARCHAR DEFAULT 'medium',
  source_pool VARCHAR DEFAULT 'watchlist',
  score FLOAT NOT NULL DEFAULT 0,
  rank_score FLOAT NOT NULL DEFAULT 0,
  confidence FLOAT,
  status VARCHAR DEFAULT 'active',
  action VARCHAR DEFAULT 'watch',
  action_label VARCHAR DEFAULT '观望',
  signal VARCHAR DEFAULT '',
  reason VARCHAR DEFAULT '',
  evidence JSON,
  holding_days INTEGER DEFAULT 3,
  entry_low FLOAT,
  entry_high FLOAT,
  stop_loss FLOAT,
  target_price FLOAT,
  invalidation VARCHAR DEFAULT '',
  plan_quality INTEGER DEFAULT 0,
  source_agent VARCHAR DEFAULT '',
  source_suggestion_id INTEGER,
  source_candidate_id INTEGER,
  trace_id VARCHAR DEFAULT '',
  is_holding_snapshot BOOLEAN DEFAULT 0,
  context_quality_score FLOAT,
  payload JSON,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_strategy_signal_daily_unique UNIQUE (tenant_id, snapshot_date, stock_symbol, stock_market, strategy_code, source_candidate_id)
)
""",
    columns=(
        "id",
        "tenant_id",
        "snapshot_date",
        "stock_symbol",
        "stock_market",
        "stock_name",
        "strategy_code",
        "strategy_name",
        "strategy_version",
        "risk_level",
        "source_pool",
        "score",
        "rank_score",
        "confidence",
        "status",
        "action",
        "action_label",
        "signal",
        "reason",
        "evidence",
        "holding_days",
        "entry_low",
        "entry_high",
        "stop_loss",
        "target_price",
        "invalidation",
        "plan_quality",
        "source_agent",
        "source_suggestion_id",
        "source_candidate_id",
        "trace_id",
        "is_holding_snapshot",
        "context_quality_score",
        "payload",
        "created_at",
        "updated_at",
    ),
    done_marker="tenant_id, snapshot_date, stock_symbol, stock_market, strategy_code, source_candidate_id",
    indexes=(
        (
            "ix_strategy_signal_runs_tenant_id",
            "CREATE INDEX ix_strategy_signal_runs_tenant_id ON strategy_signal_runs(tenant_id)",
        ),
        (
            "ix_strategy_signal_snapshot_rank",
            "CREATE INDEX ix_strategy_signal_snapshot_rank ON strategy_signal_runs(snapshot_date, rank_score)",
        ),
        (
            "ix_strategy_signal_strategy_market",
            "CREATE INDEX ix_strategy_signal_strategy_market ON strategy_signal_runs(strategy_code, stock_market)",
        ),
        (
            "ix_strategy_signal_status",
            "CREATE INDEX ix_strategy_signal_status ON strategy_signal_runs(status, updated_at)",
        ),
    ),
)


def _m123_strategy_signal_uq_tenant(conn: Connection) -> None:
    """v123：strategy_signal_runs UQ 重建为含 tenant_id（MT-P3 集成裁决）。

    同 v121 以 transactional=False 注册：strategy_outcomes 以 FK CASCADE 挂在
    本表上，须先关 PRAGMA foreign_keys 再 DROP，否则级联误删子表。
    done_marker 匹配重建后的 UQ 定义（幂等：已重建则跳过）。
    """
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.commit()
    try:
        with conn.begin():
            _apply_rebuild(conn, _V123_REBUILD_SPEC)
    finally:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()


MIGRATIONS: tuple[Migration, ...] = (
    Migration(101, "agent_config_kind_and_visibility", _m101_agent_config_kind),
    Migration(102, "backfill_agent_kind_data", _m102_backfill_agent_kind),
    Migration(103, "agent_run_observability_fields", _m103_agent_run_observability),
    Migration(104, "analysis_history_kind_snapshot", _m104_history_kind_snapshot),
    Migration(105, "indexes_for_agent_kind_and_history", _m105_indexes),
    Migration(106, "log_entry_observability_fields", _m106_log_observability),
    Migration(107, "stock_suggestion_market_dimension", _m107_suggestion_market_dimension),
    Migration(108, "entry_candidates_table", _m108_entry_candidates_table),
    Migration(109, "entry_candidate_upgrade", _m109_entry_candidate_upgrade),
    Migration(110, "entry_candidate_outcomes", _m110_entry_candidate_outcomes),
    Migration(111, "strategy_layer", _m111_strategy_layer),
    Migration(112, "strategy_analytics_snapshots", _m112_strategy_analytics_snapshots),
    Migration(113, "market_scan_snapshot_and_mixed_source", _m113_market_scan_snapshot_and_mixed_source),
    Migration(114, "paper_trading_tables", _m114_paper_trading_tables),
    Migration(115, "paper_trading_excluded_markets", _m115_paper_trading_excluded_markets),
    Migration(116, "chat_tables", _m116_chat_tables),
    Migration(117, "chat_initial_context", _m117_chat_initial_context),
    Migration(118, "paper_trading_market_allocations", _m118_paper_trading_market_allocations),
    Migration(119, "price_alert_rule_playbook_id", _m119_price_alert_rule_playbook_id),
    Migration(120, "tenant_foundation_columns_backfill", _m120_tenant_foundation),
    Migration(
        121,
        "tenant_constraint_rebuild",
        _m121_tenant_constraint_rebuild,
        transactional=False,
    ),
    Migration(122, "tenant_reconciliation", _m122_tenant_reconciliation),
    Migration(
        123,
        "strategy_signal_uq_tenant",
        _m123_strategy_signal_uq_tenant,
        transactional=False,
    ),
)


def _get_applied(conn: Connection, version: int) -> tuple[int, str, int] | None:
    row = conn.execute(
        text(
            """
SELECT version, checksum, success
FROM schema_migrations
WHERE version = :version
LIMIT 1
"""
        ),
        {"version": version},
    ).first()
    if not row:
        return None
    return int(row[0]), str(row[1]), int(row[2])


def has_pending_migrations(engine: Engine) -> bool:
    with engine.begin() as conn:
        _ensure_schema_table(conn)
        for m in MIGRATIONS:
            rec = _get_applied(conn, m.version)
            if not rec or rec[2] != 1 or rec[1] != m.checksum:
                return True
    return False


def run_versioned_migrations(engine: Engine) -> None:
    with engine.begin() as conn:
        _ensure_schema_table(conn)

    for m in MIGRATIONS:
        if not m.transactional:
            _run_nontransactional_migration(engine, m)
            continue
        with engine.begin() as conn:
            _ensure_schema_table(conn)
            rec = _get_applied(conn, m.version)
            if rec and rec[2] == 1 and rec[1] == m.checksum:
                continue

            conn.execute(
                text(
                    """
INSERT INTO schema_migrations(version, name, checksum, success, error)
VALUES(:version, :name, :checksum, 0, '')
ON CONFLICT(version) DO UPDATE SET
  name = excluded.name,
  checksum = excluded.checksum,
  success = 0,
  error = ''
"""
                ),
                {
                    "version": m.version,
                    "name": m.name,
                    "checksum": m.checksum,
                },
            )
            logger.info("Applying migration v%s: %s", m.version, m.name)

            try:
                m.runner(conn)
                conn.execute(
                    text(
                        """
UPDATE schema_migrations
SET success = 1,
    error = '',
    applied_at = CURRENT_TIMESTAMP
WHERE version = :version
"""
                    ),
                    {"version": m.version},
                )
            except Exception as exc:
                conn.execute(
                    text(
                        """
UPDATE schema_migrations
SET success = 0,
    error = :error,
    applied_at = CURRENT_TIMESTAMP
WHERE version = :version
"""
                    ),
                    {"version": m.version, "error": str(exc)[:2000]},
                )
                logger.exception("Migration v%s failed: %s", m.version, m.name)
                raise


def _run_nontransactional_migration(engine: Engine, m: Migration) -> None:
    """非事务型迁移入口（v121+ 的 __new 重建）。

    runner 需要在连接级、事务外切换 PRAGMA foreign_keys（docs/17 R4），因此
    不能用 engine.begin() 包裹；账本 upsert 与成败落库各自独立提交，runner
    内部按表自建事务，崩溃后按 checksum/success 记账重跑，断点安全。
    """
    with engine.connect() as conn:
        _ensure_schema_table(conn)
        conn.commit()
        rec = _get_applied(conn, m.version)
        if rec and rec[2] == 1 and rec[1] == m.checksum:
            return

        conn.execute(
            text(
                """
INSERT INTO schema_migrations(version, name, checksum, success, error)
VALUES(:version, :name, :checksum, 0, '')
ON CONFLICT(version) DO UPDATE SET
  name = excluded.name,
  checksum = excluded.checksum,
  success = 0,
  error = ''
"""
            ),
            {
                "version": m.version,
                "name": m.name,
                "checksum": m.checksum,
            },
        )
        conn.commit()
        logger.info("Applying migration v%s: %s", m.version, m.name)

        try:
            m.runner(conn)
            conn.execute(
                text(
                    """
UPDATE schema_migrations
SET success = 1,
    error = '',
    applied_at = CURRENT_TIMESTAMP
WHERE version = :version
"""
                ),
                {"version": m.version},
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            conn.execute(
                text(
                    """
UPDATE schema_migrations
SET success = 0,
    error = :error,
    applied_at = CURRENT_TIMESTAMP
WHERE version = :version
"""
                ),
                {"version": m.version, "error": str(exc)[:2000]},
            )
            conn.commit()
            logger.exception("Migration v%s failed: %s", m.version, m.name)
            raise
