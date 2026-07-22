"""租户上下文与身份穿透机制点（MT-P1，docs/25 §3 / docs/26-J3、J12）。

- ``TenantCtx``：当前请求/后台任务的租户身份（tenant_id + user_id + role）。
- contextvar 存当前 ctx；web 请求由 ``TenantContextMiddleware`` 在每请求开始时
  强制清空（防 anyio threadpool 线程复用串租户），鉴权依赖再 set。
- 后台任务（调度器/引擎/agent 链）禁止偷读 contextvar 当身份来源，统一走
  ``with tenant_scope(tenant_id):`` 显式传参（T16）。
- 本模块还承载租户过滤注册表（TENANT_TABLES / SHARED_TABLES / SENTINEL_TABLES）
  与 ``apply_tenant_filter`` 核心逻辑；事件注册挂在 ``database.SessionLocal``
  全局 Session 工厂上（docs/26-J12 硬约束）。

MT-P1 行为等价单用户：``PANWATCH_SINGLE_TENANT`` 默认 ``'1'`` = 单租户直通，
全部过滤逻辑短路；未登记表仅 ``logger.warning``，fail-closed 的 raise 模式
留 MT-P2 激活（保证 611 测试基线零修改全绿）。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Set

from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session, with_loader_criteria

logger = logging.getLogger(__name__)

# 市场级哨兵 tenant_id：entry_candidates / strategy_signal_runs 中
# market_scan / mixed 源行统一写 tenant_id=0（docs/21 #22/#27、docs/26-J2/J3）
MARKET_SENTINEL_TENANT_ID = 0
# 默认租户（T18：NOT NULL DEFAULT 1，单租户期全部存量行归属租户 1）
DEFAULT_TENANT_ID = 1


# ---------------------------------------------------------------------------
# 表注册表（初始内容按 docs/21 §1 分类终表 + docs/26-J1/J2/J3 裁决）
# ---------------------------------------------------------------------------

# A 强私有（docs/21 §3.1 的 32 张 v120 加列表）+ D 类租户私有新表
TENANT_TABLES: Set[str] = frozenset(
    {
        # A 类（31 张，含 J2 改判移入的 strategy_signal_runs / strategy_outcomes）
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
        # D 类租户私有新表（v120 建表，docs/21 §7）
        "tenant_settings",
        "tenant_news_pushed",
        "agent_config_overrides",
    }
)

# B 市场级全局共享 + C 实例级 + 身份表（不做行级租户过滤）
SHARED_TABLES: Set[str] = frozenset(
    {
        # B 市场级全局（docs/21 #14/#23/#26/#29-#35，含 J1 改判的 portfolio_risk_snapshots）
        "news_cache",
        "market_scan_snapshots",
        "strategy_catalog",
        "strategy_weights",
        "strategy_weight_history",
        "factor_weights",
        "factor_weight_history",
        "market_regime_snapshots",
        "strategy_factor_snapshots",
        "portfolio_risk_snapshots",
        # C 实例级
        "agent_configs",
        "app_settings",
        "data_sources",
        "schema_migrations",
        # 身份表：登录需按全局唯一 username 跨租户定位用户（docs/25 §2.2），
        # 行级过滤由 API 层鉴权控制，不走 do_orm_execute 自动过滤。
        "tenants",
        "users",
    }
)

# docs/26-J3 特例表：谓词为 tenant_id IN (:ctx, 0)，市场级哨兵行对所有租户可见
SENTINEL_TABLES: Set[str] = frozenset({"entry_candidates", "strategy_signal_runs"})


@dataclass(frozen=True)
class TenantCtx:
    """当前租户身份。后台任务用 user_id=0 / role='system'。"""

    tenant_id: int
    user_id: int = 0
    role: str = "system"


_current_tenant: ContextVar[Optional[TenantCtx]] = ContextVar(
    "panwatch_current_tenant", default=None
)


def set_current_tenant(ctx: Optional[TenantCtx]) -> Token:
    """设置当前租户上下文，返回 Token 供 reset_current_tenant 恢复。"""
    return _current_tenant.set(ctx)


def reset_current_tenant(token: Token) -> None:
    """按 Token 恢复上一个上下文（务必 try/finally 配对调用）。"""
    _current_tenant.reset(token)


def current_tenant() -> Optional[TenantCtx]:
    """读取当前租户上下文；无（公开路由/裸脚本）返回 None。"""
    return _current_tenant.get()


@contextmanager
def tenant_scope(
    tenant_id: int, user_id: int = 0, role: str = "system"
) -> Iterator[TenantCtx]:
    """后台任务显式租户入口（T16）：``with tenant_scope(2): ...``

    调度线程复用场景必须走本入口而非裸 set，finally 内 reset 防串租户。
    """
    token = set_current_tenant(TenantCtx(tenant_id=tenant_id, user_id=user_id, role=role))
    try:
        yield TenantCtx(tenant_id=tenant_id, user_id=user_id, role=role)
    finally:
        reset_current_tenant(token)


def single_tenant_mode() -> bool:
    """T20/M14 回退 flag：默认 '1' = 单租户直通（行为等价单用户）。"""
    return os.environ.get("PANWATCH_SINGLE_TENANT", "1") == "1"


class TenantContextMiddleware:
    """纯 ASGI 中间件：每请求开始强制清空 contextvar，结束清理。

    FastAPI sync handler 跑在 anyio threadpool，contextvar 随线程不随请求，
    线程复用会把上一请求的租户带进下一请求——所以必须每请求显式清空。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = set_current_tenant(None)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_tenant(token)


