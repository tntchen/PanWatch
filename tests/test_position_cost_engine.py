"""持仓交易成本引擎纯函数测试（src/core/position_trading.py）。

精度两级断言（doc/14 §1 P1）：
- Decimal 层精确相等（一步运算 / 有限小数场景）；
- 多步链式（含无限循环小数中间步）按 1e-6 量化精确断言，float 读回层容差 ≤0.001。
锚点：东芯 1600 股 @138.272 + 两笔补仓 → 3150 股 @112.572。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.position_trading import (
    InsufficientPositionError,
    apply_adjustment,
    apply_buy,
    apply_sell,
    build_adjustment_note,
)

D0 = Decimal("0")


# ---------------------------------------------------------------- 买入


def test_buy_weighted_average_cost():
    """加仓移动加权平均：100@10 + 100@8 → 200 股 @9.0（Decimal 精确相等）。"""
    comp = apply_buy(
        cost_price=10, quantity=100, invested_amount=1000,
        available_funds=5000, price=8, buy_quantity=100, fee=0,
    )
    assert comp.new_cost_price == Decimal("9.0")
    assert comp.new_quantity == 200
    assert comp.new_invested_amount == Decimal("1800")
    assert comp.new_available_funds == Decimal("4200")
    assert comp.realized_pnl is None


def test_buy_fee_included_in_cost_and_funds():
    """手续费计入成本与资金扣减：新成本 = (旧成本×旧数量 + 价×量 + fee) / 新数量。"""
    comp = apply_buy(
        cost_price=10, quantity=100, invested_amount=1000,
        available_funds=5000, price=8, buy_quantity=100, fee=10,
    )
    # (1000 + 800 + 10) / 200 = 9.05
    assert comp.new_cost_price == Decimal("9.05")
    assert comp.new_invested_amount == Decimal("1810")
    assert comp.new_available_funds == Decimal("4190")


def test_buy_from_zero_position():
    """空仓（quantity=0，如关仓留行后再建仓）走 buy：成本=买入口径价。"""
    comp = apply_buy(
        cost_price=0, quantity=0, invested_amount=0,
        available_funds=10000, price=138.272, buy_quantity=1600, fee=0,
    )
    assert comp.new_cost_price == Decimal("138.272")
    assert comp.new_quantity == 1600
    assert comp.new_invested_amount == Decimal("138.272") * 1600
    assert comp.new_available_funds == Decimal("10000") - Decimal("138.272") * 1600


def test_buy_rejects_non_positive_quantity():
    """买入数量必须 > 0。"""
    with pytest.raises(ValueError):
        apply_buy(10, 100, 1000, 5000, 8, 0)


def test_dongxin_anchor_three_buys_cost_112_572():
    """东芯锚点：1600@138.272 → +1000@95 → +550@68(fee 966.6) ⇒ 3150 股 @112.572。

    中间步 316235.2/2600 = 121.628923... 为无限循环小数，Decimal(28 位) 无法
    逐位精确，故 Decimal 层按 1e-6 量化断言精确值；float 读回层容差 ≤0.001。
    """
    funds = Decimal("1000000")
    cost, qty, invested = D0, 0, D0

    comp = apply_buy(cost, qty, invested, funds, "138.272", 1600, 0)
    cost, qty, invested, funds = (
        comp.new_cost_price, comp.new_quantity,
        comp.new_invested_amount, comp.new_available_funds,
    )
    assert cost == Decimal("138.272")  # 首笔 Decimal 精确

    comp = apply_buy(cost, qty, invested, funds, "95", 1000, 0)
    cost, qty, invested, funds = (
        comp.new_cost_price, comp.new_quantity,
        comp.new_invested_amount, comp.new_available_funds,
    )
    assert qty == 2600

    comp = apply_buy(cost, qty, invested, funds, "68", 550, "966.6")
    q6 = Decimal("0.000001")
    assert comp.new_cost_price.quantize(q6) == Decimal("112.572000")
    assert comp.new_quantity == 3150
    assert comp.new_invested_amount.quantize(Decimal("0.01")) == Decimal("354601.80")

    # float 读回层容差 ≤0.001（落库 float 后的验收口径）
    assert abs(float(comp.new_cost_price) - 112.572) <= 0.001
    assert abs(float(comp.new_invested_amount) - 354601.8) <= 0.001


# ---------------------------------------------------------------- 卖出


def test_sell_cost_unchanged_and_realized_pnl():
    """减仓：成本不变；realized_pnl = (卖价−成本)×数量 − fee；投入按比例结转。"""
    comp = apply_sell(
        cost_price=9, quantity=200, invested_amount=1800,
        available_funds=4200, price=12, sell_quantity=50, fee=5,
    )
    assert comp.new_cost_price == Decimal("9")  # 成本不变
    assert comp.new_quantity == 150
    assert comp.realized_pnl == Decimal("145")  # (12-9)*50 - 5
    # invested 按 150/200 结转
    assert comp.new_invested_amount == Decimal("1350")
    # 资金回流 12*50 - 5
    assert comp.new_available_funds == Decimal("4795")


def test_sell_to_zero_closes_position_and_keeps_row_semantics():
    """减至 0 = 关仓：数量为 0、投入结转归零、成本字段保留（行不删由 API 层保证）。"""
    comp = apply_sell(
        cost_price=112.572, quantity=3150, invested_amount=354601.8,
        available_funds=150000, price=130, sell_quantity=3150, fee=100,
    )
    assert comp.new_quantity == 0
    assert comp.new_cost_price == Decimal("112.572")  # 成本不变（留行口径）
    assert comp.new_invested_amount == D0
    assert comp.realized_pnl == (Decimal("130") - Decimal("112.572")) * 3150 - 100
    assert comp.new_available_funds == Decimal("150000") + Decimal("130") * 3150 - 100


def test_sell_oversell_rejected():
    """卖出数量 > 持仓数量 必须抛 InsufficientPositionError（API 层映射 400）。"""
    with pytest.raises(InsufficientPositionError):
        apply_sell(9, 200, 1800, 4200, 12, 201)


def test_sell_rejects_non_positive_quantity():
    """卖出数量必须 > 0。"""
    with pytest.raises(ValueError):
        apply_sell(9, 200, 1800, 4200, 12, 0)


def test_sell_fee_boundaries_zero_and_large():
    """手续费边界：fee=0 时 realized 不含费用；大额 fee 可为负 realized（亏损离场）。"""
    comp0 = apply_sell(9, 200, 1800, 4200, 12, 50, fee=0)
    assert comp0.realized_pnl == Decimal("150")

    comp_big = apply_sell(9, 200, 1800, 4200, 12, 50, fee=1000)
    assert comp_big.realized_pnl == Decimal("-850")
    # 大额手续费同样扣减回流资金
    assert comp_big.new_available_funds == Decimal("4200") + 600 - 1000


def test_sell_invested_fallback_when_none():
    """invested_amount 为 None 时按 成本×数量 兜底再按比例结转。"""
    comp = apply_sell(9, 200, None, 4200, 12, 100, fee=0)
    # 兜底 invested = 9*200 = 1800，结转 100/200 → 900
    assert comp.new_invested_amount == Decimal("900")


def test_float_roundtrip_tolerance():
    """float 读回层：Decimal 结果转 float 后与期望值容差 ≤0.001。"""
    comp = apply_sell(112.572, 3150, 354601.8, 150000, 98, 500, fee=30)
    expected = (98 - 112.572) * 500 - 30
    assert abs(float(comp.realized_pnl) - expected) <= 0.001
    assert abs(float(comp.new_invested_amount) - 354601.8 * 2650 / 3150) <= 0.001


# ---------------------------------------------------------------- adjustment


def test_adjustment_changes_cost_qty_only():
    """adjustment：只改成本/数量口径，不联动资金（funds 字段为占位 0，调用方不得采用）。"""
    comp = apply_adjustment(old_cost_price=10, old_quantity=100,
                            new_cost_price=9.5, new_quantity=150)
    assert comp.direction == "adjustment"
    assert comp.new_cost_price == Decimal("9.5")
    assert comp.new_quantity == 150
    assert comp.realized_pnl is None
    assert comp.new_available_funds == D0  # 占位：不联动资金


def test_adjustment_note_records_old_to_new():
    """adjustment 流水备注记录 old→new。"""
    note = build_adjustment_note(10, 100, 9.5, 150)
    assert "10" in note and "9.5" in note
    assert "100" in note and "150" in note
    assert "→" in note
