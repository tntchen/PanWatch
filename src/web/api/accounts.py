"""账户和持仓管理 API"""
import logging
import time
import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from datetime import datetime, timedelta, timezone
from typing import Literal

from src.web.database import get_db
from src.web.models import Account, PositionTrade, PriceAlertRule, Position, Stock
from src.core.marketdata_client import md_quote_rows
from src.core.position_trading import (
    InsufficientPositionError,
    apply_buy,
    apply_sell,
    build_adjustment_note,
)
from src.collectors.market_http import TTLCache
from src.models.market import MarketCode

logger = logging.getLogger(__name__)
router = APIRouter()

# 汇率缓存
_hkd_rate_cache: dict = {"rate": 0.92, "ts": 0}  # 港币默认汇率 0.92
_usd_rate_cache: dict = {"rate": 7.25, "ts": 0}  # 美元默认汇率 7.25
EXCHANGE_RATE_TTL = 3600  # 1 小时缓存


def get_hkd_cny_rate() -> float:
    """获取港币兑人民币汇率"""
    global _hkd_rate_cache

    # 检查缓存
    if time.time() - _hkd_rate_cache["ts"] < EXCHANGE_RATE_TTL:
        return _hkd_rate_cache["rate"]

    # 从新浪财经获取汇率
    try:
        resp = httpx.get(
            "https://hq.sinajs.cn/list=fx_shkdcny",
            timeout=5,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/"
            }
        )
        # 格式: var hq_str_fx_shkdcny="时间,汇率,..."
        text = resp.text
        if "=" in text and "," in text:
            data = text.split('"')[1]
            parts = data.split(",")
            if len(parts) > 1:
                rate = float(parts[1])
                _hkd_rate_cache = {"rate": rate, "ts": time.time()}
                logger.info(f"更新港币汇率: {rate}")
                return rate
    except Exception as e:
        logger.warning(f"获取港币汇率失败，使用缓存: {e}")

    return _hkd_rate_cache["rate"]


def get_usd_cny_rate() -> float:
    """获取美元兑人民币汇率"""
    global _usd_rate_cache

    # 检查缓存
    if time.time() - _usd_rate_cache["ts"] < EXCHANGE_RATE_TTL:
        return _usd_rate_cache["rate"]

    # 从新浪财经获取汇率
    try:
        resp = httpx.get(
            "https://hq.sinajs.cn/list=fx_susdcny",
            timeout=5,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/"
            }
        )
        # 格式: var hq_str_fx_susdcny="时间,汇率,..."
        text = resp.text
        if "=" in text and "," in text:
            data = text.split('"')[1]
            parts = data.split(",")
            if len(parts) > 1:
                rate = float(parts[1])
                _usd_rate_cache = {"rate": rate, "ts": time.time()}
                logger.info(f"更新美元汇率: {rate}")
                return rate
    except Exception as e:
        logger.warning(f"获取美元汇率失败，使用缓存: {e}")

    return _usd_rate_cache["rate"]


# ========== Pydantic Models ==========

class AccountCreate(BaseModel):
    name: str
    available_funds: float = 0


class AccountUpdate(BaseModel):
    name: str | None = None
    available_funds: float | None = None
    enabled: bool | None = None


class AccountResponse(BaseModel):
    id: int
    name: str
    available_funds: float
    enabled: bool

    class Config:
        from_attributes = True


class PositionCreate(BaseModel):
    account_id: int
    stock_id: int
    cost_price: float
    quantity: int
    invested_amount: float | None = None
    trading_style: str | None = None  # short: 短线, swing: 波段, long: 长线


class PositionUpdate(BaseModel):
    cost_price: float | None = None
    quantity: int | None = None
    invested_amount: float | None = None
    trading_style: str | None = None


class PositionResponse(BaseModel):
    id: int
    account_id: int
    stock_id: int
    cost_price: float
    quantity: int
    invested_amount: float | None
    sort_order: int
    trading_style: str | None
    # 持仓交易化 Phase 1：由 position_trades 流水聚合（新表 create_all，不加列免迁移）
    realized_pnl_total: float = 0
    trade_count: int = 0
    # 关联信息
    account_name: str | None = None
    stock_symbol: str | None = None
    stock_name: str | None = None

    class Config:
        from_attributes = True


class TradeCreate(BaseModel):
    direction: Literal["buy", "sell"]  # adjustment 由系统自动生成，不接受手工录入
    price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    fee: float = Field(default=0, ge=0)
    traded_at: datetime | None = None  # 默认当前时间
    note: str | None = None