# ---------------------------------------------------------------------------
# tenant_id 列反射缓存 + do_orm_execute 过滤核心
# ---------------------------------------------------------------------------

_tenant_column_cache: dict = {}
_warned_unregistered: Set[str] = set()


def refresh_tenant_column_cache(bind: Any) -> None:
    """启动时反射全库，缓存每张表是否有 tenant_id 列（幂等可重入）。

    接线建议：init_db() 之后以 engine 调用一次；v120 迁移加列后再刷新一次。
    """
    insp = sa_inspect(bind)
    for name in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns(name)}
        _tenant_column_cache[name] = "tenant_id" in cols


def _table_has_tenant_column(table_name: str) -> bool:
    """查反射缓存；未缓存（未 refresh）按无列处理（等价直通，MT-P1 安全默认）。"""
    return _tenant_column_cache.get(table_name, False)


def _warn_if_unregistered(table_name: str) -> None:
    """未登记且实际有 tenant_id 列的表：MT-P1 只告警不 raise（每进程每表一次）。"""
    if table_name in _warned_unregistered:
        return
    _warned_unregistered.add(table_name)
    if _tenant_column_cache.get(table_name):
        logger.warning(
            "MT-P1 租户过滤：表 %s 含 tenant_id 列但未登记 TENANT_TABLES/SHARED_TABLES，"
            "本阶段仅告警不过滤（fail-closed raise 模式留 MT-P2 激活）",
            table_name,
        )


def _criteria_option(model: Any, table_name: str, ctx: TenantCtx) -> Any:
    """SELECT 侧：为已登记且有 tenant_id 列的实体构造 with_loader_criteria。"""
    if table_name in SHARED_TABLES:
        return None
    if table_name not in TENANT_TABLES:
        _warn_if_unregistered(table_name)
        return None
    if not _table_has_tenant_column(table_name):
        return None
    if not hasattr(model, "tenant_id"):
        # schema 有列但模型未映射（迁移双轨窗口期），跳过防 AttributeError
        return None
    # 注意：不能用 lambda 闭包捕获 ctx（SQLAlchemy DeferredLambdaElement 的
    # 缓存键机制会拒绝非标量闭包变量）；直接先求值、传入已构造的表达式。
    tenant_id = ctx.tenant_id
    if table_name in SENTINEL_TABLES:
        return with_loader_criteria(
            model,
            model.tenant_id.in_([tenant_id, MARKET_SENTINEL_TENANT_ID]),
            include_aliases=True,
        )
    return with_loader_criteria(
        model, model.tenant_id == tenant_id, include_aliases=True
    )


