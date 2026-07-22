"""价格提醒引擎：规则评估、命中落库与通知发送。"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.collectors.capital_flow_collector import CapitalFlowCollector
from src.collectors.kline_collector import KlineCollector, kline_source
from src.config import Settings
from src.core.notifier import NotifierManager
from src.core.notify_policy import NotifyPolicy, parse_dedupe_overrides
from src.core.marketdata_client import md_quote_rows
from src.models.market import MarketCode, MARKETS
from src.web.database import SessionLocal
from src.web.models import NotifyChannel, PriceAlertHit, PriceAlertRule, Stock

logger = logging.getLogger(__name__)

# A 股专属条件类型：引擎层对非 CN 市场规则整体跳过并记日志（双层门控的引擎层）。
CN_ONLY_CONDITION_TYPES = {"turnover_rate", "capital_flow", "consecutive_close"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _to_market(market: str) -> MarketCode:
    try:
        return MarketCode(market)
    except Exception:
        return MarketCode.CN


def _is_trading_time(market: MarketCode) -> bool:
    market_def = MARKETS.get(market)
    if not market_def:
        return False
    return market_def.is_trading_time()


def _day_key(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _minute_bucket(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def _json_get(obj: dict, key: str, default=None):
    try:
        return obj.get(key, default)
    except Exception:
        return default


def _op_eval(left: float | None, op: str, right: Any) -> bool:
    if left is None:
        return False
    o = (op or "").strip().lower()
    if o in ("between", "in"):
        if not isinstance(right, (list, tuple)) or len(right) != 2:
            return False
        lo = _safe_float(right[0])
        hi = _safe_float(right[1])
        if lo is None or hi is None:
            return False
        return lo <= left <= hi

    rv = _safe_float(right)
    if rv is None:
        return False
    if o == ">":
        return left > rv
    if o == ">=":
        return left >= rv
    if o == "<":
        return left < rv
    if o == "<=":
        return left <= rv
    if o in ("=", "=="):
        return left == rv
    if o in ("!=", "<>"):
        return left != rv
    return False


@dataclass
class RuleEvalResult:
    matched: bool
    hits: list[dict]
    snapshot: dict


class PriceAlertEngine:
    """价格提醒扫描执行引擎（支持小规模缓存和去重）。"""

    def __init__(self):
        self._quote_cache: dict[str, tuple[float, dict]] = {}
        self._kline_cache: dict[str, tuple[float, dict]] = {}
        self._closes_cache: dict[str, tuple[float, list[float]]] = {}
        self.quote_ttl_sec = 5.0
        self.kline_ttl_sec = 60.0

    async def _fetch_quotes_map(self, stocks: list[Stock]) -> dict[tuple[str, str], dict]:
        """走 QuoteOrchestrator,支持多 provider 主备故障转移。"""
        grouped: dict[MarketCode, list[Stock]] = {}
        for s in stocks:
            grouped.setdefault(_to_market(s.market), []).append(s)

        out: dict[tuple[str, str], dict] = {}
        for market, items in grouped.items():
            symbols = [s.symbol for s in items]
            if not symbols:
                continue
            rows = await asyncio.to_thread(md_quote_rows, symbols, market.value)
            by_symbol = {str(r.get("symbol")): r for r in rows}
            for sym in symbols:
                q = by_symbol.get(sym)
                if q:
                    out[(market.value, sym)] = q
        return out

    async def _get_kline_summary_cached(self, market: MarketCode, symbol: str) -> dict:
        key = f"{market.value}:{symbol}"
        now = time.monotonic()
        cached = self._kline_cache.get(key)
        if cached and now - cached[0] < self.kline_ttl_sec:
            return cached[1]
        try:
            with kline_source("price_alert"):
                summary = await asyncio.to_thread(KlineCollector(market).get_kline_summary, symbol)
        except Exception:
            summary = {}
        self._kline_cache[key] = (now, summary or {})
        return summary or {}

    async def _get_capital_flow(self, market: MarketCode, symbol: str):
        """取资金流向（CapitalFlowCollector 自带 600s TTL 缓存）。失败 fail-safe 返回 None。"""
        try:
            return await asyncio.to_thread(
                CapitalFlowCollector(market).get_capital_flow, symbol
            )
        except Exception:
            return None

    async def _get_daily_closes_cached(
        self, market: MarketCode, symbol: str, days: int
    ) -> list[float]:
        """取最近 N 根日 K 收盘价（复用 KlineCollector 缓存）。

        fail-safe：K 线不足 N 根、源异常时返回已有/空列表，由调用方判定不触发。
        """
        need = max(1, int(days or 1))
        key = f"{market.value}:{symbol}:{need}"
        now = time.monotonic()
        cached = self._closes_cache.get(key)
        if cached and now - cached[0] < self.kline_ttl_sec:
            return list(cached[1])
        closes: list[float] = []
        try:
            with kline_source("price_alert"):
                bars = await asyncio.to_thread(
                    KlineCollector(market).get_klines, symbol, days=need
                )
            for b in (bars or [])[-need:]:
                c = _safe_float(getattr(b, "close", None))
                if c is not None:
                    closes.append(c)
        except Exception:
            closes = []
        self._closes_cache[key] = (now, closes)
        return closes

    async def _eval_condition(
        self,
        cond: dict,
        quote: dict,
        market: MarketCode,
        symbol: str,
    ) -> tuple[bool, dict]:
        ctype = str(_json_get(cond, "type", "")).strip()
        op = str(_json_get(cond, "op", "")).strip()
        value = _json_get(cond, "value")
        left: float | None = None

        if ctype == "price":
            left = _safe_float(quote.get("current_price"))
        elif ctype == "change_pct":
            left = _safe_float(quote.get("change_pct"))
        elif ctype == "turnover":
            left = _safe_float(quote.get("turnover"))
        elif ctype == "volume":
            left = _safe_float(quote.get("volume"))
        elif ctype == "volume_ratio":
            # 优先用报价里的量比(腾讯 parts[49]),免拉 K线;
            # 仅当报价缺量比(如美股 yfinance)才回退 K线摘要。
            left = _safe_float(quote.get("volume_ratio"))
            if left is None:
                summary = await self._get_kline_summary_cached(market, symbol)
                left = _safe_float(summary.get("volume_ratio"))
        elif ctype in CN_ONLY_CONDITION_TYPES:
            return await self._eval_cn_only_condition(cond, quote, market, symbol)
        else:
            return False, {"type": ctype, "error": "unsupported_type"}

        ok = _op_eval(left, op, value)
        return ok, {
            "type": ctype,
            "op": op,
            "target": value,
            "actual": left,
            "matched": ok,
        }

    async def _eval_cn_only_condition(
        self,
        cond: dict,
        quote: dict,
        market: MarketCode,
        symbol: str,
    ) -> tuple[bool, dict]:
        """评估 A 股专属条件（turnover_rate/capital_flow/consecutive_close）。

        CN-only 门控：非 CN 市场直接拒绝并记日志；数据缺失一律 fail-safe 不触发。
        """
        ctype = str(_json_get(cond, "type", "")).strip()
        op = str(_json_get(cond, "op", "")).strip()
        value = _json_get(cond, "value")

        if market != MarketCode.CN:
            logger.info(
                "[价格提醒] 条件 %s 仅支持 A 股，跳过评估 (market=%s symbol=%s)",
                ctype, market.value, symbol,
            )
            return False, {
                "type": ctype, "op": op, "target": value,
                "actual": None, "matched": False, "error": "cn_only",
            }

        if ctype == "turnover_rate":
            left = _safe_float(quote.get("turnover_rate"))
            ok = _op_eval(left, op, value)
            return ok, {
                "type": ctype, "op": op, "target": value,
                "actual": left, "matched": ok,
            }

        if ctype == "capital_flow":
            # 主力净流入（万元）：collector 原始单位为元，此处换算。
            flow = await self._get_capital_flow(market, symbol)
            raw = _safe_float(getattr(flow, "main_net_inflow", None)) if flow else None
            left = raw / 10000.0 if raw is not None else None
            ok = _op_eval(left, op, value)
            return ok, {
                "type": ctype, "op": op, "target": value,
                "actual": left, "matched": ok,
                "error": None if left is not None else "no_capital_flow",
            }

        # consecutive_close：最近 N 根日 K 收盘价全部满足 op/value 才命中。
        try:
            days = int(_json_get(cond, "days", 0) or 0)
        except Exception:
            days = 0
        closes: list[float] = []
        if days >= 1:
            closes = await self._get_daily_closes_cached(market, symbol, days)
        window = closes[-days:] if days >= 1 else []
        if days < 1 or len(window) < days:
            return False, {
                "type": ctype, "op": op, "target": value, "days": days,
                "actual": window, "matched": False, "error": "insufficient_kline",
            }
        ok = all(_op_eval(c, op, value) for c in window)
        return ok, {
            "type": ctype, "op": op, "target": value, "days": days,
            "actual": window, "matched": ok,
        }

    async def eval_rule(self, rule: PriceAlertRule, quote: dict) -> RuleEvalResult:
        cond_group = rule.condition_group or {}
        op = str(cond_group.get("op", "and")).lower()
        items = cond_group.get("items") or []
        if not isinstance(items, list) or not items:
            return RuleEvalResult(matched=False, hits=[], snapshot={"error": "empty_items"})

        market = _to_market(rule.stock.market)
        symbol = rule.stock.symbol

        # CN-only 规则级门控：非 CN 股票含 A 股专属条件的规则整体跳过并记日志。
        if market != MarketCode.CN:
            cn_only_hit = any(
                isinstance(c, dict)
                and str(_json_get(c, "type", "")).strip() in CN_ONLY_CONDITION_TYPES
                for c in items
            )
            if cn_only_hit:
                logger.info(
                    "[价格提醒] 规则 %s 含 A 股专属条件，非 CN 市场规则整体跳过 (market=%s symbol=%s)",
                    rule.id, market.value, symbol,
                )
                return RuleEvalResult(
                    matched=False,
                    hits=[],
                    snapshot={
                        "symbol": symbol,
                        "market": market.value,
                        "error": "cn_only_condition",
                    },
                )

        results: list[dict] = []
        bools: list[bool] = []
        for cond in items:
            if not isinstance(cond, dict):
                continue
            ok, detail = await self._eval_condition(cond, quote, market, symbol)
            results.append(detail)
            bools.append(ok)

        if not bools:
            matched = False
        elif op == "or":
            matched = any(bools)
        else:
            matched = all(bools)

        snapshot = {
            "symbol": symbol,
            "market": market.value,
            "quote": {
                "current_price": quote.get("current_price"),
                "change_pct": quote.get("change_pct"),
                "turnover": quote.get("turnover"),
                "volume": quote.get("volume"),
            },
            "conditions": results,
            "group_op": op,
        }
        return RuleEvalResult(matched=matched, hits=results, snapshot=snapshot)

    def _can_trigger(
        self, rule: PriceAlertRule, now: datetime, *, bypass_market_hours: bool = False
    ) -> tuple[bool, str]:
        if not rule.enabled:
            return False, "disabled"

        if rule.expire_at:
            exp = rule.expire_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if now > exp:
                return False, "expired"

        if rule.market_hours_mode == "trading_only" and not bypass_market_hours:
            if not _is_trading_time(_to_market(rule.stock.market)):
                return False, "non_trading"

        today = _day_key(now)
        if (rule.trigger_date or "") != today:
            rule.trigger_date = today
            rule.trigger_count_today = 0

        max_per_day = int(rule.max_triggers_per_day or 0)
        if max_per_day > 0 and int(rule.trigger_count_today or 0) >= max_per_day:
            return False, "daily_limit"

        if rule.repeat_mode == "once" and rule.last_trigger_at:
            return False, "once_triggered"

        if rule.last_trigger_at:
            last = rule.last_trigger_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            delta_sec = (now - last).total_seconds()
            cooldown = max(0, int(rule.cooldown_minutes or 0)) * 60
            if delta_sec < cooldown:
                return False, "cooldown"

        return True, "ok"

    def _resolve_channels(self, db: Session, rule: PriceAlertRule) -> list[NotifyChannel]:
        ids = rule.notify_channel_ids or []
        if ids:
            return (
                db.query(NotifyChannel)
                .filter(NotifyChannel.enabled == True, NotifyChannel.id.in_(ids))
                .all()
            )
        return (
            db.query(NotifyChannel)
            .filter(NotifyChannel.enabled == True, NotifyChannel.is_default == True)
            .all()
        )

    def _build_notify_policy(self, db: Session | None) -> NotifyPolicy:
        """构建统一 NotifyPolicy（静默时段等），Settings 默认值 + app_settings 覆盖。"""
        settings = Settings()
        quiet_hours = settings.notify_quiet_hours or ""
        retry_attempts = settings.notify_retry_attempts
        retry_backoff = settings.notify_retry_backoff_seconds
        overrides_raw = settings.notify_dedupe_ttl_overrides or ""
        try:
            from src.web.models import AppSettings

            def _get(key: str) -> str:
                row = db.query(AppSettings).filter(AppSettings.key == key).first()
                return row.value if row and row.value else ""

            quiet_hours = _get("notify_quiet_hours") or quiet_hours
            ra = _get("notify_retry_attempts")
            if ra:
                retry_attempts = int(ra)
            rb = _get("notify_retry_backoff_seconds")
            if rb:
                retry_backoff = float(rb)
            overrides_raw = _get("notify_dedupe_ttl_overrides") or overrides_raw
        except Exception:
            pass
        try:
            retry_attempts = int(retry_attempts)
        except Exception:
            retry_attempts = 0
        try:
            retry_backoff = float(retry_backoff)
        except Exception:
            retry_backoff = 0.0
        return NotifyPolicy(
            timezone=settings.app_timezone or "UTC",
            quiet_hours=quiet_hours,
            retry_attempts=retry_attempts,
            retry_backoff_seconds=retry_backoff,
            dedupe_ttl_overrides=parse_dedupe_overrides(overrides_raw),
        )

    def _get_playbook_hint(self, db: Session, rule: PriceAlertRule) -> str | None:
        """命中时读取关联方案的触发提示文案（无关联/无匹配/异常均返回 None）。"""
        pid = getattr(rule, "playbook_id", None)
        if not pid:
            return None
        try:
            from src.core.playbook import get_trigger_hint

            return get_trigger_hint(db, int(pid), rule.name or "")
        except Exception as e:
            logger.debug("[价格提醒] 读取方案提示失败 rule=%s: %s", rule.id, e)
            return None

    async def _send_notify(self, db: Session, rule: PriceAlertRule, snapshot: dict) -> tuple[bool, str]:
        channels = self._resolve_channels(db, rule)
        notifier = NotifierManager(policy=self._build_notify_policy(db))
        for ch in channels:
            notifier.add_channel(ch.type, ch.config or {})

        symbol = rule.stock.symbol
        name = rule.stock.name or symbol
        quote = snapshot.get("quote") or {}
        price = _safe_float(quote.get("current_price"))
        chg = _safe_float(quote.get("change_pct"))
        title = f"【价格提醒】{name} ({symbol})"
        lines = [
            f"规则: {rule.name or f'提醒#{rule.id}'}",
            f"现价: {price:.2f}" if price is not None else "现价: --",
            f"涨跌幅: {chg:+.2f}%" if chg is not None else "涨跌幅: --",
        ]
        hit_lines = []
        for h in snapshot.get("conditions") or []:
            if h.get("matched"):
                hit_lines.append(
                    f"- {h.get('type')} {h.get('op')} {h.get('target')} (当前: {h.get('actual')})"
                )
        if hit_lines:
            lines.append("命中条件:")
            lines.extend(hit_lines[:4])
        hint = self._get_playbook_hint(db, rule)
        if hint:
            lines.append(f"方案提示: {hint}")
        content = "\n".join(lines)

        try:
            result = await notifier.notify_with_result(title, content)
            if result.get("success"):
                return True, ""
            err = str(result.get("error") or result.get("skipped") or "notify_failed")
            return False, err
        except Exception as e:
            return False, str(e)

    async def scan_once(
        self,
        *,
        only_rule_id: int | None = None,
        dry_run: bool = False,
        bypass_market_hours: bool = False,
    ) -> dict:
        now = _utc_now()
        db = SessionLocal()
        try:
            query = db.query(PriceAlertRule).join(Stock).filter(PriceAlertRule.enabled == True)
            if only_rule_id:
                query = query.filter(PriceAlertRule.id == only_rule_id)
            rules = query.all()
            if not rules:
                return {"total_rules": 0, "triggered": 0, "skipped": 0, "items": []}

            stocks = [r.stock for r in rules if r.stock is not None]
            quote_map = await self._fetch_quotes_map(stocks)

            items: list[dict] = []
            triggered = 0
            skipped = 0

            for rule in rules:
                stock = rule.stock
                if not stock:
                    skipped += 1
                    items.append({"rule_id": rule.id, "status": "no_stock"})
                    continue
                market = _to_market(stock.market)
                quote = quote_map.get((market.value, stock.symbol))
                if not quote:
                    skipped += 1
                    items.append({"rule_id": rule.id, "status": "no_quote"})
                    continue

                can, reason = self._can_trigger(
                    rule, now, bypass_market_hours=bypass_market_hours
                )
                if not can:
                    skipped += 1
                    items.append({"rule_id": rule.id, "status": "gated", "reason": reason})
                    continue

                ev = await self.eval_rule(rule, quote)
                if not ev.matched:
                    skipped += 1
                    items.append({"rule_id": rule.id, "status": "not_matched"})
                    continue

                if dry_run:
                    triggered += 1
                    items.append(
                        {
                            "rule_id": rule.id,
                            "status": "would_trigger",
                            "snapshot": ev.snapshot,
                        }
                    )
                    continue

                bucket = _minute_bucket(now)
                hit = PriceAlertHit(
                    rule_id=rule.id,
                    stock_id=stock.id,
                    trigger_time=now,
                    trigger_bucket=bucket,
                    trigger_snapshot=ev.snapshot,
                )
                db.add(hit)
                try:
                    db.flush()
                except Exception:
                    db.rollback()
                    skipped += 1
                    items.append({"rule_id": rule.id, "status": "duplicated"})
                    continue

                notify_ok, notify_err = await self._send_notify(db, rule, ev.snapshot)
                hit.notify_success = bool(notify_ok)
                hit.notify_error = notify_err or ""

                rule.last_trigger_at = now
                rule.last_trigger_price = _safe_float(quote.get("current_price"))
                rule.trigger_count_today = int(rule.trigger_count_today or 0) + 1
                rule.trigger_date = _day_key(now)
                if rule.repeat_mode == "once":
                    rule.enabled = False

                db.commit()
                triggered += 1
                items.append(
                    {
                        "rule_id": rule.id,
                        "status": "triggered",
                        "notify_success": bool(notify_ok),
                        "notify_error": notify_err,
                    }
                )

            return {
                "total_rules": len(rules),
                "triggered": triggered,
                "skipped": skipped,
                "items": items,
                "scanned_at": now.isoformat(),
            }
        finally:
            db.close()


ENGINE = PriceAlertEngine()
