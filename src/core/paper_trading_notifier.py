"""模拟盘跟单通知：建仓/平仓实时推送、盘前计划、日终摘要。"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import or_

from src.core.notifier import NotifierManager
from src.web.database import SessionLocal
from src.web.models import (
    AppSettings,
    NotifyChannel,
    PaperTradingAccount,
    PaperTradingPosition,
    PaperTradingTrade,
    StrategySignalRun,
    Tenant,
    TenantSettings,
)
from src.web.tenant_context import (
    DEFAULT_TENANT_ID,
    reset_current_tenant,
    set_current_tenant,
    single_tenant_mode,
    tenant_scope,
)

logger = logging.getLogger(__name__)


@contextmanager
def _unscoped_tenant() -> Iterator[None]:
    """临时清空租户 ctx：用于「本租户 + 托管共享」渠道可见集查询。

    do_orm_execute 自动过滤只能表达 tenant_id == ctx，表达不了
    ``tenant_id == ctx OR is_shared`` 的共享语义；窗口内查询必须自带显式
    租户谓词（调用方负责）。
    """
    token = set_current_tenant(None)
    try:
        yield
    finally:
        reset_current_tenant(token)


def _pt_tenant_ids(db) -> list[int]:
    """多租户扇出的租户集合：active 租户 ∪ 已有账户租户（确定性排序）。"""
    ids: set[int] = set()
    try:
        ids |= {
            t for (t,) in db.query(Tenant.id).filter(Tenant.status == "active").all()
        }
    except Exception:
        pass
    ids |= {t for (t,) in db.query(PaperTradingAccount.tenant_id).all()}
    if not ids:
        ids = {DEFAULT_TENANT_ID}
    return sorted(ids)

# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

_CONFIG_KEYS = {
    "pt_notify_enabled": "false",
    "pt_notify_channel_ids": "",
    "pt_notify_realtime": "true",
    "pt_notify_premarket": "true",
    "pt_notify_summary": "true",
}


def _load_config(tenant_id: int | None = None) -> dict[str, str]:
    """读取 pt_notify_* 配置。

    单租户直通 / tenant_id=None：只读 app_settings（原行为）。
    多租户且指定 tenant_id：租户级 tenant_settings 优先（T20/F3），缺省键
    回退 app_settings。
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(AppSettings)
            .filter(AppSettings.key.in_(_CONFIG_KEYS.keys()))
            .all()
        )
        cfg = dict(_CONFIG_KEYS)  # defaults
        for r in rows:
            cfg[r.key] = r.value or _CONFIG_KEYS.get(r.key, "")
        if tenant_id is not None and not single_tenant_mode():
            trows = (
                db.query(TenantSettings)
                .filter(
                    TenantSettings.tenant_id == tenant_id,
                    TenantSettings.key.in_(_CONFIG_KEYS.keys()),
                )
                .all()
            )
            for r in trows:
                if r.value:
                    cfg[r.key] = r.value
        return cfg
    finally:
        db.close()


def _is_enabled(tenant_id: int | None = None) -> bool:
    cfg = _load_config(tenant_id)
    return cfg.get("pt_notify_enabled", "").lower() == "true"


def _is_mode_enabled(mode_key: str, tenant_id: int | None = None) -> bool:
    cfg = _load_config(tenant_id)
    if cfg.get("pt_notify_enabled", "").lower() != "true":
        return False
    return cfg.get(mode_key, "").lower() == "true"


# ---------------------------------------------------------------------------
# 渠道构建
# ---------------------------------------------------------------------------

def _build_notifier(tenant_id: int | None = None) -> NotifierManager | None:
    """根据配置构建 NotifierManager，无可用渠道时返回 None。

    多租户且指定 tenant_id 时按账户租户路由渠道（T21）：可见集 =
    本租户渠道 + 管理员托管共享渠道（is_shared）。
    """
    cfg = _load_config(tenant_id)
    if cfg.get("pt_notify_enabled", "").lower() != "true":
        return None

    db = SessionLocal()
    try:
        channel_ids_str = cfg.get("pt_notify_channel_ids", "").strip()
        if channel_ids_str:
            ids = [int(x.strip()) for x in channel_ids_str.split(",") if x.strip().isdigit()]
            query = db.query(NotifyChannel).filter(
                NotifyChannel.id.in_(ids), NotifyChannel.enabled.is_(True)
            )
        else:
            # 未指定渠道时使用默认渠道
            query = db.query(NotifyChannel).filter(
                NotifyChannel.enabled.is_(True), NotifyChannel.is_default.is_(True)
            )

        if tenant_id is not None and not single_tenant_mode():
            with _unscoped_tenant():
                channels = query.filter(
                    or_(
                        NotifyChannel.tenant_id == tenant_id,
                        NotifyChannel.is_shared.is_(True),
                    )
                ).all()
        else:
            channels = query.all()

        if not channels:
            logger.debug("[模拟盘通知] 无可用通知渠道")
            return None

        mgr = NotifierManager()
        for ch in channels:
            mgr.add_channel(ch.type, ch.config or {})
        return mgr
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 消息格式化
# ---------------------------------------------------------------------------