def _dml_predicate(table: Any, ctx: TenantCtx) -> Any:
    """bulk UPDATE/DELETE 侧：直接给 statement 追加 tenant WHERE 谓词。"""
    table_name = table.name
    if table_name in SHARED_TABLES:
        return None
    if table_name not in TENANT_TABLES:
        _warn_if_unregistered(table_name)
        return None
    if not _table_has_tenant_column(table_name):
        return None
    col = getattr(table.c, "tenant_id", None)
    if col is None:
        return None
    if table_name in SENTINEL_TABLES:
        return col.in_([ctx.tenant_id, MARKET_SENTINEL_TENANT_ID])
    return col == ctx.tenant_id


def apply_tenant_filter(execute_state: Any) -> None:
    """do_orm_execute 事件核心（挂 database.SessionLocal，docs/26-J12）。

    注入租户谓词需全部满足：
      a) PANWATCH_SINGLE_TENANT != '1'（MT-P1 默认 '1' = 直通，首道短路）；
      b) 涉表在 TENANT_TABLES（SHARED_TABLES 跳过；未登记仅 warning）；
      c) 该表实际有 tenant_id 列（启动时反射缓存，见 refresh_tenant_column_cache）；
      d) current_tenant() 非空（无 ctx 的公开路由/裸脚本 MT-P1 放行）。
    """
    if single_tenant_mode():
        return
    if not (
        execute_state.is_select or execute_state.is_update or execute_state.is_delete
    ):
        return
    if execute_state.is_column_load or execute_state.is_relationship_load:
        return
    ctx = current_tenant()
    if ctx is None:
        return
    statement = execute_state.statement
    if execute_state.is_select:
        options = []
        for desc in statement.column_descriptions:
            entity = desc.get("entity")
            table = getattr(entity, "__table__", None)
            if table is None:
                continue
            opt = _criteria_option(entity, table.name, ctx)
            if opt is not None:
                options.append(opt)
        if options:
            execute_state.statement = statement.options(*options)
    else:
        # ORM bulk UPDATE / DELETE（2.0 风格 session.execute(update(...)) 等）
        table = getattr(statement, "table", None)
        if table is None:
            return
        predicate = _dml_predicate(table, ctx)
        if predicate is not None:
            execute_state.statement = statement.where(predicate)


# ---------------------------------------------------------------------------
# INSERT 侧写入守卫：before_flush tenant 归属（docs/25 §4.1 scoped_insert）
# ---------------------------------------------------------------------------


def scoped_insert_attribution(session: Any, flush_context: Any, instances: Any) -> None:
    """before_flush 钩子：给未显式赋值 tenant_id 的新建行补上当前租户归属。

    规则（与 docs/25 §4.1 设计一致）：
      a) 仅处理 session.new 中有 tenant_id 映射列的对象（复用
         ``_tenant_column_cache`` 反射缓存；缓存未初始化时按无列处理，
         与读侧 ``_table_has_tenant_column`` 同一安全默认）；
      b) 仅当 ``obj.tenant_id is None`` 且 ``current_tenant()`` 非空时赋
         ``ctx.tenant_id``——显式赋值过的行一律不动（硬约束：tenant_id=0
         市场级哨兵行、admin 为他租户建的 users 行等）；
      c) 无 ctx（裸脚本/后台无 tenant_scope 路径）不动，server_default="1"
         照旧，与改造前行为等价。
    防御说明：单租户直通模式下无需短路——该模式下 ctx.tenant_id 恒为默认
    租户 1（= server_default），赋值与否结果等价。
    """
    ctx = current_tenant()
    if ctx is None:
        return
    for obj in session.new:
        table = getattr(obj, "__table__", None)
        if table is None:
            continue
        if not _table_has_tenant_column(table.name):
            continue
        if not hasattr(obj, "tenant_id"):
            # schema 有列但模型未映射（迁移双轨窗口期），跳过防 AttributeError
            continue
        if obj.tenant_id is None:
            obj.tenant_id = ctx.tenant_id


# 全局注册到 sqlalchemy.orm.Session 类（有意为之，区别于 do_orm_execute 挂
# database.SessionLocal 工厂）：测试套件自建 sessionmaker 不经 SessionLocal，
# 全局注册保证生产与测试的所有 Session 实例都自动获得写入守卫。
event.listen(Session, "before_flush", scoped_insert_attribution)
