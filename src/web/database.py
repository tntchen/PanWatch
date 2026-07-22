import glob
import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import NullPool

from src.web.migrations import has_pending_migrations, run_versioned_migrations

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "panwatch.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={
        "timeout": 30,
        "check_same_thread": False,
    },
    poolclass=NullPool,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate(engine)
    _migrate_old_providers(engine)
    _migrate_settings_to_models(engine)
    _migrate_positions_to_accounts(engine)
    _migrate_remove_stock_enabled(engine)
    if has_pending_migrations(engine):
        _backup_db_before_migration()
    run_versioned_migrations(engine)


def _has_column(conn, table: str, column: str) -> bool:
    try:
        conn.execute(text(f"SELECT {column} FROM {table} LIMIT 1"))
        return True
    except Exception:
        return False


def _has_table(conn, table: str) -> bool:
    try:
        conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        return True
    except Exception:
        return False


def _drop_dangling_ai_provider_fk(conn, table: str) -> None:
    """删掉指向已不存在的 ai_providers 表的悬空外键列。

    背景: _migrate_old_providers 把 ai_providers 表删了,但
    agent_configs.ai_provider_id / stock_agents.ai_provider_id 这两列上的 FK 没清。
    SQLite 默认 PRAGMA foreign_keys 不开,所以历史 INSERT 没事;但某些路径下
    (比如 INSERT ... RETURNING + SQLAlchemy 校验)会报 "no such table: ai_providers"。

    SQLite 3.35+ 支持 ALTER TABLE DROP COLUMN,直接 drop 即可。
    """
    if not _has_column(conn, table, "ai_provider_id"):
        return
    # ai_providers 还存在的话先不动(让 _migrate_old_providers 先迁移)
    if _has_table(conn, "ai_providers"):
        return
    try:
        conn.execute(text(f"ALTER TABLE {table} DROP COLUMN ai_provider_id"))
        conn.commit()
        logger.info(f"已清理 {table}.ai_provider_id 悬空外键列")
    except Exception as e:
        # 老 SQLite 不支持 DROP COLUMN — fallback 留 schema 不动,改用 PRAGMA foreign_keys=OFF
        # (本进程级别,不影响其他业务,因为 ai_providers 不存在,FK 永远无效)
        logger.warning(
            f"DROP COLUMN {table}.ai_provider_id 失败 (SQLite < 3.35?): {e}; "
            f"将通过 PRAGMA foreign_keys=OFF 绕开"
        )
        try:
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            conn.commit()
        except Exception:
            pass


# 备份轮转保留份数（T14 最小集：轮转 ≤3）
_BACKUP_KEEP = 3

# app_settings 中视为敏感的键（脱敏副本置空）：显式清单 + 通用模式兜底。
# 依据 docs/21 §7.1 三分映射（jwt_secret 实例级、auth_*→users）与 docs/20-M9
# （密钥静态泄露面）。模式覆盖未来新增的 *key*/*secret*/*token*/*password* 键。
_SENSITIVE_APP_SETTING_KEYS = frozenset(
    {"jwt_secret", "auth_username", "auth_password_hash"}
)
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(api_?key|secret|token|password|passwd)", re.IGNORECASE
)