EXIT_REASON_LABELS = {
    "stop_loss": "止损",
    "target_price": "止盈",
    "signal_reversal": "信号反转",
    "manual": "手动平仓",
}

STRATEGY_NAME_MAP = {
    "trend_follow": "趋势延续",
    "macd_golden": "MACD金叉",
    "volume_breakout": "放量突破",
    "pullback": "回踩确认",
    "rebound": "超跌反弹",
    "watchlist_agent": "Agent建议",
    "market_scan": "市场扫描",
    "momentum": "动量策略",
}


def _strategy_label(code: str) -> str:
    """策略代码转中文名称。"""
    return STRATEGY_NAME_MAP.get(code, code)


def _stock_display(symbol: str, market: str, name: str = "") -> str:
    """生成带链接的股票显示文本，点击代码跳转到行情页。"""
    from src.core.stock_link import stock_link_markdown
    label = f"{name} " if name else ""
    return f"{label}({stock_link_markdown(symbol, market)})"


def _format_entry_message(pos: dict, sig: dict | None) -> tuple[str, str]:
    """格式化建仓通知，返回 (title, body)。pos/sig 为序列化后的 dict。"""
    name = pos.get("stock_name") or pos["stock_symbol"]
    title = f"【模拟盘建仓】{name}"

    # 盈亏比
    rr_str = ""
    entry_price = pos.get("entry_price", 0)
    stop_loss = pos.get("stop_loss", 0)
    target_price = pos.get("target_price", 0)
    if stop_loss and target_price and entry_price:
        risk = abs(entry_price - stop_loss)
        reward = abs(target_price - entry_price)
        if risk > 0:
            rr_str = f"\n盈亏比: {reward / risk:.1f}:1"

    score_str = ""
    strategy_code = pos.get("strategy_code", "")
    if sig:
        if sig.get("rank_score"):
            score_str = f" | 评分: {sig['rank_score']:.1f}"
        if sig.get("strategy_code"):
            strategy_code = sig["strategy_code"]

    stock_info = _stock_display(pos["stock_symbol"], pos["stock_market"], name)
    body = (
        f"股票: {stock_info}\n"
        f"方向: 买入\n"
        f"买入价: {entry_price:.2f} | 数量: {pos['quantity']} 股\n"
        f"止损价: {stop_loss:.2f} | 目标价: {target_price:.2f}\n"
        f"策略: {_strategy_label(strategy_code)}{score_str}"
        f"{rr_str}"
    )
    return title, body


def _format_exit_message(pos: dict, trade: dict) -> tuple[str, str]:
    """格式化平仓通知，返回 (title, body)。pos/trade 为序列化后的 dict。"""
    name = pos.get("stock_name") or pos["stock_symbol"]
    pnl = trade["pnl"]
    pnl_sign = "+" if pnl >= 0 else ""
    title = f"【模拟盘平仓】{name} {pnl_sign}{pnl:.2f}"

    stock_info = _stock_display(pos["stock_symbol"], pos["stock_market"], name)
    reason = EXIT_REASON_LABELS.get(trade["exit_reason"], trade["exit_reason"])
    body = (
        f"股票: {stock_info}\n"
        f"平仓原因: {reason}\n"
        f"买入价: {trade['entry_price']:.2f} → 卖出价: {trade['exit_price']:.2f}\n"
        f"盈亏: {pnl_sign}{pnl:.2f} ({pnl_sign}{trade['pnl_pct']:.2f}%) | 持仓: {trade['holding_days']}天"
    )
    return title, body


def _dedup_signals(signals: list[StrategySignalRun]) -> list[tuple[StrategySignalRun, int]]:
    """按 (stock_symbol, stock_market) 去重，保留 rank_score 最高的信号。
    返回 [(signal, strategy_count), ...]，已按 rank_score desc 排序。
    """
    seen: dict[tuple[str, str], tuple[StrategySignalRun, int]] = {}
    for sig in signals:
        key = (sig.stock_symbol, sig.stock_market)
        if key not in seen:
            seen[key] = (sig, 1)
        else:
            _, count = seen[key]
            seen[key] = (seen[key][0], count + 1)
    # 已按 rank_score desc 查询，保留首次出现的顺序即可
    return list(seen.values())