class PositionReorderItem(BaseModel):
    id: int
    sort_order: int


class PositionReorderRequest(BaseModel):
    items: list[PositionReorderItem]


# ========== 持仓流水序列化辅助 ==========

def _trade_stats(db: Session, position_ids: list[int]) -> dict[int, tuple[int, float]]:
    """按持仓聚合流水统计：{position_id: (trade_count, realized_pnl_total)}。

    realized_pnl_total 用 SUM(realized_pnl) 从流水聚合（NULL 的 buy/adjustment 自动忽略），
    避免给 positions 表加列（新表 create_all 即可覆盖，老库免迁移）。
    """
    if not position_ids:
        return {}
    rows = (
        db.query(
            PositionTrade.position_id,
            func.count(PositionTrade.id),
            func.coalesce(func.sum(PositionTrade.realized_pnl), 0.0),
        )
        .filter(PositionTrade.position_id.in_(position_ids))
        .group_by(PositionTrade.position_id)
        .all()
    )
    return {pid: (int(cnt), float(total)) for pid, cnt, total in rows}


def _position_dict(pos: Position, stats: dict[int, tuple[int, float]] | None = None) -> dict:
    trade_count, realized_total = (stats or {}).get(pos.id, (0, 0.0))
    return {
        "id": pos.id,
        "account_id": pos.account_id,
        "stock_id": pos.stock_id,
        "cost_price": pos.cost_price,
        "quantity": pos.quantity,
        "invested_amount": pos.invested_amount,
        "sort_order": pos.sort_order or 0,
        "trading_style": pos.trading_style,
        "realized_pnl_total": realized_total,
        "trade_count": trade_count,
        "account_name": pos.account.name if pos.account else None,
        "stock_symbol": pos.stock.symbol if pos.stock else None,
        "stock_name": pos.stock.name if pos.stock else None,
    }


def _trade_dict(trade: PositionTrade) -> dict:
    return {
        "id": trade.id,
        "position_id": trade.position_id,
        "direction": trade.direction,
        "price": trade.price,
        "quantity": trade.quantity,
        "fee": trade.fee,
        "traded_at": trade.traded_at.isoformat() if trade.traded_at else None,
        "realized_pnl": trade.realized_pnl,
        "note": trade.note,
        "created_at": trade.created_at.isoformat() if trade.created_at else None,
    }


# ========== Account Endpoints ==========

@router.get("/accounts", response_model=list[AccountResponse])
def list_accounts(db: Session = Depends(get_db)):
    """获取所有账户"""
    return db.query(Account).order_by(Account.id).all()


