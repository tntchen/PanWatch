"""Event-driven gate for intraday monitor.

Goal: avoid calling AI on every tick; only analyze when meaningful events happen.

We persist a small per-symbol state under DATA_DIR.

MT-P3（T7/R3，docs/23 §4）：状态文件 schema_version=2 分层——
- ``market.{symbol}``：市场观测态（last_price 基线 / tech_sig / 观测记录），
  全租户共享单份（行情是连续观测流，租户复制反而错误）；
- ``tenants.{tenant_id}.{symbol}``：租户态（pb_fired 冷却），冷却键
  ``方向:位名@价位`` 语义不变但归属租户命名空间，跨租户互吞消除。
旧 v1 文件读取时惰性自愈迁移（pb_fired 归默认租户 1，原子写 + .v1.bak 备份）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.core.json_store import read_json, write_json_atomic

logger = logging.getLogger(__name__)

# 状态文件 schema 版本（v2 = 市场观测态 / 租户态分层，docs/23 §4.3）
STATE_SCHEMA_VERSION = 2
# v1→v2 迁移备份后缀（保留现场，幂等：已存在不覆盖）
_V1_BACKUP_SUFFIX = ".v1.bak"
# 默认租户（T18：单租户期全部存量状态归属租户 1）
DEFAULT_TENANT_ID = 1


def _data_dir() -> str:
    return os.environ.get("DATA_DIR", "./data")


def _state_path() -> str:
    return os.path.join(_data_dir(), "state", "intraday_monitor_state.json")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 状态文件 v2 分层读写（MT-P3 / T7）
# ---------------------------------------------------------------------------

# 市场观测态字段（v1 symbol 级记录中归入 market 节的字段）
_MARKET_FIELDS = ("last_price", "tech_sig", "last_seen_at", "change_pct", "volume_ratio")


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_SCHEMA_VERSION, "market": {}, "tenants": {}}


def _tenant_key(tenant_id: Any) -> str:
    """租户态命名空间键；异常入参兜底默认租户（fail-soft，不阻断 agent）。"""
    try:
        return str(int(tenant_id))
    except (TypeError, ValueError):
        return str(DEFAULT_TENANT_ID)


def _migrate_v1_state(path: str, v1: dict[str, Any]) -> dict[str, Any]:
    """v1 → v2 一次性迁移：行情字段归 market 节，pb_fired 归默认租户 1。

    原子写回 + 原文件备份为 ``<path>.v1.bak``（已存在备份不覆盖，幂等可重入）。
    """
    market: dict[str, Any] = {}
    fired: dict[str, Any] = {}
    for symbol, rec in v1.items():
        if not isinstance(rec, dict):
            continue
        market[symbol] = {
            k: rec[k] for k in _MARKET_FIELDS if k in rec
        }
        pb_fired = rec.get("pb_fired")
        if isinstance(pb_fired, dict) and pb_fired:
            fired[symbol] = {"pb_fired": pb_fired}
    state: dict[str, Any] = {
        "version": STATE_SCHEMA_VERSION,
        "market": market,
        "tenants": {str(DEFAULT_TENANT_ID): fired} if fired else {},
    }
    try:
        backup = path + _V1_BACKUP_SUFFIX
        if os.path.exists(path) and not os.path.exists(backup):
            shutil.copy2(path, backup)
    except Exception:
        logger.exception("事件门状态 v1 备份失败 path=%s", path)
    write_json_atomic(path, state)
    logger.info("事件门状态文件已迁移 v1→v2：%s", path)
    return state


def migrate_state_file_to_v2(path: str | None = None) -> bool:
    """显式一次性迁移入口（幂等）：已 v2 / 文件为空返回 False，迁移返回 True。"""
    path = path or _state_path()
    raw = read_json(path, default={})
    if not isinstance(raw, dict) or not raw:
        return False
    if raw.get("version") == STATE_SCHEMA_VERSION:
        return False
    _migrate_v1_state(path, raw)
    return True


def _load_state(path: str) -> dict[str, Any]:
    """读取状态文件并归一化为 v2 结构；v1 文件惰性自愈迁移（fail-soft）。"""
    raw = read_json(path, default={})
    if isinstance(raw, dict) and raw.get("version") == STATE_SCHEMA_VERSION:
        market = raw.get("market")
        tenants = raw.get("tenants")
        return {
            "version": STATE_SCHEMA_VERSION,
            "market": market if isinstance(market, dict) else {},
            "tenants": tenants if isinstance(tenants, dict) else {},
        }
    if isinstance(raw, dict) and raw:
        return _migrate_v1_state(path, raw)
    return _empty_state()


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


# ATR 自适应异动默认倍数:涨跌幅 >= k×ATR% 视为相对个股自身波动的异动。
DEFAULT_ATR_K = 1.5


def adaptive_price_threshold(
    atr_pct: float | None,
    fixed_threshold: float,
    k: float = DEFAULT_ATR_K,
) -> float:
    """返回自适应价格异动阈值 = max(固定阈值, k×ATR%)。

    ATR% 缺失/非正(None/0/负/异常)时退回固定阈值,保证不丢失原有行为。
    固定阈值始终作为下限(floor),避免极低波动个股阈值过松。
    """
    fixed = _safe_float(fixed_threshold) or 0.0
    ap = _safe_float(atr_pct)
    if ap is None or ap <= 0:
        return fixed
    return max(fixed, (_safe_float(k) or DEFAULT_ATR_K) * ap)


def is_abnormal_move(
    change_pct: float | None,
    atr_pct: float | None,
    k: float = DEFAULT_ATR_K,
    fixed_threshold: float = 0.0,
) -> bool:
    """判断今日涨跌幅相对个股自身波动率是否异常。

    规则:|change_pct| >= max(固定阈值, k×ATR%) 即异动。
    - atr_pct 为 None/0 时回退到 fixed_threshold(保留原有固定阈值行为)。
    - 任一入参异常一律按"非异动"返回 False(fail-soft,不阻断 agent)。
    """
    cp = _safe_float(change_pct)
    if cp is None:
        return False
    threshold = adaptive_price_threshold(atr_pct, fixed_threshold, k)
    if threshold <= 0:
        return False
    return abs(cp) >= threshold


@dataclass(frozen=True)
class EventDecision:
    should_analyze: bool
    reasons: list[str]


# ---------------------------------------------------------------------------
# 方案档案（playbook）关键价位事件（P3c）
# ---------------------------------------------------------------------------

# 同一价位同一方向穿越的冷却时间（秒），避免价格在关键位附近来回穿越反复触发。
PLAYBOOK_CROSS_COOLDOWN_SEC = 1800

_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~—]\s*(\d+(?:\.\d+)?)")
_CENTER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*±\s*(\d+(?:\.\d+)?)")
_CMP_NUM_RE = re.compile(r"[<>≥≤]\s*(\d+(?:\.\d+)?)")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class PlaybookLevel:
    """方案关键价位：name 为人类可读位名，price 为价位。"""

    name: str
    price: float


def _parse_rule_price(text: str) -> float | None:
    """从规则文案解析唯一价位；无法唯一确定时返回 None（容错跳过）。

    优先取比较符后的数字（如 "连续2日收盘<98" -> 98）；无比较符时仅在
    全文恰好一个数字时采用，多个数字视为歧义跳过。
    """
    cmp_nums = [float(x) for x in _CMP_NUM_RE.findall(text)]
    if len(cmp_nums) == 1:
        return cmp_nums[0]
    if not cmp_nums:
        nums = [float(x) for x in _NUM_RE.findall(text)]
        if len(nums) == 1:
            return nums[0]
    return None


def extract_playbook_levels(payload: Any) -> list[PlaybookLevel]:
    """从 playbook payload 提取关键价位（做T区/接回区/防线/批次触发区）。

    全部字段容错：缺字段/类型不符/文案无法解析时跳过对应规则，不抛异常。
    同价位去重（保留先出现的名称）。
    """
    levels: list[PlaybookLevel] = []
    if not isinstance(payload, dict):
        return levels
    try:
        t_zone = payload.get("t_zone")
        if isinstance(t_zone, dict):
            for key, label in (
                ("sell_range", "做T卖出区"),
                ("buyback_range", "做T接回区"),
            ):
                rng = t_zone.get(key)
                if not isinstance(rng, (list, tuple)):
                    continue
                for i, v in enumerate(rng):
                    f = _safe_float(v)
                    if f is None or f <= 0:
                        continue
                    if len(rng) == 2:
                        edge = "下沿" if i == 0 else "上沿"
                    else:
                        edge = f"档{i + 1}"
                    levels.append(PlaybookLevel(f"{label}{edge}", f))

        defense = payload.get("defense")
        if isinstance(defense, dict):
            rule = str(defense.get("rule") or "")
            price = _parse_rule_price(rule)
            if price is not None and price > 0:
                levels.append(PlaybookLevel("防线", price))

        batches = payload.get("batches")
        if isinstance(batches, list):
            for b in batches:
                if not isinstance(b, dict):
                    continue
                # 已执行批次不再是"待触发"价位，跳过
                if str(b.get("status") or "") == "executed":
                    continue
                name = str(b.get("name") or "").strip() or "未命名"
                trigger = str(b.get("trigger") or "")
                if not trigger:
                    continue
                m = _CENTER_RE.search(trigger)
                if m:  # "120±3" -> 中心价 120
                    levels.append(PlaybookLevel(f"批次{name}触发区", float(m.group(1))))
                    continue
                m = _RANGE_RE.search(trigger)
                if m:  # "110-115" -> 区间上下沿
                    levels.append(
                        PlaybookLevel(f"批次{name}触发区下沿", float(m.group(1)))
                    )
                    levels.append(
                        PlaybookLevel(f"批次{name}触发区上沿", float(m.group(2)))
                    )
                    continue
                price = _parse_rule_price(trigger)
                if price is not None and price > 0:
                    levels.append(PlaybookLevel(f"批次{name}触发区", price))
    except Exception:
        logger.exception("extract_playbook_levels 失败")

    # 同价位去重，保留先出现的名称（顺序即优先级：做T区 > 防线 > 批次）
    seen: set[float] = set()
    deduped: list[PlaybookLevel] = []
    for lv in levels:
        if lv.price in seen:
            continue
        seen.add(lv.price)
        deduped.append(lv)
    return deduped


def _load_playbook_levels(
    symbol: str, tenant_id: int = DEFAULT_TENANT_ID
) -> list[PlaybookLevel]:
    """按 (tenant, symbol) 从库中读取本租户激活 playbook 并提取关键价位；任何异常返回 []。

    MT-P3（T15）：Stock / StockPlaybook 均为租户私有表，``tenant_scope`` 下
    do_orm_execute 机制点自动按 tenant 过滤，只读得到本租户 is_active 档案；
    单租户直通模式（PANWATCH_SINGLE_TENANT='1'，默认）过滤短路，行为与改造前等价。
    """
    try:
        from src.web.database import SessionLocal
        from src.web.models import Stock
        from src.web.tenant_context import tenant_scope

        from src.core.playbook import load_active_playbook

        with tenant_scope(tenant_id):
            db = SessionLocal()
            try:
                stock = db.query(Stock).filter(Stock.symbol == symbol).first()
                if stock is None:
                    return []
                playbook = load_active_playbook(db, stock.id)
                if playbook is None or not isinstance(playbook.payload, dict):
                    return []
                return extract_playbook_levels(playbook.payload)
            finally:
                db.close()
    except Exception:
        logger.exception("_load_playbook_levels 失败 symbol=%s", symbol)
        return []


def _tech_sig(kline_summary: dict | None) -> dict[str, Any]:
    ks = kline_summary or {}
    return {
        "trend": ks.get("trend"),
        "macd_status": ks.get("macd_status"),
        "rsi_status": ks.get("rsi_status"),
        "kdj_status": ks.get("kdj_status"),
        "boll_status": ks.get("boll_status"),
        "kline_pattern": ks.get("kline_pattern"),
    }


def _parse_iso(ts: Any) -> datetime | None:
    try:
        if not isinstance(ts, str) or not ts:
            return None
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def check_and_update(
    *,
    symbol: str,
    change_pct: float | None,
    volume_ratio: float | None,
    kline_summary: dict | None,
    price_threshold: float,
    volume_threshold: float,
    current_price: float | None = None,
    playbook_levels: list[PlaybookLevel] | None = None,
    tenant_id: int = DEFAULT_TENANT_ID,
) -> EventDecision:
    """Return whether we should analyze now, and persist latest state.

    新增可选参数（P3c，默认 None 时行为与改造前完全一致）：
    - current_price: 现价。提供时才启用方案价位穿越检测。
    - playbook_levels: 方案关键价位列表。None 时按 (tenant, symbol) 自动从库中读取
      本租户激活 playbook（fail-soft，读不到视为无方案）；显式传列表（含空列表）
      则直接使用、不再查库（测试/调用方自控）。

    MT-P3（T7）新增 tenant_id（默认 1，单租户期行为与改造前等价）：
    - 行情观测态（last_price 基线 / tech_sig / 观测记录）落 ``market.{symbol}``，
      全租户共享单份；
    - 穿越冷却 pb_fired 落 ``tenants.{tenant_id}.{symbol}``，冷却键
      ``方向:位名@价位`` 语义不变，跨租户互不影响。
    """

    path = _state_path()
    state = _load_state(path)
    market: dict[str, Any] = state["market"]
    rec: dict[str, Any] = market.get(symbol)
    if not isinstance(rec, dict):
        rec = {}
    tenants: dict[str, Any] = state["tenants"]
    tenant_recs = tenants.get(_tenant_key(tenant_id))
    if not isinstance(tenant_recs, dict):
        tenant_recs = {}
        tenants[_tenant_key(tenant_id)] = tenant_recs
    t_rec: dict[str, Any] = tenant_recs.get(symbol)
    if not isinstance(t_rec, dict):
        t_rec = {}

    reasons: list[str] = []

    # 1) Price move / volume spike thresholds
    cp = _safe_float(change_pct)
    if cp is not None and abs(cp) >= float(price_threshold or 0):
        reasons.append("price_threshold")

    vr = _safe_float(volume_ratio)
    if (
        vr is not None
        and float(volume_threshold or 0) > 0
        and vr >= float(volume_threshold)
    ):
        reasons.append("volume_threshold")

    # 2) Technical state changed
    new_sig = _tech_sig(kline_summary)
    old_sig = rec.get("tech_sig") if isinstance(rec.get("tech_sig"), dict) else None
    if old_sig is not None and old_sig != new_sig:
        reasons.append("tech_state_changed")

    # 3) 方案关键价位穿越（仅有 playbook 的股票；无方案/无价位时零改动）
    cur_price = _safe_float(current_price)
    if cur_price is not None and cur_price > 0:
        if playbook_levels is None:
            try:
                playbook_levels = _load_playbook_levels(symbol, tenant_id=tenant_id)
            except Exception:
                logger.exception("playbook 价位加载失败 symbol=%s", symbol)
                playbook_levels = []
        prev_price = _safe_float(rec.get("last_price"))
        if playbook_levels:
            fired = t_rec.get("pb_fired")
            if not isinstance(fired, dict):
                fired = {}
            now = datetime.now(timezone.utc)
            for lv in playbook_levels:
                if prev_price is None or prev_price <= 0 or prev_price == cur_price:
                    break  # 首次观测无价态基线，不判定穿越
                if prev_price < lv.price <= cur_price:
                    direction = "上穿"
                elif prev_price > lv.price >= cur_price:
                    direction = "下穿"
                else:
                    continue
                cool_key = f"{direction}:{lv.name}@{lv.price:g}"
                last = _parse_iso(fired.get(cool_key))
                if (
                    last is not None
                    and (now - last).total_seconds() < PLAYBOOK_CROSS_COOLDOWN_SEC
                ):
                    continue  # 冷却期内同向重复穿越不重复报
                reasons.append(f"playbook_cross:{direction}{lv.name}@{lv.price:g}")
                fired[cool_key] = now.isoformat()
            t_rec["pb_fired"] = fired
            tenant_recs[symbol] = t_rec
        rec["last_price"] = cur_price

    # Persist latest observation（市场观测态，全租户共享单份）
    rec["last_seen_at"] = _now_iso()
    rec["change_pct"] = cp
    rec["volume_ratio"] = vr
    rec["tech_sig"] = new_sig
    market[symbol] = rec
    write_json_atomic(path, state)

    return EventDecision(should_analyze=bool(reasons), reasons=reasons)
