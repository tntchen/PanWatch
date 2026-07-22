"""个股方案档案（stock_playbooks）读取与摘要生成 —— Phase 2 P2a。

契约 C（四方一致）：
- ``load_active_playbook(db, stock_id)``：读激活版本，无档案返回 None。
- ``get_trigger_hint(db, playbook_id, rule_name)``：按价格提醒规则名匹配
  ``payload.trigger_hints``，无匹配返回 None。
- ``summarize_playbook(payload)``：生成紧凑中文摘要（≤500 token），含
  价位表/批次状态/防线/做T区/临近30天日历项/策略模式。
- 所有函数容错：payload 缺 schema_version 或字段缺失不抛异常。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from src.web.models import StockPlaybook

logger = logging.getLogger(__name__)

# 摘要长度预算（字符数）。CJK 文本 1 字 ≈ 1 token、ASCII 1 token ≈ 4 字符，
# 因此 len(text) 是 token 数的保守上界；预算取 500 字符即保证 ≤500 token。
SUMMARY_CHAR_BUDGET = 500

_STATUS_LABELS = {
    "executed": "已执行",
    "frozen": "冻结",
    "pending": "待触发",
}


def load_active_playbook(db, stock_id: int) -> StockPlaybook | None:
    """读取某股票当前激活的方案档案；无档案或查询异常返回 None。"""
    try:
        return (
            db.query(StockPlaybook)
            .filter(
                StockPlaybook.stock_id == stock_id,
                StockPlaybook.is_active.is_(True),
            )
            .order_by(StockPlaybook.version.desc(), StockPlaybook.id.desc())
            .first()
        )
    except Exception:
        logger.exception("load_active_playbook 失败 stock_id=%s", stock_id)
        return None


def get_trigger_hint(db, playbook_id: int, rule_name: str) -> str | None:
    """按价格提醒规则名取方案提示文案；无匹配/档案不存在/异常均返回 None。"""
    try:
        if not rule_name:
            return None
        row = db.query(StockPlaybook).filter(StockPlaybook.id == playbook_id).first()
        if not row or not isinstance(row.payload, dict):
            return None
        hints = row.payload.get("trigger_hints")
        if not isinstance(hints, dict):
            return None
        exact = hints.get(rule_name)
        if isinstance(exact, str) and exact.strip():
            return exact
        stripped = rule_name.strip()
        for key, value in hints.items():
            if (
                isinstance(key, str)
                and key.strip() == stripped
                and isinstance(value, str)
                and value.strip()
            ):
                return value
        return None
    except Exception:
        logger.exception(
            "get_trigger_hint 失败 playbook_id=%s rule=%r", playbook_id, rule_name
        )
        return None


def _fmt_num(value: Any) -> str:
    """100.0 -> '100'；209.55 -> '209.55'；不可解析时原样字符串化。"""
    try:
        f = float(value)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_range(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return f"{_fmt_num(value[0])}-{_fmt_num(value[1])}"
    return _fmt_num(value) if value is not None else ""


def _section_meta(payload: dict) -> str:
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    parts: list[str] = []
    name = str(meta.get("name") or "").strip()
    version_label = str(meta.get("version_label") or "").strip()
    if name or version_label:
        parts.append(f"方案:{name} {version_label}".strip())
    strategy_mode = str(meta.get("strategy_mode") or "").strip()
    if strategy_mode:
        parts.append(f"策略:{strategy_mode}")
    base_date = str(meta.get("base_date") or "").strip()
    base_price = meta.get("base_price")
    if base_date and base_price is not None:
        parts.append(f"基准:{base_date}@{_fmt_num(base_price)}")
    elif base_date:
        parts.append(f"基准:{base_date}")
    return "｜".join(parts)


def _section_price_levels(payload: dict) -> str:
    levels = payload.get("price_levels")
    if not isinstance(levels, list):
        return ""
    items = []
    for lv in levels:
        if not isinstance(lv, dict):
            continue
        label = str(lv.get("label") or "").strip()
        value = lv.get("value")
        if label and value is not None:
            items.append(f"{label}{_fmt_num(value)}")
    return "价位:" + "/".join(items) if items else ""


def _section_batches(payload: dict) -> str:
    batches = payload.get("batches")
    if not isinstance(batches, list):
        return ""
    items = []
    for b in batches:
        if not isinstance(b, dict):
            continue
        name = str(b.get("name") or "").strip()
        trigger = str(b.get("trigger") or "").strip()
        status = _STATUS_LABELS.get(str(b.get("status") or ""), str(b.get("status") or ""))
        if name:
            items.append(f"{name}{trigger}{status}")
    return "批次:" + " ".join(items) if items else ""


def _section_t_zone(payload: dict) -> str:
    tz = payload.get("t_zone")
    if not isinstance(tz, dict):
        return ""
    sell = _fmt_range(tz.get("sell_range"))
    buyback = _fmt_range(tz.get("buyback_range"))
    size = str(tz.get("size") or "").strip()
    mode = str(tz.get("mode") or "").strip()
    parts = []
    if sell:
        parts.append(f"{sell}卖{size}" if size else f"{sell}卖")
    if buyback:
        parts.append(f"{buyback}接回")
    if mode:
        parts.append(f"({mode})")
    return "做T:" + ",".join(parts) if parts else ""


def _section_defense(payload: dict) -> str:
    defense = payload.get("defense")
    if not isinstance(defense, dict):
        return ""
    rule = str(defense.get("rule") or "").strip()
    action = str(defense.get("action") or "").strip()
    if not rule and not action:
        return ""
    return f"防线:{rule}→{action}" if action else f"防线:{rule}"


def _section_stop_loss(payload: dict) -> str:
    tracks = payload.get("stop_loss_tracks")
    if not isinstance(tracks, list):
        return ""
    items = []
    for t in tracks:
        if not isinstance(t, dict):
            continue
        track = str(t.get("track") or "").strip()
        trigger = str(t.get("trigger") or "").strip()
        action = str(t.get("action") or "").strip()
        if track:
            seg = track
            if trigger:
                seg += f"({trigger[:24]})"
            if action:
                seg += f"→{action[:20]}"
            items.append(seg)
    return "止损轨:" + ";".join(items) if items else ""


def _section_calendar(payload: dict, today: date | None = None) -> str:
    cal = payload.get("calendar")
    if not isinstance(cal, list):
        return ""
    today = today or date.today()
    horizon = today + timedelta(days=30)
    items = []
    for entry in cal:
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("date") or "").strip()
        event = str(entry.get("event") or "").strip()
        if not raw or not event:
            continue
        try:
            d = date.fromisoformat(raw[:10])
        except ValueError:
            continue
        if today <= d <= horizon:
            items.append(f"{raw[5:10]}{event}")
    return "日历30天:" + ";".join(items) if items else ""


def _section_scenarios(payload: dict) -> str:
    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        return ""
    items = []
    for s in scenarios:
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "").strip()
        action = str(s.get("action") or "").strip()
        if name:
            items.append(f"{name}→{action[:20]}" if action else name)
    return "情景:" + ";".join(items) if items else ""


def summarize_playbook(payload: dict) -> str:
    """从契约 A payload 生成紧凑中文摘要（≤500 token，缺字段容错不抛）。

    章节按优先级排序：策略/价位/批次/做T/防线/日历为契约 B 必需项，排在前；
    超预算时从尾部（止损轨/情景）开始舍弃，保证必需项完整。
    """
    if not isinstance(payload, dict):
        return ""
    try:
        sections = [
            _section_meta(payload),
            _section_price_levels(payload),
            _section_batches(payload),
            _section_t_zone(payload),
            _section_defense(payload),
            _section_calendar(payload),
            _section_stop_loss(payload),
            _section_scenarios(payload),
        ]
        sections = [s for s in sections if s]
        while sections and len("\n".join(sections)) > SUMMARY_CHAR_BUDGET:
            sections.pop()  # 舍弃最低优先级章节
        return "\n".join(sections)
    except Exception:
        logger.exception("summarize_playbook 失败")
        return ""