@router.get("/accounts/{account_id}", response_model=AccountResponse)
def get_account(account_id: int, db: Session = Depends(get_db)):
    """获取单个账户"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(404, "账户不存在")
    return account


@router.post("/accounts", response_model=AccountResponse)
def create_account(data: AccountCreate, db: Session = Depends(get_db)):
    """创建账户"""
    account = Account(name=data.name, available_funds=data.available_funds)
    db.add(account)
    db.commit()
    db.refresh(account)
    logger.info(f"创建账户: {account.name}")
    return account


@router.put("/accounts/{account_id}", response_model=AccountResponse)
def update_account(account_id: int, data: AccountUpdate, db: Session = Depends(get_db)):
    """更新账户"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(404, "账户不存在")

    if data.name is not None:
        account.name = data.name
    if data.available_funds is not None:
        account.available_funds = data.available_funds
    if data.enabled is not None:
        account.enabled = data.enabled

    db.commit()
    db.refresh(account)
    logger.info(f"更新账户: {account.name}")
    return account


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    """删除账户（会同时删除该账户的所有持仓）"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(404, "账户不存在")

    db.delete(account)
    db.commit()
    logger.info(f"删除账户: {account.name}")
    return {"success": True}


# ========== Position Endpoints ==========

@router.get("/positions", response_model=list[PositionResponse])
def list_positions(
    account_id: int | None = None,
    stock_id: int | None = None,
    db: Session = Depends(get_db)
):
    """获取持仓列表，可按账户或股票筛选"""
    query = db.query(Position)
    if account_id:
        query = query.filter(Position.account_id == account_id)
    if stock_id:
        query = query.filter(Position.stock_id == stock_id)

    positions = query.order_by(Position.account_id.asc(), Position.sort_order.asc(), Position.id.asc()).all()
    stats = _trade_stats(db, [p.id for p in positions])
    return [_position_dict(pos, stats) for pos in positions]


@router.post("/positions", response_model=PositionResponse)
def create_position(data: PositionCreate, db: Session = Depends(get_db)):
    """创建持仓"""
    # 检查账户和股票是否存在
    account = db.query(Account).filter(Account.id == data.account_id).first()
    if not account:
        raise HTTPException(400, "账户不存在")

    stock = db.query(Stock).filter(Stock.id == data.stock_id).first()
    if not stock:
        raise HTTPException(400, "股票不存在")

    # 检查是否已存在该账户的该股票持仓
    existing = db.query(Position).filter(
        Position.account_id == data.account_id,
        Position.stock_id == data.stock_id,
    ).first()
    if existing:
        raise HTTPException(400, f"账户 {account.name} 已有 {stock.name} 的持仓，请编辑现有持仓")

    max_order = db.query(func.max(Position.sort_order)).filter(
        Position.account_id == data.account_id
    ).scalar() or 0

    position = Position(
        account_id=data.account_id,
        stock_id=data.stock_id,
        cost_price=data.cost_price,
        quantity=data.quantity,
        invested_amount=data.invested_amount,
        sort_order=int(max_order) + 1,
        trading_style=data.trading_style,
    )
    db.add(position)
    db.commit()
    db.refresh(position)

    logger.info(f"创建持仓: {account.name} - {stock.name}")
    return _position_dict(position)


@router.put("/positions/{position_id}", response_model=PositionResponse)
def update_position(position_id: int, data: PositionUpdate, db: Session = Depends(get_db)):
    """更新持仓

    契约（Phase 1）：cost_price 或 quantity 实际发生变化时，自动生成一条
    direction="adjustment" 流水（note 记录 old→new，realized_pnl=null），不联动资金。
    """
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise HTTPException(404, "持仓不存在")

    old_cost = position.cost_price
    old_qty = position.quantity

    if data.cost_price is not None:
        position.cost_price = data.cost_price
    if data.quantity is not None:
        position.quantity = data.quantity
    if data.invested_amount is not None:
        position.invested_amount = data.invested_amount
    if data.trading_style is not None:
        # 空字符串表示清空，设为 None
        position.trading_style = data.trading_style if data.trading_style else None

    cost_changed = data.cost_price is not None and data.cost_price != old_cost
    qty_changed = data.quantity is not None and data.quantity != old_qty
    if cost_changed or qty_changed:
        db.add(PositionTrade(
            position_id=position.id,
            direction="adjustment",
            price=position.cost_price,
            quantity=position.quantity,
            fee=0,
            traded_at=datetime.now(),
            realized_pnl=None,
            note=build_adjustment_note(old_cost, old_qty, position.cost_price, position.quantity),
        ))
        logger.info(
            f"持仓手动调整生成 adjustment 流水: position={position.id} "
            f"成本 {old_cost}→{position.cost_price} 数量 {old_qty}→{position.quantity}"
        )

    db.commit()
    db.refresh(position)

    logger.info(f"更新持仓: {position.account.name} - {position.stock.name}")
    stats = _trade_stats(db, [position.id])
    return _position_dict(position, stats)


@router.delete("/positions/{position_id}")
def delete_position(position_id: int, db: Session = Depends(get_db)):
    """删除持仓（存在交易流水时禁止删除，返回 400）"""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise HTTPException(404, "持仓不存在")

    trade_count = (
        db.query(func.count(PositionTrade.id))
        .filter(PositionTrade.position_id == position.id)
        .scalar()
    ) or 0
    if trade_count > 0:
        raise HTTPException(
            400,
            f"持仓存在交易流水（{trade_count} 条），禁止删除；"
            f"如需清仓请通过减仓流水将数量归零（关仓保留行）",
        )

    # 删除提交后实例 detached，先取名称再删，避免日志触发 DetachedInstanceError
    acc_name = position.account.name if position.account else ""
    stock_name = position.stock.name if position.stock else ""
    db.delete(position)
    db.commit()
    logger.info(f"删除持仓: {acc_name} - {stock_name}")
    return {"success": True}


# ========== Position Trade Endpoints（持仓交易化 Phase 1） ==========

@router.post("/positions/{position_id}/trades")
def create_position_trade(position_id: int, data: TradeCreate, db: Session = Depends(get_db)):
    """录入一笔买入/卖出流水。

    单事务提交：流水插入 + 成本重算 + Account 资金联动 同事务；
    任一步失败整体回滚，三处状态保持不变。
    """
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise HTTPException(404, "持仓不存在")
    account = db.query(Account).filter(Account.id == position.account_id).first()
    if not account:
        raise HTTPException(404, "账户不存在")

    try:
        if data.direction == "buy":
            comp = apply_buy(
                position.cost_price, position.quantity, position.invested_amount,
                account.available_funds, data.price, data.quantity, data.fee,
            )
        else:  # sell
            comp = apply_sell(
                position.cost_price, position.quantity, position.invested_amount,
                account.available_funds, data.price, data.quantity, data.fee,
            )
    except InsufficientPositionError as e:
        raise HTTPException(400, str(e))

    trade = PositionTrade(
        position_id=position.id,
        direction=data.direction,
        price=float(data.price),
        quantity=int(data.quantity),
        fee=float(data.fee),
        traded_at=data.traded_at or datetime.now(),
        realized_pnl=float(comp.realized_pnl) if comp.realized_pnl is not None else None,
        note=data.note,
    )
    position.cost_price = float(comp.new_cost_price)
    position.quantity = int(comp.new_quantity)
    position.invested_amount = float(comp.new_invested_amount)
    account.available_funds = float(comp.new_available_funds)

    db.add(trade)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(f"交易流水提交失败已回滚: position={position_id}")
        raise HTTPException(500, "交易流水提交失败，已回滚，持仓与资金未变更")

    db.refresh(trade)
    db.refresh(position)
    logger.info(
        f"录入流水: position={position.id} {data.direction} "
        f"{data.quantity}@{data.price} fee={data.fee}"
    )
    stats = _trade_stats(db, [position.id])
    return {"position": _position_dict(position, stats), "trade": _trade_dict(trade)}


@router.get("/positions/{position_id}/trades")
def list_position_trades(position_id: int, db: Session = Depends(get_db)):
    """获取持仓交易流水，按 traded_at 升序。"""
    position = db.query(Position).filter(Position.id == position_id).first()
    if not position:
        raise HTTPException(404, "持仓不存在")

    trades = (
        db.query(PositionTrade)
        .filter(PositionTrade.position_id == position.id)
        .order_by(PositionTrade.traded_at.asc(), PositionTrade.id.asc())
        .all()
    )
    return [_trade_dict(t) for t in trades]


@router.put("/positions/reorder/batch")
def reorder_positions(data: PositionReorderRequest, db: Session = Depends(get_db)):
    """批量更新持仓排序"""
    if not data.items:
        return {"updated": 0}
    ids = [int(x.id) for x in data.items]
    rows = db.query(Position).filter(Position.id.in_(ids)).all()
    row_map = {r.id: r for r in rows}
    updated = 0
    for item in data.items:
        row = row_map.get(int(item.id))
        if not row:
            continue
        row.sort_order = int(item.sort_order)
        updated += 1
    db.commit()
    return {"updated": updated}


# ========== Portfolio Summary ==========

@router.get("/portfolio/summary")
def get_portfolio_summary(
    account_id: int | None = None,
    include_quotes: bool = True,
    db: Session = Depends(get_db),
):
    """
    获取持仓汇总信息

    Args:
        account_id: 可选，指定账户ID。不指定则汇总所有账户

    Returns:
        accounts: 账户列表及各账户持仓明细
        total: 所有账户汇总
    """
    # 获取账户
    if account_id:
        accounts = db.query(Account).filter(Account.id == account_id, Account.enabled == True).all()
    else:
        accounts = db.query(Account).filter(Account.enabled == True).all()

    if not accounts:
        return {
            "accounts": [],
            "total": {
                "total_market_value": 0,
                "total_cost": 0,
                "total_pnl": 0,
                "total_pnl_pct": 0,
                "available_funds": 0,
                "total_assets": 0,
                "realized_pnl": 0,
            }
        }

    # 获取所有相关股票
    all_stock_ids = set()
    for acc in accounts:
        for pos in acc.positions:
            all_stock_ids.add(pos.stock_id)

    stocks = db.query(Stock).filter(Stock.id.in_(all_stock_ids)).all() if all_stock_ids else []
    stock_map = {s.id: s for s in stocks}

    # 获取实时行情（可选）
    quotes = _fetch_quotes_for_stocks(stocks) if include_quotes else {}

    # 获取汇率
    hkd_rate = get_hkd_cny_rate()
    usd_rate = get_usd_cny_rate()

    # 计算各账户持仓
    account_summaries = []
    grand_total_market_value = 0
    grand_total_cost = 0
    grand_available_funds = 0
    grand_daily_pnl = 0
    grand_realized_pnl = 0.0

    # 持仓流水聚合（realized_pnl_total / trade_count），一次查询避免 N+1
    all_position_ids = [pos.id for acc in accounts for pos in (acc.positions or [])]
    trade_stats = _trade_stats(db, all_position_ids)

    for acc in accounts:
        positions_data = []
        acc_market_value = 0
        acc_cost = 0
        acc_daily_pnl = 0
        acc_realized_pnl = 0.0

        positions_sorted = sorted(
            list(acc.positions or []),
            key=lambda p: (int(getattr(p, "sort_order", 0) or 0), int(p.id)),
        )
        for pos in positions_sorted:
            stock = stock_map.get(pos.stock_id)
            if not stock:
                continue

            quote = quotes.get(stock.symbol)
            current_price = quote["current_price"] if quote else None
            change_pct = quote["change_pct"] if quote else None
            prev_close = quote.get("prev_close") if quote else None

            # 根据市场确定汇率
            is_foreign = stock.market in ("HK", "US")
            if stock.market == "HK":
                rate = hkd_rate
            elif stock.market == "US":
                rate = usd_rate
            else:
                rate = 1.0

            market_value = None
            market_value_cny = None
            pnl = None
            pnl_pct = None
            daily_pnl = None
            daily_pnl_pct = None

            if current_price is not None and prev_close and prev_close > 0:
                daily_pnl = (current_price - prev_close) * pos.quantity * rate
                daily_pnl_pct = (current_price - prev_close) / prev_close * 100
                acc_daily_pnl += daily_pnl

            cost = pos.cost_price * pos.quantity
            cost_cny = cost * rate  # 假设成本价也是原币种
            acc_cost += cost_cny

            if current_price is not None:
                market_value = current_price * pos.quantity  # 原币种市值
                market_value_cny = market_value * rate  # 人民币市值
                pnl = market_value_cny - cost_cny
                pnl_pct = (pnl / cost_cny * 100) if cost_cny > 0 else 0

                acc_market_value += market_value_cny

            pos_trade_count, pos_realized_total = trade_stats.get(pos.id, (0, 0.0))
            acc_realized_pnl += pos_realized_total

            positions_data.append({
                "id": pos.id,
                "stock_id": pos.stock_id,
                "symbol": stock.symbol,
                "name": stock.name,
                "market": stock.market,
                "cost_price": pos.cost_price,
                "quantity": pos.quantity,
                "invested_amount": pos.invested_amount,
                "sort_order": pos.sort_order or 0,
                "trading_style": pos.trading_style,
                "realized_pnl_total": round(pos_realized_total, 2),
                "trade_count": pos_trade_count,
                "current_price": current_price,
                "current_price_cny": round(current_price * rate, 2) if current_price else None,
                "change_pct": change_pct,
                "market_value": round(market_value, 2) if market_value else None,
                "market_value_cny": round(market_value_cny, 2) if market_value_cny else None,
                "pnl": round(pnl, 2) if pnl else None,
                "pnl_pct": round(pnl_pct, 2) if pnl_pct else None,
                "daily_pnl": round(daily_pnl, 2) if daily_pnl is not None else None,
                "daily_pnl_pct": round(daily_pnl_pct, 2) if daily_pnl_pct is not None else None,
                "exchange_rate": rate if is_foreign else None,
            })

        if include_quotes:
            acc_pnl = acc_market_value - acc_cost
            acc_pnl_pct = (acc_pnl / acc_cost * 100) if acc_cost > 0 else 0
            acc_total_assets = acc_market_value + acc.available_funds
        else:
            acc_pnl = 0
            acc_pnl_pct = 0
            acc_total_assets = acc.available_funds

        account_summaries.append({
            "id": acc.id,
            "name": acc.name,
            "available_funds": acc.available_funds,
            "total_market_value": round(acc_market_value, 2),
            "total_cost": round(acc_cost, 2),
            "total_pnl": round(acc_pnl, 2),
            "total_pnl_pct": round(acc_pnl_pct, 2),
            "total_daily_pnl": round(acc_daily_pnl, 2),
            "total_assets": round(acc_total_assets, 2),
            "realized_pnl": round(acc_realized_pnl, 2),
            "positions": positions_data,
        })

        grand_total_market_value += acc_market_value
        grand_total_cost += acc_cost
        grand_available_funds += acc.available_funds
        grand_daily_pnl += acc_daily_pnl
        grand_realized_pnl += acc_realized_pnl

    if include_quotes:
        grand_pnl = grand_total_market_value - grand_total_cost
        grand_pnl_pct = (grand_pnl / grand_total_cost * 100) if grand_total_cost > 0 else 0
        grand_total_assets = grand_total_market_value + grand_available_funds
    else:
        grand_pnl = 0
        grand_pnl_pct = 0
        grand_total_assets = grand_available_funds

    # 构建 quotes 字典（用于前端股票列表显示）
    quotes_dict = {}
    if include_quotes:
        for symbol, quote in quotes.items():
            quotes_dict[symbol] = {
                "current_price": quote.get("current_price"),
                "change_pct": quote.get("change_pct"),
            }

    return {
        "accounts": account_summaries,
        "total": {
            "total_market_value": round(grand_total_market_value, 2),
            "total_cost": round(grand_total_cost, 2),
            "total_pnl": round(grand_pnl, 2),
            "total_pnl_pct": round(grand_pnl_pct, 2),
            "total_daily_pnl": round(grand_daily_pnl, 2),
            "available_funds": round(grand_available_funds, 2),
            "total_assets": round(grand_total_assets, 2),
            "realized_pnl": round(grand_realized_pnl, 2),
        },
        "exchange_rates": {
            "HKD_CNY": hkd_rate,
            "USD_CNY": usd_rate,
        },
        "quotes": quotes_dict,  # 可选：返回行情数据
    }


def _fetch_quotes_for_stocks(stocks: list[Stock]) -> dict:
    """获取股票列表的实时行情"""
    if not stocks:
        return {}

    # 按市场分组
    market_stocks: dict[str, list[Stock]] = {}
    for s in stocks:
        market_stocks.setdefault(s.market, []).append(s)

    quotes = {}
    for market, stock_list in market_stocks.items():
        try:
            market_code = MarketCode(market)
        except ValueError:
            continue

        symbols = [s.symbol for s in stock_list]
        try:
            items = md_quote_rows(symbols, market_code.value)
            for item in items:
                quotes[item["symbol"]] = item
        except Exception as e:
            logger.error(f"获取 {market} 行情失败: {e}")

    return quotes


# 组合基准/归因结果缓存:重建全持仓 NAV 很贵(逐只拉 K 线),按持仓指纹缓存结果。
# 持仓变动即失效(指纹变);失败/空结果不缓存,避免把瞬时故障冻住 10 分钟。
_PORTFOLIO_RESULT_CACHE = TTLCache(default_ttl_sec=600.0)

# ── 缓存 key 租户化（MT-P2，docs/22 §2.4 / docs/26-J11）──────────────────
try:  # 防御：tenant_context 不可用时退化为全局缓存（等价单租户）
    from src.web.tenant_context import current_tenant as _current_tenant
except Exception:  # pragma: no cover - 防御性兜底
    _current_tenant = None  # type: ignore[assignment]


def _tenant_cache_prefix() -> str:
    """缓存 key 租户前缀：有 ctx 用其 tenant_id，无 ctx（裸脚本/公开路由）兜底 0。

    单租户直通模式（PANWATCH_SINGLE_TENANT=1）下所有 key 同前缀，行为不变。
    """
    if _current_tenant is None:
        return "0"
    try:
        ctx = _current_tenant()
    except Exception:  # pragma: no cover - 防御性兜底
        return "0"
    return str(ctx.tenant_id) if ctx is not None else "0"


def _holdings_signature(db: Session) -> str:
    """启用账户持仓的稳定指纹(stock_id + 合并后数量);仅查 DB,不拉行情/K 线。"""
    rows = (
        db.query(Position.stock_id, Position.quantity)
        .join(Account, Account.id == Position.account_id)
        .filter(Account.enabled == True)  # noqa: E712
        .all()
    )
    agg: dict[int, float] = {}
    for sid, qty in rows:
        agg[sid] = agg.get(sid, 0.0) + (qty or 0)
    return ";".join(f"{sid}:{agg[sid]:g}" for sid in sorted(agg))


def _gather_holdings(db: Session) -> list[dict]:
    """汇总所有启用账户的真实持仓为统一列表(CNY 市值/浮盈 + fx),多账户同股合并。"""
    accounts = db.query(Account).filter(Account.enabled == True).all()  # noqa: E712
    stock_ids = {p.stock_id for acc in accounts for p in acc.positions}
    stocks = db.query(Stock).filter(Stock.id.in_(stock_ids)).all() if stock_ids else []
    stock_map = {s.id: s for s in stocks}
    quotes = _fetch_quotes_for_stocks(stocks) if stocks else {}
    hkd, usd = get_hkd_cny_rate(), get_usd_cny_rate()

    out: list[dict] = []
    seen: dict[tuple[str, str], dict] = {}
    for acc in accounts:
        for pos in acc.positions:
            stock = stock_map.get(pos.stock_id)
            if not stock:
                continue
            rate = hkd if stock.market == "HK" else usd if stock.market == "US" else 1.0
            quote = quotes.get(stock.symbol)
            price = quote.get("current_price") if quote else None
            cost_cny = pos.cost_price * pos.quantity * rate
            mv_cny = (price * pos.quantity * rate) if price else cost_cny
            pnl_cny = (mv_cny - cost_cny) if price else 0.0
            key = (stock.market, stock.symbol)
            if key in seen:  # 多账户同一标的合并
                h = seen[key]
                h["quantity"] += pos.quantity
                h["market_value"] += mv_cny
                h["unrealized_pnl"] += pnl_cny
            else:
                h = {
                    "symbol": stock.symbol,
                    "market": stock.market,
                    "name": stock.name,
                    "quantity": pos.quantity,
                    "fx": rate,
                    "market_value": mv_cny,
                    "unrealized_pnl": pnl_cny,
                    "strategy_code": pos.trading_style or "",
                }
                seen[key] = h
                out.append(h)
    return out


@router.get("/portfolio/diagnostics")
def portfolio_diagnostics(db: Session = Depends(get_db)):
    """真实持仓组合诊断:集中度(HHI)/最大单仓/市场分布/风险提示(只读)。"""
    from src.core.portfolio_diagnostics import diagnose_positions

    return diagnose_positions(_gather_holdings(db))


@router.get("/portfolio/benchmark")
def portfolio_benchmark(
    days: int = 60, benchmark: str = "000300", db: Session = Depends(get_db)
):
    """真实持仓组合 vs 基准:超额收益/信息比率/相对回撤 + 归一化净值曲线。"""
    from src.core.portfolio_benchmark import (
        DEFAULT_BENCHMARK,
        build_portfolio_benchmark,
    )

    days = max(20, min(int(days), 250))
    bcode = benchmark or DEFAULT_BENCHMARK
    sig = _holdings_signature(db)
    if not sig:
        return {"empty": True, "reason": "no_holdings"}
    ckey = f"{_tenant_cache_prefix()}:bench:{days}:{bcode}:{sig}"
    cached = _PORTFOLIO_RESULT_CACHE.get(ckey)
    if cached is not None:
        return cached

    holdings = _gather_holdings(db)
    if not holdings:
        return {"empty": True, "reason": "no_holdings"}
    res = build_portfolio_benchmark(holdings, days=days, benchmark_code=bcode)
    if not res:
        # 失败/数据不足不缓存,下轮可重试(由 K 线负缓存兜住打爆)
        return {"empty": True, "reason": "insufficient_data"}
    _PORTFOLIO_RESULT_CACHE.set(ckey, res)
    return res


@router.get("/portfolio/todos")
def portfolio_todos(db: Session = Depends(get_db)):
    """首页空态待办:持仓但未设提醒 / 提醒即将到期(可行动,盘后也不空)。"""
    todos: list[dict] = []
    accounts = db.query(Account).filter(Account.enabled == True).all()  # noqa: E712
    held_ids = {p.stock_id for acc in accounts for p in acc.positions}
    if held_ids:
        ruled = {
            r.stock_id
            for r in db.query(PriceAlertRule)
            .filter(PriceAlertRule.enabled == True, PriceAlertRule.stock_id.in_(held_ids))  # noqa: E712
            .all()
        }
        for sid in held_ids - ruled:
            stock = db.query(Stock).filter(Stock.id == sid).first()
            if stock:
                todos.append(
                    {
                        "type": "no_alert",
                        "symbol": stock.symbol,
                        "market": stock.market,
                        "message": f"{stock.name} 持仓中,未设价格提醒",
                    }
                )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    soon = now + timedelta(days=3)
    expiring = (
        db.query(PriceAlertRule)
        .filter(
            PriceAlertRule.enabled == True,  # noqa: E712
            PriceAlertRule.expire_at.isnot(None),
            PriceAlertRule.expire_at >= now,
            PriceAlertRule.expire_at <= soon,
        )
        .all()
    )
    for r in expiring:
        stock = db.query(Stock).filter(Stock.id == r.stock_id).first()
        todos.append(
            {
                "type": "alert_expiring",
                "symbol": stock.symbol if stock else "",
                "market": stock.market if stock else "CN",
                "message": f"{(r.name or '提醒')} 即将到期",
            }
        )

    return {"todos": todos[:10], "count": len(todos)}


@router.get("/portfolio/attribution")
def portfolio_attribution(days: int = 60, benchmark: str = "000300", db: Session = Depends(get_db)):
    """近 days 日各持仓对组合收益的贡献(谁拖累/贡献),降序。"""
    from src.core.portfolio_benchmark import DEFAULT_BENCHMARK, build_attribution

    days = max(20, min(int(days), 250))
    bcode = benchmark or DEFAULT_BENCHMARK
    sig = _holdings_signature(db)
    if not sig:
        return {"items": []}
    ckey = f"{_tenant_cache_prefix()}:attr:{days}:{bcode}:{sig}"
    cached = _PORTFOLIO_RESULT_CACHE.get(ckey)
    if cached is not None:
        return cached

    holdings = _gather_holdings(db)
    if not holdings:
        return {"items": []}
    items = build_attribution(holdings, days=days, benchmark_code=bcode)
    result = {"items": items}
    if items:  # 空结果不缓存,下轮可重试
        _PORTFOLIO_RESULT_CACHE.set(ckey, result)
    return result


@router.post("/portfolio/ai-review")
async def portfolio_ai_review(model_id: int | None = None, db: Session = Depends(get_db)):
    """组合 AI 体检:诊断+基准+归因 → 叙述结论 + 调仓建议(只读,不下单)。"""
    from src.core.portfolio_benchmark import build_attribution, build_portfolio_benchmark
    from src.core.portfolio_diagnostics import diagnose_positions
    from src.web.api.chat import _get_ai_client

    holdings = _gather_holdings(db)
    if not holdings:
        return {"empty": True, "reason": "no_holdings"}

    diag = diagnose_positions(holdings)
    bench = build_portfolio_benchmark(holdings, days=60) or {}
    attr = build_attribution(holdings, days=60)
    top = attr[:3]
    worst = list(reversed(attr[-3:])) if len(attr) > 3 else []

    lines = [
        f"持仓 {diag['position_count']} 只,总市值 {diag['total_market_value']:.0f},浮盈 {diag['total_unrealized_pnl']:.0f}",
        f"集中度 HHI {diag['hhi']},最大单仓 {diag['max_weight'] * 100:.0f}%",
    ]
    if bench.get("excess_return") is not None:
        lines.append(
            f"近60日 vs {bench.get('benchmark_label', '基准')}:超额 {bench['excess_return']}%"
            f"(组合 {bench.get('portfolio_return')}% / 基准 {bench.get('benchmark_return')}%),"
            f"相对回撤 {bench.get('relative_drawdown')}%"
        )
    if diag.get("by_market"):
        lines.append("市场分布:" + ", ".join(f"{k} {v:.0f}" for k, v in diag["by_market"].items()))
    if diag.get("alerts"):
        lines.append("风险提示:" + "; ".join(diag["alerts"]))
    if top:
        lines.append("贡献最大:" + ", ".join(f"{r['name']}({r['contribution_pct']:+.2f}%)" for r in top))
    if worst:
        lines.append("拖累最大:" + ", ".join(f"{r['name']}({r['contribution_pct']:+.2f}%)" for r in worst))

    system_prompt = (
        "你是稳健的组合顾问。基于给定的组合诊断/基准对比/个股归因,给一段简短体检 + 可执行调仓建议,"
        "只读分析、不下单、不承诺收益。严格格式:\n体检: 一句话总评\n建议:\n- (2~3 条具体可执行)\n风险: 一句话最大风险"
    )
    user_content = "组合概况:\n" + "\n".join(lines)
    try:
        content = await _get_ai_client(db, model_id).chat(system_prompt, user_content, temperature=0.3)
    except Exception as e:
        raise HTTPException(502, f"AI 体检失败: {e}")

    return {"content": content, "top": top, "worst": worst, "diagnostics": diag, "benchmark": bench}