def _format_premarket_plan(signals: list[StrategySignalRun], account: PaperTradingAccount) -> tuple[str, str]:
    """格式化盘前计划，返回 (title, body)。信号会自动去重。"""
    title = "【模拟盘盘前计划】"
    if not signals:
        return title, "今日无候选股票"

    deduped = _dedup_signals(signals)

    lines = [f"可用资金: {account.current_capital:,.2f}\n"]
    lines.append("今日候选:")
    for i, (sig, strat_count) in enumerate(deduped, 1):
        name = sig.stock_name or sig.stock_symbol
        from src.core.stock_link import stock_link_markdown
        link = stock_link_markdown(sig.stock_symbol, sig.stock_market)
        entry_range = ""
        if sig.entry_low and sig.entry_high:
            entry_range = f" 入场区间: {sig.entry_low:.2f}-{sig.entry_high:.2f}"
        score_str = f" 评分:{sig.rank_score:.1f}" if sig.rank_score else ""
        strat_label = _strategy_label(sig.strategy_code)
        if strat_count > 1:
            strat_label = f"{strat_label} 等{strat_count}个策略"
        lines.append(f"{i}. {name} ({link}){entry_range}{score_str} [{strat_label}]")

    return title, "\n".join(lines)


def _format_daily_summary(
    trades: list[PaperTradingTrade],
    positions: list[PaperTradingPosition],
    account: PaperTradingAccount,
) -> tuple[str, str]:
    """格式化日终摘要，返回 (title, body)。"""
    # 总资产
    positions_value = sum((p.current_price or p.entry_price) * p.quantity for p in positions)
    total_equity = account.current_capital + positions_value
    unrealized = sum(p.unrealized_pnl or 0 for p in positions)

    title = "【模拟盘日终摘要】"
    lines = [f"总资产: {total_equity:,.2f}"]

    # 当日平仓
    if trades:
        day_pnl = sum(t.pnl for t in trades)
        pnl_sign = "+" if day_pnl >= 0 else ""
        lines.append(f"\n当日平仓 {len(trades)} 笔, 盈亏: {pnl_sign}{day_pnl:,.2f}")
        for t in trades:
            s = "+" if t.pnl >= 0 else ""
            reason = EXIT_REASON_LABELS.get(t.exit_reason, t.exit_reason)
            lines.append(f"  · {t.stock_name or t.stock_symbol}: {s}{t.pnl:,.2f} ({s}{t.pnl_pct:.2f}%) [{reason}]")
    else:
        lines.append("\n当日无平仓操作")

    # 持仓浮盈
    if positions:
        u_sign = "+" if unrealized >= 0 else ""
        lines.append(f"\n持仓中 {len(positions)} 只, 浮动盈亏: {u_sign}{unrealized:,.2f}")
        for p in positions:
            pnl = p.unrealized_pnl or 0
            s = "+" if pnl >= 0 else ""
            lines.append(f"  · {p.stock_name or p.stock_symbol}: {s}{pnl:,.2f}")
    else:
        lines.append("\n当前无持仓")

    lines.append(f"\n可用资金: {account.current_capital:,.2f}")
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# 触发函数
# ---------------------------------------------------------------------------

async def notify_entry(
    pos: dict, sig: dict | None, tenant_id: int | None = None
) -> None:
    """建仓通知（异步，失败仅日志）。pos/sig 为序列化后的 dict。

    tenant_id：多租户扇出时按事件归属租户路由渠道；None = 旧行为。
    """
    try:
        if not _is_mode_enabled("pt_notify_realtime", tenant_id):
            return
        mgr = _build_notifier(tenant_id)
        if not mgr:
            return
        title, body = _format_entry_message(pos, sig)
        await mgr.notify(title, body)
    except Exception:
        logger.exception("[模拟盘通知] 建仓通知发送失败")


async def notify_exit(
    pos: dict, trade: dict, tenant_id: int | None = None
) -> None:
    """平仓通知（异步，失败仅日志）。pos/trade 为序列化后的 dict。"""
    try:
        if not _is_mode_enabled("pt_notify_realtime", tenant_id):
            return
        mgr = _build_notifier(tenant_id)
        if not mgr:
            return
        title, body = _format_exit_message(pos, trade)
        await mgr.notify(title, body)
    except Exception:
        logger.exception("[模拟盘通知] 平仓通知发送失败")


