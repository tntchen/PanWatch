"""持仓交易成本引擎（纯函数，Decimal 计算，落库 float 由调用方负责）。

语义约定（doc/12 §3 Phase 1，定死不得变更）：
- 买入：移动加权平均。新成本 = (旧成本×旧数量 + 价格×数量 + fee) / (旧数量+数量)；
  invested_amount += 价格×数量 + fee；available_funds -= 价格×数量 + fee。
- 卖出：成本不变；realized_pnl = (价格−成本)×数量 − fee；数量减少（归零=关仓留行）；
  invested_amount 按 剩余/原 比例结转；available_funds += 价格×数量 − fee。
- adjustment：只记录流水，不改资金（本模块仅生成计算结果与 note）。

输入接受 int/float/str/Decimal，内部统一 Decimal(str(x)) 转换，输出均为 Decimal。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")


class InsufficientPositionError(ValueError):
    """卖出数量超过持仓数量。"""


@dataclass(frozen=True)
class TradeComputation:
    """一笔交易应用后的新状态（均为 Decimal，调用方转 float 落库）。"""

    direction: str  # buy / sell / adjustment
    new_cost_price: Decimal
    new_quantity: int
    new_invested_amount: Decimal
    new_available_funds: Decimal
    realized_pnl: Decimal | None = None  # 仅 sell 有值


def _d(value: int | float | str | Decimal | None) -> Decimal:
    """统一转 Decimal；None 视为 0。"""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _base_invested(
    cost_price: int | float | str | Decimal | None,
    quantity: int,
    invested_amount: int | float | str | Decimal | None,
) -> Decimal:
    """invested_amount 为 None 时以 成本×数量 兜底（历史手工维护的数据）。"""
    if invested_amount is not None:
        return _d(invested_amount)
    return _d(cost_price) * int(quantity)


def apply_buy(
    cost_price: int | float | str | Decimal,
    quantity: int,
    invested_amount: int | float | str | Decimal | None,
    available_funds: int | float | str | Decimal | None,
    price: int | float | str | Decimal,
    buy_quantity: int,
    fee: int | float | str | Decimal = 0,
) -> TradeComputation:
    """买入（建仓/加仓）：移动加权平均重算成本，资金扣减 价格×数量+fee。"""
    if buy_quantity <= 0:
        raise ValueError("买入数量必须 > 0")
    old_cost = _d(cost_price)
    old_qty = int(quantity)
    p = _d(price)
    f = _d(fee)
    outlay = p * buy_quantity + f

    new_qty = old_qty + buy_quantity
    new_cost = (old_cost * old_qty + outlay) / new_qty
    new_invested = _base_invested(old_cost, old_qty, invested_amount) + outlay
    new_funds = _d(available_funds) - outlay

    return TradeComputation(
        direction="buy",
        new_cost_price=new_cost,
        new_quantity=new_qty,
        new_invested_amount=new_invested,
        new_available_funds=new_funds,
        realized_pnl=None,
    )


def apply_sell(
    cost_price: int | float | str | Decimal,
    quantity: int,
    invested_amount: int | float | str | Decimal | None,
    available_funds: int | float | str | Decimal | None,
    price: int | float | str | Decimal,
    sell_quantity: int,
    fee: int | float | str | Decimal = 0,
) -> TradeComputation:
    """卖出（减仓/清仓）：成本不变，按比例结转投入与已实现盈亏。"""
    if sell_quantity <= 0:
        raise ValueError("卖出数量必须 > 0")
    old_qty = int(quantity)
    if sell_quantity > old_qty:
        raise InsufficientPositionError(
            f"卖出数量 {sell_quantity} 超过持仓数量 {old_qty}"
        )

    cost = _d(cost_price)
    p = _d(price)
    f = _d(fee)
    proceeds = p * sell_quantity - f

    new_qty = old_qty - sell_quantity
    realized = (p - cost) * sell_quantity - f
    old_invested = _base_invested(cost, old_qty, invested_amount)
    # invested_amount 按 剩余/原 比例结转（关仓时归零）
    new_invested = (
        old_invested * new_qty / old_qty if old_qty > 0 else ZERO
    )
    new_funds = _d(available_funds) + proceeds

    return TradeComputation(
        direction="sell",
        new_cost_price=cost,  # 成本不变
        new_quantity=new_qty,
        new_invested_amount=new_invested,
        new_available_funds=new_funds,
        realized_pnl=realized,
    )


def apply_adjustment(
    old_cost_price: int | float | str | Decimal,
    old_quantity: int,
    new_cost_price: int | float | str | Decimal,
    new_quantity: int,
) -> TradeComputation:
    """手动编辑成本/数量 → adjustment 流水：只改成本/数量口径，不联动资金。

    invested_amount / available_funds 不由本函数改写（调用方保持原值或按
    PUT 入参显式覆盖），返回的对应字段仅作占位（等于旧投入口径、资金 0 含义为不动）。
    """
    return TradeComputation(
        direction="adjustment",
        new_cost_price=_d(new_cost_price),
        new_quantity=int(new_quantity),
        new_invested_amount=_base_invested(old_cost_price, old_quantity, None),
        new_available_funds=ZERO,  # 占位：adjustment 不联动资金，调用方不得采用
        realized_pnl=None,
    )


def build_adjustment_note(
    old_cost_price: int | float | str | Decimal,
    old_quantity: int,
    new_cost_price: int | float | str | Decimal,
    new_quantity: int,
) -> str:
    """生成 adjustment 流水备注（记录 old→new）。"""
    return (
        f"手动调整: 成本 {_d(old_cost_price)}→{_d(new_cost_price)}, "
        f"数量 {int(old_quantity)}→{int(new_quantity)}"
    )
