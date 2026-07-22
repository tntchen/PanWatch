"""持仓交易流水 API 测试（src/web/api/accounts.py 契约端点）。

覆盖（doc/14 §1 P1）：
- POST/GET /positions/{id}/trades 契约行为与字段；
- 卖出数量 > 持仓 → 400；
- 「流水插入 + 成本重算 + 资金联动」单事务回滚后三处都不变；
- PUT 成本/数量变更自动生成 adjustment 流水（不联动资金）；
- 有流水的持仓 DELETE → 400（message 含「存在交易流水」）；
- POST /positions 同股票 400 拒绝逻辑不变；
- portfolio/summary 新增 realized_pnl 字段。

测试模式：复用 test_portfolio_result_cache.py 的内存 sqlite + 直接调用端点函数。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M
from src.web.api import accounts as accounts_api
from src.web.database import Base


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _make_position(db, cost=10.0, qty=100, funds=10000.0, invested=1000.0):
    acc = M.Account(name="招商证券", available_funds=funds, enabled=True)
    db.add(acc)
    db.flush()
    st = M.Stock(symbol="688110", name="东芯股份", market="CN")
    db.add(st)
    db.flush()
    pos = M.Position(
        account_id=acc.id, stock_id=st.id,
        cost_price=cost, quantity=qty, invested_amount=invested,
    )
    db.add(pos)
    db.commit()
    return acc, st, pos


def _buy(price, qty, fee=0, traded_at=None, note=None):
    return accounts_api.TradeCreate(
        direction="buy", price=price, quantity=qty, fee=fee,
        traded_at=traded_at, note=note,
    )


def _sell(price, qty, fee=0, traded_at=None, note=None):
    return accounts_api.TradeCreate(
        direction="sell", price=price, quantity=qty, fee=fee,
        traded_at=traded_at, note=note,
    )


# ---------------------------------------------------------------- POST trades


def test_buy_trade_updates_cost_and_funds(db):
    """买入流水：移动加权平均重算成本、扣减可用资金、响应含 position+trade。"""
    acc, _, pos = _make_position(db, cost=10.0, qty=100, funds=5000.0, invested=1000.0)

    res = accounts_api.create_position_trade(
        pos.id, _buy(price=8, qty=100, fee=10, traded_at=datetime(2026, 7, 20, 10, 0)), db=db,
    )

    p, t = res["position"], res["trade"]
    assert p["quantity"] == 200
    assert abs(p["cost_price"] - 9.05) <= 0.001  # (1000+800+10)/200
    assert abs(p["invested_amount"] - 1810) <= 0.001
    assert p["trade_count"] == 1
    assert p["realized_pnl_total"] == 0

    db.refresh(acc)
    assert abs(acc.available_funds - (5000 - 810)) <= 0.001

    assert t["direction"] == "buy"
    assert t["realized_pnl"] is None
    assert t["traded_at"].startswith("2026-07-20T10:00:00")
    for field in ("id", "position_id", "direction", "price", "quantity", "fee",
                  "traded_at", "realized_pnl", "note", "created_at"):
        assert field in t


def test_sell_trade_realized_pnl_and_funds_flow_back(db):
    """卖出流水：成本不变、已实现盈亏正确、资金回流、realized_pnl_total 累加。"""
    acc, _, pos = _make_position(db, cost=9.0, qty=200, funds=4200.0, invested=1800.0)

    res = accounts_api.create_position_trade(
        pos.id, _sell(price=12, qty=50, fee=5), db=db,
    )

    p, t = res["position"], res["trade"]
    assert abs(p["cost_price"] - 9.0) <= 0.001  # 成本不变
    assert p["quantity"] == 150
    assert abs(p["invested_amount"] - 1350) <= 0.001  # 按 150/200 结转
    assert abs(p["realized_pnl_total"] - 145) <= 0.001  # (12-9)*50-5

    assert t["direction"] == "sell"
    assert abs(t["realized_pnl"] - 145) <= 0.001

    db.refresh(acc)
    assert abs(acc.available_funds - (4200 + 600 - 5)) <= 0.001


def test_sell_to_zero_keeps_row(db):
    """减至 0 = 关仓留行：持仓行仍在、数量为 0、成本字段保留。"""
    _, _, pos = _make_position(db, cost=9.0, qty=100, funds=0.0, invested=900.0)
    accounts_api.create_position_trade(pos.id, _sell(price=9, qty=100), db=db)

    db.refresh(pos)
    assert pos.quantity == 0
    assert abs(pos.cost_price - 9.0) <= 0.001
    assert db.query(M.Position).filter(M.Position.id == pos.id).count() == 1

    # 关仓后再买入走 trades 端点，成本重算正确
    res = accounts_api.create_position_trade(pos.id, _buy(price=8, qty=100), db=db)
    assert res["position"]["quantity"] == 100
    assert abs(res["position"]["cost_price"] - 8.0) <= 0.001


def test_sell_oversell_returns_400(db):
    """卖出数量 > 持仓 → 400，且持仓/资金不变。"""
    acc, _, pos = _make_position(db, cost=9.0, qty=200, funds=4200.0)
    with pytest.raises(HTTPException) as exc:
        accounts_api.create_position_trade(pos.id, _sell(price=12, qty=201), db=db)
    assert exc.value.status_code == 400

    db.refresh(pos)
    db.refresh(acc)
    assert pos.quantity == 200
    assert acc.available_funds == 4200.0
    assert db.query(M.PositionTrade).count() == 0


def test_trade_on_missing_position_404(db):
    """不存在的持仓 → 404。"""
    with pytest.raises(HTTPException) as exc:
        accounts_api.create_position_trade(999, _buy(price=8, qty=100), db=db)
    assert exc.value.status_code == 404


def test_single_transaction_rollback_on_commit_failure(db, monkeypatch):
    """单事务：提交失败时流水/成本/资金三处都回滚到原状。"""
    acc, _, pos = _make_position(db, cost=10.0, qty=100, funds=5000.0, invested=1000.0)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "commit", boom)
    with pytest.raises(HTTPException) as exc:
        accounts_api.create_position_trade(pos.id, _buy(price=8, qty=100), db=db)
    assert exc.value.status_code == 500
    monkeypatch.undo()

    # 三处状态都不变
    pos2 = db.query(M.Position).filter(M.Position.id == pos.id).one()
    acc2 = db.query(M.Account).filter(M.Account.id == acc.id).one()
    assert pos2.quantity == 100
    assert pos2.cost_price == 10.0
    assert pos2.invested_amount == 1000.0
    assert acc2.available_funds == 5000.0
    assert db.query(M.PositionTrade).count() == 0


# ---------------------------------------------------------------- GET trades


def test_list_trades_ascending_by_traded_at(db):
    """GET 流水按 traded_at 升序（与录入顺序无关）。"""
    _, _, pos = _make_position(db)
    accounts_api.create_position_trade(
        pos.id, _buy(8, 100, traded_at=datetime(2026, 7, 21, 10, 0)), db=db,
    )
    accounts_api.create_position_trade(
        pos.id, _buy(7, 100, traded_at=datetime(2026, 7, 20, 10, 0)), db=db,
    )
    accounts_api.create_position_trade(
        pos.id, _sell(9, 50, traded_at=datetime(2026, 7, 22, 10, 0)), db=db,
    )

    trades = accounts_api.list_position_trades(pos.id, db=db)
    assert [t["traded_at"][:10] for t in trades] == ["2026-07-20", "2026-07-21", "2026-07-22"]
    assert [t["direction"] for t in trades] == ["buy", "buy", "sell"]


def test_list_trades_missing_position_404(db):
    """GET 流水：持仓不存在 → 404。"""
    with pytest.raises(HTTPException) as exc:
        accounts_api.list_position_trades(999, db=db)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------- PUT adjustment


def test_put_cost_change_generates_adjustment_trade(db):
    """PUT 成本实际变化 → 自动生成 adjustment 流水（note 记录 old→new，不联动资金）。"""
    acc, _, pos = _make_position(db, cost=10.0, qty=100, funds=5000.0)

    res = accounts_api.update_position(
        pos.id, accounts_api.PositionUpdate(cost_price=9.5), db=db,
    )
    assert res["cost_price"] == 9.5
    assert res["trade_count"] == 1

    trades = accounts_api.list_position_trades(pos.id, db=db)
    assert len(trades) == 1
    t = trades[0]
    assert t["direction"] == "adjustment"
    assert t["realized_pnl"] is None
    assert "10" in t["note"] and "9.5" in t["note"] and "→" in t["note"]

    db.refresh(acc)
    assert acc.available_funds == 5000.0  # 不联动资金


def test_put_quantity_change_generates_adjustment_trade(db):
    """PUT 数量实际变化 → 同样生成 adjustment 流水。"""
    _, _, pos = _make_position(db, cost=10.0, qty=100)
    accounts_api.update_position(pos.id, accounts_api.PositionUpdate(quantity=150), db=db)
    trades = accounts_api.list_position_trades(pos.id, db=db)
    assert len(trades) == 1
    assert trades[0]["direction"] == "adjustment"
    assert "100" in trades[0]["note"] and "150" in trades[0]["note"]


def test_put_same_value_no_adjustment(db):
    """PUT 值未实际变化（含仅改 trading_style）→ 不生成 adjustment 流水。"""
    _, _, pos = _make_position(db, cost=10.0, qty=100)
    accounts_api.update_position(
        pos.id, accounts_api.PositionUpdate(cost_price=10.0, trading_style="long"), db=db,
    )
    assert db.query(M.PositionTrade).count() == 0


# ---------------------------------------------------------------- DELETE 禁删


def test_delete_position_with_trades_rejected(db):
    """有流水的持仓 DELETE → 400，message 含「存在交易流水」。"""
    _, _, pos = _make_position(db)
    accounts_api.create_position_trade(pos.id, _buy(8, 100), db=db)

    with pytest.raises(HTTPException) as exc:
        accounts_api.delete_position(pos.id, db=db)
    assert exc.value.status_code == 400
    assert "存在交易流水" in str(exc.value.detail)
    assert db.query(M.Position).filter(M.Position.id == pos.id).count() == 1


def test_delete_position_without_trades_ok(db):
    """无流水的持仓仍可正常删除。"""
    _, _, pos = _make_position(db)
    res = accounts_api.delete_position(pos.id, db=db)
    assert res["success"] is True
    assert db.query(M.Position).filter(M.Position.id == pos.id).count() == 0


def test_delete_closed_position_with_trades_still_rejected(db):
    """关仓留行（quantity=0）但有流水 → 仍禁删。"""
    _, _, pos = _make_position(db, cost=9.0, qty=100, invested=900.0)
    accounts_api.create_position_trade(pos.id, _sell(9, 100), db=db)
    with pytest.raises(HTTPException) as exc:
        accounts_api.delete_position(pos.id, db=db)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------- 既有行为回归


def test_create_position_duplicate_still_400(db):
    """POST /positions 同账户同股票 400 拒绝逻辑保持不变（再建仓走 trades 端点）。"""
    acc, st, _ = _make_position(db)
    with pytest.raises(HTTPException) as exc:
        accounts_api.create_position(
            accounts_api.PositionCreate(
                account_id=acc.id, stock_id=st.id, cost_price=8, quantity=100,
            ),
            db=db,
        )
    assert exc.value.status_code == 400


def test_list_positions_includes_trade_fields(db):
    """持仓列表端点同样带 realized_pnl_total / trade_count。"""
    _, _, pos = _make_position(db, cost=9.0, qty=200, invested=1800.0)
    accounts_api.create_position_trade(pos.id, _sell(12, 50, fee=5), db=db)

    rows = accounts_api.list_positions(db=db)
    assert len(rows) == 1
    assert rows[0]["trade_count"] == 1
    assert abs(rows[0]["realized_pnl_total"] - 145) <= 0.001


def test_portfolio_summary_has_realized_pnl(db, monkeypatch):
    """portfolio/summary：每账户与总计新增 realized_pnl。"""
    monkeypatch.setattr(accounts_api, "get_hkd_cny_rate", lambda: 0.92)
    monkeypatch.setattr(accounts_api, "get_usd_cny_rate", lambda: 7.25)

    _, _, pos = _make_position(db, cost=9.0, qty=200, funds=4200.0, invested=1800.0)
    accounts_api.create_position_trade(pos.id, _sell(12, 50, fee=5), db=db)

    res = accounts_api.get_portfolio_summary(include_quotes=False, db=db)
    assert abs(res["accounts"][0]["realized_pnl"] - 145) <= 0.001
    assert abs(res["total"]["realized_pnl"] - 145) <= 0.001
    # 持仓行也带流水聚合字段
    assert res["accounts"][0]["positions"][0]["trade_count"] == 1


def test_dongxin_anchor_via_api(db):
    """验收锚点走 API：录入东芯三笔历史流水后成本 = 112.572（容差 ≤0.001）。"""
    acc = M.Account(name="招商证券", available_funds=1000000.0, enabled=True)
    db.add(acc)
    db.flush()
    st = M.Stock(symbol="688110", name="东芯股份", market="CN")
    db.add(st)
    db.flush()
    pos = M.Position(account_id=acc.id, stock_id=st.id, cost_price=0.0, quantity=0)
    db.add(pos)
    db.commit()

    accounts_api.create_position_trade(
        pos.id, _buy(138.272, 1600, traded_at=datetime(2025, 4, 7, 9, 35)), db=db,
    )
    accounts_api.create_position_trade(
        pos.id, _buy(95.0, 1000, traded_at=datetime(2025, 6, 10, 10, 20)), db=db,
    )
    res = accounts_api.create_position_trade(
        pos.id, _buy(68.0, 550, fee=966.6, traded_at=datetime(2025, 7, 15, 14, 10)), db=db,
    )

    p = res["position"]
    assert p["quantity"] == 3150
    assert abs(p["cost_price"] - 112.572) <= 0.001
    assert p["trade_count"] == 3

    db.refresh(acc)
    assert abs(acc.available_funds - (1000000 - 354601.8)) <= 0.001