def _account_for_tenant(db, tenant_id: int | None) -> PaperTradingAccount | None:
    """取账户：tenant_id=None 保持原 ``.first()`` 单例行为；否则按租户过滤。"""
    if tenant_id is None:
        return db.query(PaperTradingAccount).first()
    return (
        db.query(PaperTradingAccount)
        .filter(PaperTradingAccount.tenant_id == tenant_id)
        .first()
    )


async def _send_premarket_plan_one(tenant_id: int | None) -> None:
    """单租户（或单例）盘前计划。tenant_id=None 时与原行为完全一致。"""
    if not _is_mode_enabled("pt_notify_premarket", tenant_id):
        return
    mgr = _build_notifier(tenant_id)
    if not mgr:
        return

    db = SessionLocal()
    try:
        account = _account_for_tenant(db, tenant_id)
        if not account or not account.enabled:
            return

        # 按投资比例排除不投入（比例为 0）的市场
        from src.core.paper_trading_engine import ALL_MARKETS, market_allocations_or_default
        alloc = market_allocations_or_default(account)
        excluded = [m for m in ALL_MARKETS if alloc.get(m, 0.0) <= 0]
        query = (
            db.query(StrategySignalRun)
            .filter(
                StrategySignalRun.status == "active",
                StrategySignalRun.action.in_(["buy", "add"]),
                StrategySignalRun.entry_low.isnot(None),
                StrategySignalRun.entry_high.isnot(None),
            )
        )
        if excluded:
            query = query.filter(StrategySignalRun.stock_market.notin_(excluded))
        signals = query.order_by(StrategySignalRun.rank_score.desc()).all()

        title, body = _format_premarket_plan(signals, account)
        await mgr.notify(title, body)
    finally:
        db.close()


async def send_premarket_plan() -> None:
    """盘前计划通知。多租户：单 job 内遍历租户，逐租户路由（T9/T17/T21）。"""
    try:
        if single_tenant_mode():
            await _send_premarket_plan_one(None)
            return
        db = SessionLocal()
        try:
            tenant_ids = _pt_tenant_ids(db)
        finally:
            db.close()
        for tid in tenant_ids:
            try:
                with tenant_scope(tid):
                    await _send_premarket_plan_one(tid)
            except Exception:
                logger.exception("[模拟盘通知] 租户 %s 盘前计划发送失败", tid)
    except Exception:
        logger.exception("[模拟盘通知] 盘前计划发送失败")


async def _send_daily_summary_one(tenant_id: int | None) -> None:
    """单租户（或单例）日终摘要。tenant_id=None 时与原行为完全一致。"""
    if not _is_mode_enabled("pt_notify_summary", tenant_id):
        return
    mgr = _build_notifier(tenant_id)
    if not mgr:
        return

    db = SessionLocal()
    try:
        account = _account_for_tenant(db, tenant_id)
        if not account or not account.enabled:
            return

        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # 当日已平仓
        trades = (
            db.query(PaperTradingTrade)
            .filter(PaperTradingTrade.closed_at >= today_start)
            .order_by(PaperTradingTrade.closed_at.desc())
            .all()
        )

        # 持仓中
        positions = (
            db.query(PaperTradingPosition)
            .filter(PaperTradingPosition.status == "open")
            .all()
        )

        title, body = _format_daily_summary(trades, positions, account)
        await mgr.notify(title, body)
    finally:
        db.close()


async def send_daily_summary() -> None:
    """日终摘要通知。多租户：单 job 内遍历租户，逐租户路由（T9/T17/T21）。"""
    try:
        if single_tenant_mode():
            await _send_daily_summary_one(None)
            return
        db = SessionLocal()
        try:
            tenant_ids = _pt_tenant_ids(db)
        finally:
            db.close()
        for tid in tenant_ids:
            try:
                with tenant_scope(tid):
                    await _send_daily_summary_one(tid)
            except Exception:
                logger.exception("[模拟盘通知] 租户 %s 日终摘要发送失败", tid)
    except Exception:
        logger.exception("[模拟盘通知] 日终摘要发送失败")


async def send_test_notification(tenant_id: int | None = None) -> dict:
    """发送测试通知，返回结果。tenant_id=None 保持原行为。"""
    mgr = _build_notifier(tenant_id)
    if not mgr:
        return {"success": False, "error": "通知未启用或无可用渠道"}
    result = await mgr.notify_with_result(
        "【模拟盘测试】",
        "这是一条测试通知，确认通知渠道配置正常。",
    )
    return result
