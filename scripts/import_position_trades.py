#!/usr/bin/env python3
"""历史持仓交易流水幂等导入脚本（持仓交易化 Phase 1）。

判重键: position_id + direction + price + quantity + traded_at —— 已存在则跳过，
重复执行安全（幂等）。导入按 traded_at 升序逐笔应用成本引擎（移动加权平均，
Decimal 计算 float 落库），并联动 Account.available_funds。

用法:
    python scripts/import_position_trades.py --position-id 1 --file trades.json
    python scripts/import_position_trades.py --position-id 1 --dongxin-example

trades.json 格式（数组）:
    [{"direction": "buy", "price": 138.272, "quantity": 1600, "fee": 0,
      "traded_at": "2025-04-07T09:35:00", "note": "首批建仓"}, ...]

东芯三笔历史流水示例（1600 股 @138.272 → 3150 股 @112.572，含手续费口径）:
    1) buy 1600 @ 138.272  fee 0      2025-04-07  首批建仓
    2) buy 1000 @  95.000  fee 0      2025-06-10  第一次补仓
    3) buy  550 @  68.000  fee 966.6  2025-07-15  第二次补仓（含手续费）
    合计投入 = 138.272×1600 + 95×1000 + 68×550 + 966.6 = 354,601.8
    354,601.8 / 3150 股 = 112.572（移动加权平均，含手续费，与验收锚点一致）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# 允许以脚本方式直接运行（python scripts/import_position_trades.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.position_trading import (  # noqa: E402
    InsufficientPositionError,
    apply_buy,
    apply_sell,
)
from src.web.database import SessionLocal  # noqa: E402
from src.web.models import Account, Position, PositionTrade  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("import_position_trades")

# 东芯三笔历史流水（见模块 docstring 演算，成本锚点 3150 股 @112.572）
DONGXIN_EXAMPLE_TRADES: list[dict] = [
    {
        "direction": "buy",
        "price": 138.272,
        "quantity": 1600,
        "fee": 0,
        "traded_at": "2025-04-07T09:35:00",
        "note": "首批建仓",
    },
    {
        "direction": "buy",
        "price": 95.000,
        "quantity": 1000,
        "fee": 0,
        "traded_at": "2025-06-10T10:20:00",
        "note": "第一次补仓",
    },
    {
        "direction": "buy",
        "price": 68.000,
        "quantity": 550,
        "fee": 966.6,
        "traded_at": "2025-07-15T14:10:00",
        "note": "第二次补仓（含手续费）",
    },
]


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    return datetime.fromisoformat(value)


def _dedupe_key(position_id: int, trade: dict) -> tuple:
    return (
        position_id,
        trade["direction"],
        round(float(trade["price"]), 6),
        int(trade["quantity"]),
        _parse_dt(trade.get("traded_at")),
    )


def import_trades(position_id: int, trades: list[dict]) -> dict:
    """幂等导入流水。返回 {"imported": n, "skipped": n}。"""
    db = SessionLocal()
    imported = skipped = 0
    try:
        position = db.query(Position).filter(Position.id == position_id).first()
        if not position:
            raise SystemExit(f"持仓不存在: position_id={position_id}")
        account = db.query(Account).filter(Account.id == position.account_id).first()
        if not account:
            raise SystemExit(f"账户不存在: account_id={position.account_id}")

        # 已存在流水的判重键集合（float 价格按 6 位小数归一后与输入同口径比较）
        existing_keys = set()
        for t in db.query(PositionTrade).filter(
            PositionTrade.position_id == position_id
        ).all():
            existing_keys.add(
                (t.position_id, t.direction, round(float(t.price), 6), int(t.quantity), t.traded_at)
            )

        # 按 traded_at 升序应用，保证移动加权平均成本口径正确
        ordered = sorted(trades, key=lambda t: _parse_dt(t.get("traded_at")))

        for item in ordered:
            key = _dedupe_key(position_id, item)
            if key in existing_keys:
                skipped += 1
                logger.info(f"跳过（已存在）: {item}")
                continue

            direction = item["direction"]
            fee = float(item.get("fee", 0) or 0)
            if direction == "buy":
                comp = apply_buy(
                    position.cost_price, position.quantity, position.invested_amount,
                    account.available_funds, item["price"], int(item["quantity"]), fee,
                )
            elif direction == "sell":
                try:
                    comp = apply_sell(
                        position.cost_price, position.quantity, position.invested_amount,
                        account.available_funds, item["price"], int(item["quantity"]), fee,
                    )
                except InsufficientPositionError as e:
                    raise SystemExit(f"导入中止（超卖）: {e}; 流水: {item}")
            else:
                raise SystemExit(f"导入脚本仅支持 buy/sell 流水，收到: {direction}")

            trade = PositionTrade(
                position_id=position.id,
                direction=direction,
                price=float(item["price"]),
                quantity=int(item["quantity"]),
                fee=fee,
                traded_at=_parse_dt(item.get("traded_at")),
                realized_pnl=(
                    float(comp.realized_pnl) if comp.realized_pnl is not None else None
                ),
                note=item.get("note"),
            )
            position.cost_price = float(comp.new_cost_price)
            position.quantity = int(comp.new_quantity)
            position.invested_amount = float(comp.new_invested_amount)
            account.available_funds = float(comp.new_available_funds)
            db.add(trade)
            existing_keys.add(key)
            imported += 1

        # 单事务提交：全部流水 + 成本 + 资金一起落库，失败整体回滚
        db.commit()
        logger.info(
            f"导入完成: position={position_id} 新增 {imported} 条, 跳过 {skipped} 条; "
            f"当前持仓 {position.quantity} 股 @ {position.cost_price}"
        )
        return {"imported": imported, "skipped": skipped}
    except SystemExit:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("导入失败，已回滚")
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="历史持仓交易流水幂等导入")
    parser.add_argument("--position-id", type=int, required=True, help="目标持仓 ID")
    parser.add_argument("--file", type=str, help="流水 JSON 文件路径")
    parser.add_argument(
        "--dongxin-example",
        action="store_true",
        help="导入东芯三笔示例流水（1600@138.272 → 3150@112.572）",
    )
    args = parser.parse_args()

    if args.dongxin_example:
        trades = DONGXIN_EXAMPLE_TRADES
    elif args.file:
        trades = json.loads(Path(args.file).read_text(encoding="utf-8"))
    else:
        parser.error("必须指定 --file 或 --dongxin-example")

    result = import_trades(args.position_id, trades)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
