"""价格提醒新条件（P2b 触发器扩展）单测。

覆盖 doc/14 §1 P2b 要求：
- turnover_rate：CN 报价带字段直接评估；HK/US 规则引擎层拒绝（双层门控的引擎层）。
- consecutive_close：最近 N 根日 K 收盘价全部满足才命中；K 线不足/异常 fail-safe 不触发不抛异常。
- capital_flow：走 CapitalFlowCollector 600s 缓存（不直拉数据源），单位换算为万元。
- playbook_id：命中通知文案追加方案提示。
- _send_notify 注入统一 NotifyPolicy。
- 现有 5 种条件回归。
- v119 迁移冒烟（幂等加列）。
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text

from src.core.price_alert_engine import PriceAlertEngine
from src.models.market import MarketCode


# ---------------------------------------------------------------------------
# turnover_rate（换手率 %，即时值，CN-only）
# ---------------------------------------------------------------------------


def test_turnover_rate_cn_quote_eval():
    """CN 报价带 turnover_rate 字段时直接评估命中。"""
    eng = PriceAlertEngine()
    quote = {"current_price": 120.0, "turnover_rate": 8.5}
    ok, detail = asyncio.run(
        eng._eval_condition(
            {"type": "turnover_rate", "op": ">=", "value": 8.0},
            quote,
            MarketCode.CN,
            "688110",
        )
    )
    assert ok is True
    assert detail["actual"] == 8.5
    assert detail["matched"] is True


def test_turnover_rate_between_and_miss():
    """turnover_rate 支持 between；不满足时不命中。"""
    eng = PriceAlertEngine()
    quote = {"turnover_rate": 5.0}
    ok, detail = asyncio.run(
        eng._eval_condition(
            {"type": "turnover_rate", "op": "between", "value": [3.0, 10.0]},
            quote,
            MarketCode.CN,
            "688110",
        )
    )
    assert ok is True

    ok2, _ = asyncio.run(
        eng._eval_condition(
            {"type": "turnover_rate", "op": ">", "value": 9.0},
            quote,
            MarketCode.CN,
            "688110",
        )
    )
    assert ok2 is False

    # 报价缺字段 → fail-safe 不触发
    ok3, detail3 = asyncio.run(
        eng._eval_condition(
            {"type": "turnover_rate", "op": ">", "value": 1.0},
            {"current_price": 10.0},
            MarketCode.CN,
            "688110",
        )
    )
    assert ok3 is False
    assert detail3["actual"] is None


# ---------------------------------------------------------------------------
# CN-only 双层门控的引擎层：HK/US 拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("market", [MarketCode.HK, MarketCode.US])
@pytest.mark.parametrize(
    "cond",
    [
        {"type": "turnover_rate", "op": ">", "value": 1.0},
        {"type": "capital_flow", "op": ">", "value": 0.0},
        {"type": "consecutive_close", "op": "<", "value": 98.0, "days": 2},
    ],
)
def test_cn_only_conditions_rejected_for_hk_us(market, cond):
    """HK/US 个股配 A 股专属条件时引擎层拒绝（不触发、标记 cn_only）。"""
    eng = PriceAlertEngine()
    quote = {"current_price": 10.0, "turnover_rate": 99.0}
    ok, detail = asyncio.run(eng._eval_condition(cond, quote, market, "00700"))
    assert ok is False
    assert detail["error"] == "cn_only"
    assert detail["matched"] is False


def test_rule_level_cn_gate_skips_whole_rule():
    """非 CN 股票的规则只要含 A 股专属条件，整条规则跳过并记日志。"""
    eng = PriceAlertEngine()
    rule = SimpleNamespace(
        id=42,
        stock=SimpleNamespace(market="HK", symbol="00700"),
        condition_group={
            "op": "and",
            "items": [
                {"type": "price", "op": ">", "value": 1.0},
                {"type": "turnover_rate", "op": ">", "value": 1.0},
            ],
        },
    )
    result = asyncio.run(eng.eval_rule(rule, {"current_price": 999.0}))
    assert result.matched is False
    assert result.snapshot["error"] == "cn_only_condition"
    assert result.hits == []


# ---------------------------------------------------------------------------
# consecutive_close（连续 N 日收盘满足条件）
# ---------------------------------------------------------------------------


def test_consecutive_close_hit_uses_recent_window(monkeypatch):
    """取最近 N 根日 K 收盘价（窗口方向=最新 N 根），全部满足才命中。"""
    eng = PriceAlertEngine()

    async def fake_closes(market, symbol, days):
        assert days == 2
        return [100.0, 101.0, 97.5, 96.8]  # 最近 2 根为 97.5 / 96.8

    monkeypatch.setattr(eng, "_get_daily_closes_cached", fake_closes)
    ok, detail = asyncio.run(
        eng._eval_condition(
            {"type": "consecutive_close", "op": "<", "value": 98.0, "days": 2},
            {"current_price": 96.0},
            MarketCode.CN,
            "688110",
        )
    )
    assert ok is True
    assert detail["days"] == 2
    assert detail["actual"] == [97.5, 96.8], "窗口应取最近 N 根而非最早 N 根"

    # 最近 2 根并非全部满足 → 不命中
    ok2, _ = asyncio.run(
        eng._eval_condition(
            {"type": "consecutive_close", "op": "<", "value": 97.0, "days": 2},
            {"current_price": 96.0},
            MarketCode.CN,
            "688110",
        )
    )
    assert ok2 is False


def test_consecutive_close_insufficient_kline_fail_safe(monkeypatch):
    """K 线不足 N 根时 fail-safe：不触发、不抛异常、标记 insufficient_kline。"""
    eng = PriceAlertEngine()

    async def fake_closes(market, symbol, days):
        return [100.0]  # 只有 1 根，不足 days=2

    monkeypatch.setattr(eng, "_get_daily_closes_cached", fake_closes)
    ok, detail = asyncio.run(
        eng._eval_condition(
            {"type": "consecutive_close", "op": "<", "value": 98.0, "days": 2},
            {},
            MarketCode.CN,
            "688110",
        )
    )
    assert ok is False
    assert detail["error"] == "insufficient_kline"


def test_consecutive_close_missing_days_fail_safe():
    """缺 days 参数时 fail-safe 不触发。"""
    eng = PriceAlertEngine()
    ok, detail = asyncio.run(
        eng._eval_condition(
            {"type": "consecutive_close", "op": "<", "value": 98.0},
            {},
            MarketCode.CN,
            "688110",
        )
    )
    assert ok is False
    assert detail["error"] == "insufficient_kline"


def test_consecutive_close_kline_error_fail_safe(monkeypatch):
    """K 线源返回空（无K线数据的真实行为）或抛异常时，_get_daily_closes_cached 不抛异常。"""
    from src.collectors.kline_collector import KlineCollector

    eng = PriceAlertEngine()
    monkeypatch.setattr(KlineCollector, "get_klines", lambda self, symbol, days=60: [])
    closes = asyncio.run(eng._get_daily_closes_cached(MarketCode.CN, "688110", 2))
    assert closes == []

    def _boom(self, symbol, days=60):
        raise RuntimeError("源故障")

    eng2 = PriceAlertEngine()
    monkeypatch.setattr(KlineCollector, "get_klines", _boom)
    closes2 = asyncio.run(eng2._get_daily_closes_cached(MarketCode.CN, "688110", 2))
    assert closes2 == []


# ---------------------------------------------------------------------------
# capital_flow（主力净流入，万元，走 600s 缓存）
# ---------------------------------------------------------------------------


def test_capital_flow_uses_collector_cache(monkeypatch):
    """capital_flow 走 CapitalFlowCollector 600s TTL 缓存：第二次评估不再直拉数据源。"""
    import src.collectors.capital_flow_collector as cfc

    calls = {"n": 0}

    fake_cf = SimpleNamespace(
        symbol="688110",
        name="东芯股份",
        main_net_inflow=5_000_000.0,  # 元 → 500 万元
        main_net_inflow_pct=6.0,
        super_net_inflow=3_000_000.0,
        big_net_inflow=2_000_000.0,
        mid_net_inflow=-1_000_000.0,
        small_net_inflow=-4_000_000.0,
        main_net_5d=None,
    )

    class _FakeMd:
        def capital_flow(self, symbol, market=None):
            calls["n"] += 1
            return fake_cf

    monkeypatch.setattr(cfc, "get_market_data", lambda: _FakeMd())

    cond = {"type": "capital_flow", "op": ">=", "value": 500.0}  # 万元
    ok, detail = asyncio.run(
        PriceAlertEngine()._eval_condition(cond, {}, MarketCode.CN, "688110")
    )
    assert ok is True
    assert detail["actual"] == 500.0, "主力净流入应按万元评估"
    assert calls["n"] == 1

    # 全新引擎实例再次评估：模块级 600s 缓存命中，不再直拉数据源
    ok2, _ = asyncio.run(
        PriceAlertEngine()._eval_condition(cond, {}, MarketCode.CN, "688110")
    )
    assert ok2 is True
    assert calls["n"] == 1, "600s 缓存窗口内不应重复直拉数据源"


def test_capital_flow_no_data_fail_safe(monkeypatch):
    """资金流无数据时 fail-safe 不触发。"""
    import src.collectors.capital_flow_collector as cfc

    class _FakeMd:
        def capital_flow(self, symbol, market=None):
            return None

    monkeypatch.setattr(cfc, "get_market_data", lambda: _FakeMd())
    ok, detail = asyncio.run(
        PriceAlertEngine()._eval_condition(
            {"type": "capital_flow", "op": ">", "value": 0.0},
            {},
            MarketCode.CN,
            "688110",
        )
    )
    assert ok is False
    assert detail["actual"] is None
    assert detail["error"] == "no_capital_flow"


# ---------------------------------------------------------------------------
# playbook hint 进通知 + NotifyPolicy 注入
# ---------------------------------------------------------------------------


class _RecorderNotifier:
    captured: dict = {}

    def __init__(self, policy=None):
        _RecorderNotifier.captured = {"policy": policy}

    def add_channel(self, channel_type, config):
        pass

    async def notify_with_result(self, title, content, **kwargs):
        _RecorderNotifier.captured["title"] = title
        _RecorderNotifier.captured["content"] = content
        return {"success": True}


def _fake_rule(**over):
    base = dict(
        id=7,
        name="东芯-连续2日收盘<98",
        playbook_id=None,
        notify_channel_ids=[],
        stock=SimpleNamespace(symbol="688110", name="东芯股份"),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _snapshot():
    return {
        "quote": {"current_price": 97.5, "change_pct": -3.2},
        "conditions": [
            {"type": "consecutive_close", "op": "<", "target": 98.0, "actual": [97.5, 96.8], "matched": True}
        ],
    }


def test_playbook_hint_appended_to_notify(monkeypatch):
    """规则关联方案时，命中通知文案追加 get_trigger_hint 返回的方案提示。"""
    eng = PriceAlertEngine()
    monkeypatch.setattr(eng, "_resolve_channels", lambda db, rule: [])
    monkeypatch.setattr(
        "src.core.price_alert_engine.NotifierManager", _RecorderNotifier
    )

    fake_pb = types.ModuleType("src.core.playbook")
    fake_pb.get_trigger_hint = lambda db, pid, rule_name: "防线触发：按方案减仓1/3~1/2"
    monkeypatch.setitem(sys.modules, "src.core.playbook", fake_pb)

    ok, err = asyncio.run(eng._send_notify(None, _fake_rule(playbook_id=3), _snapshot()))
    assert ok is True and err == ""
    content = _RecorderNotifier.captured["content"]
    assert "方案提示: 防线触发：按方案减仓1/3~1/2" in content


def test_notify_policy_injected(monkeypatch):
    """_send_notify 必须注入统一 NotifyPolicy（不得裸建 NotifierManager）。"""
    from src.core.notify_policy import NotifyPolicy

    eng = PriceAlertEngine()
    monkeypatch.setattr(eng, "_resolve_channels", lambda db, rule: [])
    monkeypatch.setattr(
        "src.core.price_alert_engine.NotifierManager", _RecorderNotifier
    )

    ok, _ = asyncio.run(eng._send_notify(None, _fake_rule(), _snapshot()))
    assert ok is True
    policy = _RecorderNotifier.captured.get("policy")
    assert isinstance(policy, NotifyPolicy), "NotifierManager 应注入 NotifyPolicy"
    # 无 playbook_id 时不追加方案提示
    assert "方案提示" not in _RecorderNotifier.captured["content"]


def test_playbook_hint_failure_does_not_block_notify(monkeypatch):
    """方案提示读取异常时通知照常发送（容错）。"""
    eng = PriceAlertEngine()
    monkeypatch.setattr(eng, "_resolve_channels", lambda db, rule: [])
    monkeypatch.setattr(
        "src.core.price_alert_engine.NotifierManager", _RecorderNotifier
    )

    fake_pb = types.ModuleType("src.core.playbook")

    def _boom(db, pid, rule_name):
        raise RuntimeError("档案损坏")

    fake_pb.get_trigger_hint = _boom
    monkeypatch.setitem(sys.modules, "src.core.playbook", fake_pb)

    ok, _ = asyncio.run(eng._send_notify(None, _fake_rule(playbook_id=3), _snapshot()))
    assert ok is True
    assert "方案提示" not in _RecorderNotifier.captured["content"]


# ---------------------------------------------------------------------------
# 现有 5 种条件回归
# ---------------------------------------------------------------------------


def test_existing_five_condition_types_regression(monkeypatch):
    """price/change_pct/turnover/volume/volume_ratio 五种旧条件行为不变。"""
    eng = PriceAlertEngine()

    async def _no_kline(market, symbol):
        raise AssertionError("报价带量比时不应回退 K线")

    monkeypatch.setattr(eng, "_get_kline_summary_cached", _no_kline)

    quote = {
        "current_price": 120.0,
        "change_pct": -3.5,
        "turnover": 12.3e8,
        "volume": 5.6e6,
        "volume_ratio": 2.5,
    }
    cases = [
        ({"type": "price", "op": ">=", "value": 120.0}, True),
        ({"type": "price", "op": "<", "value": 100.0}, False),
        ({"type": "change_pct", "op": "<=", "value": -3.0}, True),
        ({"type": "turnover", "op": ">", "value": 1e8}, True),
        ({"type": "volume", "op": "between", "value": [1e6, 1e7]}, True),
        ({"type": "volume_ratio", "op": ">", "value": 2.0}, True),
    ]
    for cond, expected in cases:
        ok, _ = asyncio.run(eng._eval_condition(cond, quote, MarketCode.CN, "688110"))
        assert ok is expected, f"旧条件回归失败: {cond}"

    # 未知类型仍然 unsupported
    ok, detail = asyncio.run(
        eng._eval_condition({"type": "nope", "op": ">", "value": 1}, quote, MarketCode.CN, "688110")
    )
    assert ok is False
    assert detail["error"] == "unsupported_type"


# ---------------------------------------------------------------------------
# API 层校验（白名单 + days + turnover_rate op 子集）
# ---------------------------------------------------------------------------


def test_api_condition_validation_new_types():
    """条件白名单接受 3 个新类型；days 必填且 1-10；turnover_rate op 受限。"""
    from src.web.api.price_alerts import (
        AlertConditionGroup,
        AlertConditionItem,
        _validate_condition_group,
    )

    def _group(item):
        return AlertConditionGroup(op="and", items=[item])

    # 合法：三个新类型
    _validate_condition_group(
        _group(AlertConditionItem(type="turnover_rate", op=">=", value=5.0))
    )
    _validate_condition_group(
        _group(AlertConditionItem(type="capital_flow", op=">", value=100.0))
    )
    _validate_condition_group(
        _group(
            AlertConditionItem(type="consecutive_close", op="<", value=98.0, days=2)
        )
    )

    # consecutive_close 缺 days → 400
    with pytest.raises(HTTPException):
        _validate_condition_group(
            _group(AlertConditionItem(type="consecutive_close", op="<", value=98.0))
        )
    # days 越界 → 400
    with pytest.raises(HTTPException):
        _validate_condition_group(
            _group(
                AlertConditionItem(type="consecutive_close", op="<", value=98.0, days=11)
            )
        )
    # turnover_rate 用 == → 400（契约仅支持 >=/<=/>/</between）
    with pytest.raises(HTTPException):
        _validate_condition_group(
            _group(AlertConditionItem(type="turnover_rate", op="==", value=5.0))
        )


# ---------------------------------------------------------------------------
# v119 迁移冒烟（幂等加列 + 注册顺序）
# ---------------------------------------------------------------------------


def test_m119_adds_playbook_id_idempotent():
    """v119 迁移在旧库上幂等新增 playbook_id 列。"""
    from src.web.migrations import _m119_price_alert_rule_playbook_id

    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE price_alert_rules (id INTEGER PRIMARY KEY)"))
        _m119_price_alert_rule_playbook_id(conn)
        cols = [
            r[1]
            for r in conn.execute(text("PRAGMA table_info(price_alert_rules)")).fetchall()
        ]
        assert "playbook_id" in cols
        # 重复执行不抛异常（幂等）
        _m119_price_alert_rule_playbook_id(conn)


def test_m119_registered_last_after_118():
    """v119 注册在迁移表末尾（118 之后），已发布迁移未被改动。"""
    from src.web.migrations import MIGRATIONS

    versions = [m.version for m in MIGRATIONS]
    assert versions[-1] == 119
    assert versions[-2] == 118
    assert len(set(versions)) == len(versions)