def _backup_db_before_migration(
    db_path: str = DB_PATH, keep: int = _BACKUP_KEEP
) -> str:
    """迁移前创建一致性备份 + 自检 + 轮转 + 脱敏出卷副本（docs/17 R5 / docs/20-C3）。

    返回完整备份文件路径；库不存在或为空时返回 ""（跳过）。

    取舍说明（一致性快照二选一）：选 ``VACUUM INTO`` 而非
    ``wal_checkpoint(TRUNCATE) + copy2``。原因：
      1. 旧实现 copy2 只拷主文件不拷 -wal/-shm，WAL 下是非一致性快照，
         可能丢最近事务（docs/20-C3 判不可复用）；
      2. ``VACUUM INTO`` 由 SQLite 自身经 WAL 读取生成自包含一致性快照，
         对源库零写入；而 checkpoint(TRUNCATE) 会把 WAL 折回主文件，
         属于对生产文件的写操作，备份动作不应改变源库状态；
      3. 代价是全量重写一份文件，本项目库体积小，可接受。
    失败语义：任何一步失败即抛异常阻断迁移（不再仅 warning），
    否则「迁移失败可回滚」承诺落空（R5）。
    """
    if not os.path.exists(db_path):
        return ""
    try:
        if os.path.getsize(db_path) <= 0:
            return ""
    except OSError:
        return ""

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak.{ts}"
    suffix = 1
    while os.path.exists(backup_path):
        backup_path = f"{db_path}.bak.{ts}_{suffix}"
        suffix += 1

    # 1) VACUUM INTO 生成一致性快照
    escaped = backup_path.replace("'", "''")
    src = sqlite3.connect(db_path, timeout=30)
    try:
        src.isolation_level = None  # autocommit：VACUUM 不能在事务内执行
        src.execute(f"VACUUM INTO '{escaped}'")
    except Exception:
        try:
            os.remove(backup_path)
        except OSError:
            pass
        raise RuntimeError(f"数据库迁移前备份失败（VACUUM INTO）: {db_path}")
    finally:
        src.close()

    # 2) 备份自检：integrity_check 不过即删除并抛错阻断迁移
    _verify_backup_integrity(backup_path)
    logger.info(f"数据库迁移前备份已创建并通过 integrity_check: {backup_path}")

    # 3) 脱敏出卷副本（.sanitized）：schema 完整、敏感值置空，供出卷冷备
    _create_sanitized_copy(backup_path)

    # 4) 轮转：只保留最新 keep 份（连同其 .sanitized 副本一起淘汰）
    _rotate_backups(db_path, keep)

    return backup_path


def _verify_backup_integrity(backup_path: str) -> None:
    """对备份文件跑 PRAGMA integrity_check，失败即删文件并抛错。"""
    conn = sqlite3.connect(backup_path, timeout=30)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        result = row[0] if row else ""
    except Exception as e:
        result = f"integrity_check 执行失败: {e}"
    finally:
        conn.close()
    if result != "ok":
        try:
            os.remove(backup_path)
        except OSError:
            pass
        raise RuntimeError(
            f"备份自检失败，已阻断迁移（integrity_check={result!r}）: {backup_path}"
        )


def _create_sanitized_copy(backup_path: str) -> str:
    """生成脱敏出卷副本 <backup>.sanitized：剔密钥，保 schema 完整可恢复。

    脱敏方式 = 置空值（不删行），保证表结构与行数不变、可直接恢复使用：
      - ai_services.api_key → ''
      - notify_channels.config → ''
      - app_settings 中敏感 key 的 value → ''（显式清单 + 模式匹配）
    表/列不存在时跳过（新库可能尚无这些表）。
    """
    sanitized_path = backup_path + ".sanitized"
    shutil.copy2(backup_path, sanitized_path)
    conn = sqlite3.connect(sanitized_path, timeout=30)
    try:
        cur = conn.cursor()
        if _sqlite_has_column(cur, "ai_services", "api_key"):
            cur.execute("UPDATE ai_services SET api_key = ''")
        if _sqlite_has_column(cur, "notify_channels", "config"):
            cur.execute("UPDATE notify_channels SET config = ''")
        if _sqlite_has_column(cur, "app_settings", "key"):
            rows = cur.execute("SELECT key FROM app_settings").fetchall()
            for (key,) in rows:
                if key in _SENSITIVE_APP_SETTING_KEYS or (
                    isinstance(key, str) and _SENSITIVE_KEY_PATTERN.search(key)
                ):
                    cur.execute(
                        "UPDATE app_settings SET value = '' WHERE key = ?", (key,)
                    )
        conn.commit()
    finally:
        conn.close()
    logger.info(f"脱敏出卷副本已创建: {sanitized_path}")
    return sanitized_path


def _rotate_backups(db_path: str, keep: int) -> None:
    """panwatch.db.bak.* 只保留最新 keep 份，超出删最旧（含 .sanitized）。"""
    candidates = [
        p
        for p in glob.glob(f"{db_path}.bak.*")
        if not p.endswith(".sanitized")
    ]
    candidates.sort()  # 文件名含时间戳，字典序即时间序
    for old in candidates[:-keep] if keep > 0 else candidates:
        for path in (old, old + ".sanitized"):
            try:
                os.remove(path)
                logger.info(f"备份轮转删除: {path}")
            except OSError:
                pass


def _sqlite_has_column(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    """裸 sqlite3 游标版列存在性判断（区别于 SQLAlchemy 版 _has_column）。"""
    try:
        cur.execute(f"SELECT {column} FROM {table} LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def _migrate(engine):
    """增量 schema 迁移（SQLite ALTER TABLE ADD COLUMN）"""
    migrations = [
        # Phase 1(模拟盘求真):持仓期最高价,移动止损用
        (
            "paper_trading_positions",
            "highest_price",
            "ALTER TABLE paper_trading_positions ADD COLUMN highest_price REAL",
        ),
        (
            "stock_agents",
            "schedule",
            "ALTER TABLE stock_agents ADD COLUMN schedule TEXT DEFAULT ''",
        ),
        (
            "agent_configs",
            "ai_model_id",
            "ALTER TABLE agent_configs ADD COLUMN ai_model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL",
        ),
        (
            "agent_configs",
            "notify_channel_ids",
            "ALTER TABLE agent_configs ADD COLUMN notify_channel_ids TEXT DEFAULT '[]'",
        ),
        (
            "stock_agents",
            "ai_model_id",
            "ALTER TABLE stock_agents ADD COLUMN ai_model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL",
        ),
        (
            "stock_agents",
            "notify_channel_ids",
            "ALTER TABLE stock_agents ADD COLUMN notify_channel_ids TEXT DEFAULT '[]'",
        ),
        # Phase 3: 持仓增强
        (
            "stocks",
            "invested_amount",
            "ALTER TABLE stocks ADD COLUMN invested_amount REAL",
        ),
        # Phase 4: Agent 执行模式
        (
            "agent_configs",
            "execution_mode",
            "ALTER TABLE agent_configs ADD COLUMN execution_mode TEXT DEFAULT 'batch'",
        ),
        # Phase 4: 持仓交易风格
        (
            "positions",
            "trading_style",
            "ALTER TABLE positions ADD COLUMN trading_style TEXT DEFAULT 'swing'",
        ),
        # 排序字段：关注列表/持仓拖拽排序
        (
            "stocks",
            "sort_order",
            "ALTER TABLE stocks ADD COLUMN sort_order INTEGER DEFAULT 0",
        ),
        (
            "positions",
            "sort_order",
            "ALTER TABLE positions ADD COLUMN sort_order INTEGER DEFAULT 0",
        ),
        # 数据源增强
        (
            "data_sources",
            "supports_batch",
            "ALTER TABLE data_sources ADD COLUMN supports_batch INTEGER DEFAULT 0",
        ),
        (
            "data_sources",
            "test_symbols",
            "ALTER TABLE data_sources ADD COLUMN test_symbols TEXT DEFAULT '[]'",
        ),
        # Phase 5: 建议池元数据
        (
            "stock_suggestions",
            "meta",
            "ALTER TABLE stock_suggestions ADD COLUMN meta TEXT DEFAULT '{}'",
        ),
    ]
    with engine.connect() as conn:
        for table, column, sql in migrations:
            if not _has_column(conn, table, column):
                conn.execute(text(sql))
                conn.commit()

        # 清理 legacy 悬空外键:agent_configs.ai_provider_id / stock_agents.ai_provider_id
        # 这两列原本 REFERENCES ai_providers(id),但 _migrate_old_providers 已经把
        # ai_providers 表删了。如果保留 FK,新 INSERT 会触发 SQLite "no such table" 错误
        # (因为 SQLite 在 INSERT 时校验 FK 引用的表是否存在)。
        _drop_dangling_ai_provider_fk(conn, "agent_configs")
        _drop_dangling_ai_provider_fk(conn, "stock_agents")

        # 初始化排序字段（仅对未初始化数据）
        if _has_column(conn, "stocks", "sort_order"):
            conn.execute(text("UPDATE stocks SET sort_order = id WHERE sort_order IS NULL OR sort_order = 0"))
            conn.commit()
        if _has_column(conn, "positions", "sort_order"):
            conn.execute(text("UPDATE positions SET sort_order = id WHERE sort_order IS NULL OR sort_order = 0"))
            conn.commit()

        # Create new tables if missing (SQLite)
        if not _has_table(conn, "suggestion_feedback"):
            conn.execute(
                text(
                    """
CREATE TABLE IF NOT EXISTS suggestion_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  suggestion_id INTEGER NOT NULL REFERENCES stock_suggestions(id) ON DELETE CASCADE,
  useful INTEGER DEFAULT 1,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_feedback_suggestion_id ON suggestion_feedback(suggestion_id);"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_feedback_created_at ON suggestion_feedback(created_at);"
                )
            )
            conn.commit()


def _migrate_old_providers(engine):
    """如果存在旧的 ai_providers 表，迁移数据到 ai_services + ai_models"""
    with engine.connect() as conn:
        if not _has_table(conn, "ai_providers"):
            return
        # Check if it has the old schema (has base_url column)
        if not _has_column(conn, "ai_providers", "base_url"):
            return

        rows = conn.execute(
            text(
                "SELECT id, name, base_url, api_key, model, is_default FROM ai_providers"
            )
        ).fetchall()
        if not rows:
            conn.execute(text("DROP TABLE IF EXISTS ai_providers"))
            conn.commit()
            return

        # Group by base_url+api_key to create services
        service_map = {}  # (base_url, api_key) -> service_id
        for row in rows:
            old_id, name, base_url, api_key, model, is_default = row
            key = (base_url, api_key)
            if key not in service_map:
                # Create service
                conn.execute(
                    text(
                        "INSERT INTO ai_services (name, base_url, api_key) VALUES (:name, :base_url, :api_key)"
                    ),
                    {"name": name, "base_url": base_url, "api_key": api_key},
                )
                result = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                service_map[key] = result

            service_id = service_map[key]
            conn.execute(
                text(
                    "INSERT INTO ai_models (name, service_id, model, is_default) VALUES (:name, :service_id, :model, :is_default)"
                ),
                {
                    "name": name,
                    "service_id": service_id,
                    "model": model,
                    "is_default": is_default,
                },
            )
            new_model_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()

            # Update references: agent_configs.ai_provider_id → ai_model_id
            if _has_column(conn, "agent_configs", "ai_provider_id"):
                conn.execute(
                    text(
                        "UPDATE agent_configs SET ai_model_id = :new_id WHERE ai_provider_id = :old_id"
                    ),
                    {"new_id": new_model_id, "old_id": old_id},
                )
            # stock_agents.ai_provider_id → ai_model_id
            if _has_column(conn, "stock_agents", "ai_provider_id"):
                conn.execute(
                    text(
                        "UPDATE stock_agents SET ai_model_id = :new_id WHERE ai_provider_id = :old_id"
                    ),
                    {"new_id": new_model_id, "old_id": old_id},
                )

        conn.execute(text("DROP TABLE ai_providers"))
        conn.commit()
        logger.info(
            f"已迁移 {len(rows)} 条旧 AI Provider 数据到 ai_services + ai_models"
        )


def _migrate_settings_to_models(engine):
    """将旧的 app_settings 中的 AI/通知配置迁移为 AIService+AIModel / NotifyChannel 记录"""
    with engine.connect() as conn:
        if not _has_table(conn, "app_settings"):
            return

        rows = conn.execute(text("SELECT key, value FROM app_settings")).fetchall()
        settings_map = {row[0]: row[1] for row in rows}

        ai_base_url = settings_map.get("ai_base_url", "")
        ai_api_key = settings_map.get("ai_api_key", "")
        ai_model = settings_map.get("ai_model", "")

        # Migrate AI settings if present and no services exist yet
        if ai_base_url and ai_model:
            existing = conn.execute(text("SELECT COUNT(*) FROM ai_services")).scalar()
            if existing == 0:
                conn.execute(
                    text(
                        "INSERT INTO ai_services (name, base_url, api_key) VALUES (:name, :base_url, :api_key)"
                    ),
                    {"name": ai_model, "base_url": ai_base_url, "api_key": ai_api_key},
                )
                service_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                conn.execute(
                    text(
                        "INSERT INTO ai_models (name, service_id, model, is_default) VALUES (:name, :service_id, :model, 1)"
                    ),
                    {"name": ai_model, "service_id": service_id, "model": ai_model},
                )
                logger.info(f"已迁移 AI 配置: {ai_model}")

        # Migrate Telegram settings if present and no channels exist yet
        bot_token = settings_map.get("notify_telegram_bot_token", "")
        chat_id = settings_map.get("notify_telegram_chat_id", "")

        if bot_token:
            existing = conn.execute(
                text("SELECT COUNT(*) FROM notify_channels")
            ).scalar()
            if existing == 0:
                config_json = json.dumps({"bot_token": bot_token, "chat_id": chat_id})
                conn.execute(
                    text(
                        "INSERT INTO notify_channels (name, type, config, enabled, is_default) VALUES (:name, :type, :config, 1, 1)"
                    ),
                    {"name": "Telegram", "type": "telegram", "config": config_json},
                )
                logger.info("已迁移 Telegram 配置为 NotifyChannel")

        # Remove old settings keys
        old_keys = [
            "ai_base_url",
            "ai_api_key",
            "ai_model",
            "notify_telegram_bot_token",
            "notify_telegram_chat_id",
        ]
        for key in old_keys:
            if key in settings_map:
                conn.execute(
                    text("DELETE FROM app_settings WHERE key = :key"), {"key": key}
                )

        conn.commit()


def _migrate_positions_to_accounts(engine):
    """
    将旧的 stocks 表中的持仓数据迁移到 accounts + positions 表
    创建一个默认账户，并将有持仓的股票数据迁移过去
    """
    with engine.connect() as conn:
        # 检查是否已有账户数据（避免重复迁移）
        if not _has_table(conn, "accounts"):
            return

        existing_accounts = conn.execute(text("SELECT COUNT(*) FROM accounts")).scalar()
        if existing_accounts > 0:
            return

        # 检查 stocks 表是否有持仓数据需要迁移
        if not _has_column(conn, "stocks", "cost_price"):
            return

        stocks_with_position = conn.execute(
            text(
                "SELECT id, cost_price, quantity, invested_amount FROM stocks "
                "WHERE cost_price IS NOT NULL AND quantity IS NOT NULL"
            )
        ).fetchall()

        if not stocks_with_position:
            # 没有持仓数据，创建一个空的默认账户
            conn.execute(
                text(
                    "INSERT INTO accounts (name, available_funds, enabled) VALUES ('默认账户', 0, 1)"
                )
            )
            conn.commit()
            logger.info("已创建默认账户")
            return

        # 创建默认账户
        # 先获取旧的 available_funds 设置
        old_funds = conn.execute(
            text("SELECT value FROM app_settings WHERE key = 'available_funds'")
        ).scalar()
        available_funds = float(old_funds) if old_funds else 0

        conn.execute(
            text(
                "INSERT INTO accounts (name, available_funds, enabled) VALUES (:name, :funds, 1)"
            ),
            {"name": "默认账户", "funds": available_funds},
        )
        account_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()

        # 迁移持仓数据
        for row in stocks_with_position:
            stock_id, cost_price, quantity, invested_amount = row
            conn.execute(
                text(
                    "INSERT INTO positions (account_id, stock_id, cost_price, quantity, invested_amount) "
                    "VALUES (:account_id, :stock_id, :cost_price, :quantity, :invested_amount)"
                ),
                {
                    "account_id": account_id,
                    "stock_id": stock_id,
                    "cost_price": cost_price,
                    "quantity": quantity,
                    "invested_amount": invested_amount,
                },
            )

        # 删除旧的 available_funds 设置
        conn.execute(text("DELETE FROM app_settings WHERE key = 'available_funds'"))

        conn.commit()
        logger.info(f"已迁移 {len(stocks_with_position)} 条持仓数据到默认账户")


def _migrate_remove_stock_enabled(engine):
    """移除历史 stocks.enabled 软删除字段并清理残留数据。"""
    with engine.connect() as conn:
        if not _has_table(conn, "stocks") or not _has_column(conn, "stocks", "enabled"):
            return

        # 历史软删除数据：无任何关联则直接删除；有关联则恢复为有效股票。
        conn.execute(
            text(
                """
DELETE FROM stocks
WHERE COALESCE(enabled, 1) = 0
  AND id NOT IN (SELECT DISTINCT stock_id FROM positions)
  AND id NOT IN (SELECT DISTINCT stock_id FROM stock_agents)
  AND id NOT IN (SELECT DISTINCT stock_id FROM price_alert_rules)
"""
            )
        )
        conn.execute(text("UPDATE stocks SET enabled = 1 WHERE COALESCE(enabled, 1) = 0"))
        conn.commit()

        # 优先直接删列；旧版 SQLite 不支持时，重建表以确保物理移除。
        try:
            conn.execute(text("ALTER TABLE stocks DROP COLUMN enabled"))
            conn.commit()
            logger.info("已移除 stocks.enabled 列")
        except Exception:
            conn.rollback()
            logger.info("当前 SQLite 不支持 DROP COLUMN，改为重建 stocks 表移除 enabled")
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            conn.execute(
                text(
                    """
CREATE TABLE IF NOT EXISTS stocks__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol VARCHAR NOT NULL,
  name VARCHAR NOT NULL,
  market VARCHAR NOT NULL,
  cost_price FLOAT,
  quantity INTEGER,
  invested_amount FLOAT,
  sort_order INTEGER DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""
                )
            )
            conn.execute(
                text(
                    """
INSERT INTO stocks__new (
  id, symbol, name, market, cost_price, quantity, invested_amount, sort_order, created_at, updated_at
)
SELECT
  id, symbol, name, market, cost_price, quantity, invested_amount, COALESCE(sort_order, 0), created_at, updated_at
FROM stocks
"""
                )
            )
            conn.execute(text("DROP TABLE stocks"))
            conn.execute(text("ALTER TABLE stocks__new RENAME TO stocks"))
            conn.execute(text("PRAGMA foreign_keys=ON"))
            conn.commit()
            logger.info("已通过重建表移除 stocks.enabled 列")


# ---------------------------------------------------------------------------
# MT-P1 身份穿透机制点：do_orm_execute 租户自动过滤（docs/25 §3、docs/26-J12）
# 事件挂全局 Session 工厂（SessionLocal）而非单个 engine/session，确保
# log_handler / notify_dedupe / context_store / chat.py 等自建 SessionLocal()
# 的路径同样被覆盖。默认 PANWATCH_SINGLE_TENANT='1' 单租户直通，行为等价
# 单用户；注册表与过滤核心逻辑见 src/web/tenant_context.py。
# ---------------------------------------------------------------------------
from src.web.tenant_context import apply_tenant_filter  # noqa: E402


@event.listens_for(SessionLocal, "do_orm_execute")
def _apply_tenant_filter(execute_state):
    apply_tenant_filter(execute_state)
