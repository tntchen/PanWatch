"""MT-P5-A 泄漏套件：持仓与自选核心 API 跨租户越权断言（docs/14 双租户对照模式）。

覆盖 router（docs/22 §2.1/§2.4/§2.14/§2.17/§2.24）：
- src/web/api/stocks.py        自选股 CRUD / agents 绑定 / reorder
- src/web/api/accounts.py      账户 / 持仓 / 流水 / portfolio 汇总
- src/web/api/suggestions.py   建议池查询 / 清理（core.suggestion_pool 自建 SessionLocal）
- src/web/api/playbooks.py     方案档案（含 bulk update 机制点验收）
- src/web/api/recommendations.py 入场候选 / 策略信号（哨兵表 tenant_id IN (ctx, 0)）

每端点三类断言：
  ① 租户 A 登录只见 A 的数据（list/detail 不含 B 的行，断言打到 symbol/股数/方案标题等字段）；
  ② A 用 B 的资源 id 做 GET/PUT/DELETE → 404（不泄露存在性，且 B 的行不受影响）；
  ③ A 创建的资源 tenant_id=A 且 B 不可见。

⚠️ 真缺陷上报（本文件 CREATE_ATTRIBUTION_XFAIL 标记的用例）：
  多租户模式下 web 创建端点（create_stock / create_account / create_position /
  create_position_trade / update_position 的 adjustment 流水 / create_playbook /
  PUT stocks/{id}/agents 的 StockAgent 行 /
  save_entry_candidate_feedback）均未做 INSERT 侧 tenant 归属——模型只有
  server_default="1"，新行一律落租户 1。后果双重：创建者（非默认租户）自己看不到
  刚创建的资源；默认租户（管理员）可见他租户用户创建的数据（跨租户泄漏）。
  docs/25 §4.1 设计的写入守卫 scoped_insert 未实现（src/web/tenant_context.py 仅有
  do_orm_execute 读侧过滤，无 before_flush/insert 钩子）。已按铁律上报，未改产品代码；
  修复后这些用例应转 XPASS 并移除标记。

设计意图不断言隔离：agent_configs / data_sources（实例级）、strategy_factor_snapshots /
strategy_catalog 等市场级共享表（docs/20 M2、docs/26-J1/J2）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M
import src.web.tenant_context as tc
from src.web.api import accounts as accounts_api
from src.web.api import auth, stocks, accounts, suggestions, playbooks, recommendations
from src.web.api.auth import get_current_user
from src.web.database import Base, get_db
from src.web.tenant_context import TenantContextMiddleware

import src.core.suggestion_pool as suggestion_pool
import src.core.entry_candidates as entry_candidates
import src.core.strategy_engine as strategy_engine
import src.core.strategy_catalog as strategy_catalog

SECRET = "mt-p5-leak-holdings-secret-0123456789"
OLD_PWD_AT = datetime(2020, 1, 1, 0, 0, 0)
SNAP = "2025-01-10"

# 真缺陷（见模块 docstring）：INSERT 侧无 tenant 归属，新行落 server_default=1。
CREATE_ATTRIBUTION_XFAIL = pytest.mark.xfail(
    reason=(
        "MT-P5-A 真缺陷：web 创建端点未做 INSERT tenant 归属，新行 tenant_id 落 "
        "server_default=1（创建者不可见 + 默认租户可见 = 跨租户泄漏），已上报；"
        "修复后转 XPASS 并移除此标记"
    ),
    strict=False,
)


@pytest.fixture
def mt_env(monkeypatch):
    """内存库 + do_orm_execute 事件 + 多租户模式 + 五 router 测试 app。

    suggestions/recommendations 的 handler 不走 get_db 依赖，而是经 core 模块自建
    SessionLocal()——必须把各 core 模块的 SessionLocal 符号替换为测试 Session 工厂，
    否则查询打到真实库（docs/22 §1 点名的机制约束在测试侧的同构处理）。
    """
    monkeypatch.setattr(auth, "_jwt_secret", SECRET)
    monkeypatch.setattr(auth, "ENV_AUTH_USERNAME", None)
    monkeypatch.setattr(auth, "ENV_AUTH_PASSWORD", None)
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")  # 多租户模式

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    event.listen(Session, "do_orm_execute", tc.apply_tenant_filter)
    old_cache = dict(tc._tenant_column_cache)
    tc.refresh_tenant_column_cache(engine)

    # core 模块 SessionLocal 替身（suggestions/recommendations 链路）
    for mod in (
        suggestion_pool,
        entry_candidates,
        strategy_engine,
        strategy_catalog,
        recommendations,
    ):
        monkeypatch.setattr(mod, "SessionLocal", Session)

    # 汇率接口防出网（portfolio/summary 无条件调用）
    monkeypatch.setattr(accounts_api, "get_hkd_cny_rate", lambda: 0.92)
    monkeypatch.setattr(accounts_api, "get_usd_cny_rate", lambda: 7.25)

    protected = [Depends(get_current_user)]
    app = FastAPI(redirect_slashes=False)
    app.add_middleware(TenantContextMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(
        playbooks.router, prefix="/api", dependencies=protected
    )  # 先于 stocks：/api/stocks/{id}/playbook(s) 不被 /api/stocks 抢占
    app.include_router(stocks.router, prefix="/api/stocks", dependencies=protected)
    app.include_router(accounts.router, prefix="/api", dependencies=protected)
    app.include_router(
        suggestions.router, prefix="/api/suggestions", dependencies=protected
    )
    app.include_router(
        recommendations.router, prefix="/api/recommendations", dependencies=protected
    )

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    yield TestClient(app), Session

    tc._tenant_column_cache.clear()
    tc._tenant_column_cache.update(old_cache)


# ---------------------------------------------------------------------------
# 夹具辅助：用户/登录/种子数据（复制自 test_mt_p4_credentials_api.py 模式）
# ---------------------------------------------------------------------------


def _mkuser(Session, username: str, *, tenant_id: int, role: str = "user") -> None:
    s = Session()
    if s.get(M.Tenant, tenant_id) is None:
        s.add(
            M.Tenant(id=tenant_id, name=f"租户{tenant_id}", is_default=(tenant_id == 1))
        )
    s.add(
        M.User(
            tenant_id=tenant_id,
            username=username,
            password_hash=auth.hash_password("secret123"),
            role=role,
            is_active=True,
            pwd_changed_at=OLD_PWD_AT,
        )
    )
    s.commit()
    s.close()


def _mk_two_tenants(Session) -> None:
    """租户 1=管理员 admin（B），租户 2=普通用户 u2（A）。"""
    _mkuser(Session, "admin", tenant_id=1, role="admin")
    _mkuser(Session, "u2", tenant_id=2, role="user")


def _hdr(client: TestClient, username: str) -> dict:
    r = client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed_stocks(Session) -> tuple[int, int]:
    """A(t2)=000001 平安银行；B(t1)=600519 贵州茅台。返回 (stock_a, stock_b)。"""
    s = Session()
    a = M.Stock(tenant_id=2, symbol="000001", name="平安银行", market="CN", sort_order=1)
    b = M.Stock(tenant_id=1, symbol="600519", name="贵州茅台", market="CN", sort_order=1)
    s.add_all([a, b])
    s.commit()
    ids = (a.id, b.id)
    s.close()
    return ids


def _seed_accounts(Session) -> tuple[int, int]:
    """A(t2)=A证券账户；B(t1)=B机密账户。返回 (acc_a, acc_b)。"""
    s = Session()
    a = M.Account(tenant_id=2, name="A证券账户", available_funds=5000.0)
    b = M.Account(tenant_id=1, name="B机密账户", available_funds=99999.0)
    s.add_all([a, b])
    s.commit()
    ids = (a.id, b.id)
    s.close()
    return ids


def _seed_positions(
    Session, acc_a: int, acc_b: int, stock_a: int, stock_b: int
) -> tuple[int, int]:
    """A 持 000001 共 700 股；B 持 600519 共 100 股。返回 (pos_a, pos_b)。"""
    s = Session()
    a = M.Position(
        tenant_id=2, account_id=acc_a, stock_id=stock_a,
        cost_price=10.0, quantity=700, sort_order=1,
    )
    b = M.Position(
        tenant_id=1, account_id=acc_b, stock_id=stock_b,
        cost_price=1600.0, quantity=100, sort_order=1,
    )
    s.add_all([a, b])
    s.commit()
    ids = (a.id, b.id)
    s.close()
    return ids


def _seed_holdings(Session) -> dict:
    """一套完整持仓面种子：两租户 stock/account/position 各一。"""
    stock_a, stock_b = _seed_stocks(Session)
    acc_a, acc_b = _seed_accounts(Session)
    pos_a, pos_b = _seed_positions(Session, acc_a, acc_b, stock_a, stock_b)
    return {
        "stock_a": stock_a, "stock_b": stock_b,
        "acc_a": acc_a, "acc_b": acc_b,
        "pos_a": pos_a, "pos_b": pos_b,
    }


def _get_unfiltered(Session, model, pk):
    """无 ctx 直查（种子/验尸用，绕过租户过滤——请求外无 ctx，机制放行）。"""
    s = Session()
    try:
        return s.get(model, pk)
    finally:
        s.close()


# ===========================================================================
# 1. stocks.py —— 自选股
# ===========================================================================


def test_stock_list_isolation(mt_env):
    """① GET /api/stocks：A 只见 A 的自选股，响应体不含 B 的 symbol/名称。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    stock_a, stock_b = _seed_stocks(Session)

    r = client.get("/api/stocks", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["id"] for x in body] == [stock_a]
    assert body[0]["symbol"] == "000001"
    assert body[0]["name"] == "平安银行"
    assert "600519" not in r.text and "贵州茅台" not in r.text

    r2 = client.get("/api/stocks", headers=_hdr(client, "admin"))
    assert [x["id"] for x in r2.json()] == [stock_b]
    assert "000001" not in r2.text


def test_stock_update_delete_cross_tenant_404(mt_env):
    """② A 改/删 B 的自选股 → 404，B 的行不受影响；改/删本租户正常。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    stock_a, stock_b = _seed_stocks(Session)
    hdr = _hdr(client, "u2")

    assert (
        client.put(
            f"/api/stocks/{stock_b}", json={"name": "被篡改"}, headers=hdr
        ).status_code
        == 404
    )
    assert client.delete(f"/api/stocks/{stock_b}", headers=hdr).status_code == 404

    row = _get_unfiltered(Session, M.Stock, stock_b)
    assert row is not None and row.name == "贵州茅台" and row.tenant_id == 1

    # 本租户可正常改/删
    assert (
        client.put(
            f"/api/stocks/{stock_a}", json={"name": "平安银行A"}, headers=hdr
        ).status_code
        == 200
    )
    assert client.delete(f"/api/stocks/{stock_a}", headers=hdr).status_code == 200
    assert _get_unfiltered(Session, M.Stock, stock_a) is None
    assert _get_unfiltered(Session, M.Stock, stock_b) is not None


def test_stock_agents_binding_cross_tenant_404(mt_env):
    """② A 给 B 的股票绑 Agent → 404；绑本租户股票 200 且响应含绑定。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    stock_a, stock_b = _seed_stocks(Session)
    s = Session()
    s.add(M.AgentConfig(name="daily_report", display_name="收盘复盘", kind="workflow"))
    s.commit()
    s.close()
    hdr = _hdr(client, "u2")
    payload = {"agents": [{"agent_name": "daily_report", "schedule": "0 16 * * *"}]}

    assert (
        client.put(
            f"/api/stocks/{stock_b}/agents", json=payload, headers=hdr
        ).status_code
        == 404
    )
    s = Session()
    assert s.query(M.StockAgent).filter_by(stock_id=stock_b).count() == 0
    s.close()

    r = client.put(f"/api/stocks/{stock_a}/agents", json=payload, headers=hdr)
    assert r.status_code == 200, r.text
    agents = r.json()["agents"]
    assert len(agents) == 1 and agents[0]["agent_name"] == "daily_report"
    assert agents[0]["schedule"] == "0 16 * * *"


def test_stock_reorder_cross_tenant_ignored(mt_env):
    """② reorder 混入 B 的股票 id：静默丢弃，只更新本租户行（docs/22 §2.1:241）。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    stock_a, stock_b = _seed_stocks(Session)

    r = client.put(
        "/api/stocks/reorder",
        json={
            "items": [
                {"id": stock_a, "sort_order": 5},
                {"id": stock_b, "sort_order": 9},
            ]
        },
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 1, "B 的股票 id 必须被过滤丢弃"
    assert _get_unfiltered(Session, M.Stock, stock_a).sort_order == 5
    assert _get_unfiltered(Session, M.Stock, stock_b).sort_order == 1


def test_stock_create_attributed_to_creator_tenant(mt_env):
    """③ A 创建自选股：tenant_id=A、A 可见、B（含管理员）不可见。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    _seed_stocks(Session)

    r = client.post(
        "/api/stocks",
        json={"symbol": "00700", "name": "腾讯控股", "market": "HK"},
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 200, r.text

    s = Session()
    row = s.query(M.Stock).filter_by(symbol="00700", market="HK").one()
    assert row.tenant_id == 2, "新行必须归属创建者租户"
    s.close()

    listed = client.get("/api/stocks", headers=_hdr(client, "u2")).json()
    assert "00700" in {x["symbol"] for x in listed}, "创建者必须可见自己的新股"
    admin_listed = client.get("/api/stocks", headers=_hdr(client, "admin")).json()
    assert "00700" not in {x["symbol"] for x in admin_listed}


# ===========================================================================
# 2. accounts.py —— 账户 / 持仓 / 流水 / 组合
# ===========================================================================


def test_account_list_isolation(mt_env):
    """① GET /api/accounts：A 只见 A 的账户，响应体不含 B 的账户名/资金。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    acc_a, acc_b = _seed_accounts(Session)

    r = client.get("/api/accounts", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["id"] for x in body] == [acc_a]
    assert body[0]["name"] == "A证券账户"
    assert body[0]["available_funds"] == 5000.0
    assert "B机密账户" not in r.text and "99999" not in r.text

    r2 = client.get("/api/accounts", headers=_hdr(client, "admin"))
    assert [x["id"] for x in r2.json()] == [acc_b]


def test_account_get_update_delete_cross_tenant_404(mt_env):
    """② A 对 B 的账户 GET/PUT/DELETE → 404，行不受影响；本租户正常。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    acc_a, acc_b = _seed_accounts(Session)
    hdr = _hdr(client, "u2")

    assert client.get(f"/api/accounts/{acc_b}", headers=hdr).status_code == 404
    assert (
        client.put(
            f"/api/accounts/{acc_b}", json={"available_funds": 1}, headers=hdr
        ).status_code
        == 404
    )
    assert client.delete(f"/api/accounts/{acc_b}", headers=hdr).status_code == 404

    row = _get_unfiltered(Session, M.Account, acc_b)
    assert row.available_funds == 99999.0 and row.name == "B机密账户"

    r = client.get(f"/api/accounts/{acc_a}", headers=hdr)
    assert r.status_code == 200 and r.json()["name"] == "A证券账户"
    assert client.delete(f"/api/accounts/{acc_a}", headers=hdr).status_code == 200
    assert _get_unfiltered(Session, M.Account, acc_a) is None


def test_account_create_attributed_to_creator_tenant(mt_env):
    """③ A 创建账户：tenant_id=A、A 可见、B 不可见。"""
    client, Session = mt_env
    _mk_two_tenants(Session)

    r = client.post(
        "/api/accounts",
        json={"name": "A新建账户", "available_funds": 123},
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 200, r.text

    s = Session()
    row = s.query(M.Account).filter_by(name="A新建账户").one()
    assert row.tenant_id == 2
    s.close()

    listed = client.get("/api/accounts", headers=_hdr(client, "u2")).json()
    assert "A新建账户" in {x["name"] for x in listed}
    admin_listed = client.get("/api/accounts", headers=_hdr(client, "admin")).json()
    assert "A新建账户" not in {x["name"] for x in admin_listed}


def test_position_list_isolation(mt_env):
    """① GET /api/positions：A 只见 A 的持仓（股数/股票字段）；按 B 的账户过滤为空。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    ids = _seed_holdings(Session)

    r = client.get("/api/positions", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["id"] for p in body] == [ids["pos_a"]]
    pos = body[0]
    assert pos["quantity"] == 700
    assert pos["stock_symbol"] == "000001"
    assert pos["account_name"] == "A证券账户"
    assert "600519" not in r.text and "1600" not in r.text

    # 用 B 的 account_id 过滤 → 空（不报错、不返回 B 的行）
    r2 = client.get(
        f"/api/positions?account_id={ids['acc_b']}", headers=_hdr(client, "u2")
    )
    assert r2.status_code == 200 and r2.json() == []

    # 用 B 的 stock_id 过滤 → 同样为空
    r3 = client.get(
        f"/api/positions?stock_id={ids['stock_b']}", headers=_hdr(client, "u2")
    )
    assert r3.status_code == 200 and r3.json() == []


def test_position_update_delete_cross_tenant_404(mt_env):
    """② A 改/删 B 的持仓 → 404，B 的股数不变；改本租户持仓生成 adjustment 流水。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    ids = _seed_holdings(Session)
    hdr = _hdr(client, "u2")

    assert (
        client.put(
            f"/api/positions/{ids['pos_b']}", json={"quantity": 1}, headers=hdr
        ).status_code
        == 404
    )
    assert (
        client.delete(f"/api/positions/{ids['pos_b']}", headers=hdr).status_code == 404
    )
    row = _get_unfiltered(Session, M.Position, ids["pos_b"])
    assert row.quantity == 100

    r = client.put(
        f"/api/positions/{ids['pos_a']}", json={"quantity": 800}, headers=hdr
    )
    assert r.status_code == 200, r.text
    assert r.json()["quantity"] == 800
    # 注：改仓自动生成的 adjustment 流水归属缺陷见下方 xfail 用例


def test_position_update_adjustment_trade_visible_to_creator(mt_env):
    """③ A 改仓生成的 adjustment 流水：tenant_id=A 且 A 可查回（Phase 1 契约）。

    update_position 内 db.add(PositionTrade(...)) 未带 tenant_id → adjustment 行落
    租户 1，创建者查 /trades 反而看不到自己的调整记录（同一 INSERT 归属根因）。
    """
    client, Session = mt_env
    _mk_two_tenants(Session)
    ids = _seed_holdings(Session)
    hdr = _hdr(client, "u2")

    r = client.put(
        f"/api/positions/{ids['pos_a']}", json={"quantity": 800}, headers=hdr
    )
    assert r.status_code == 200, r.text

    s = Session()
    trade = (
        s.query(M.PositionTrade)
        .filter_by(position_id=ids["pos_a"], direction="adjustment")
        .one()
    )
    assert trade.tenant_id == 2
    s.close()

    r2 = client.get(f"/api/positions/{ids['pos_a']}/trades", headers=hdr)
    assert r2.status_code == 200
    assert any(t["direction"] == "adjustment" for t in r2.json())


def test_position_create_cross_tenant_refs_rejected(mt_env):
    """② A 用 B 的账户/股票建持仓 → 400 不存在（不泄露 B 资源）。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    ids = _seed_holdings(Session)
    hdr = _hdr(client, "u2")

    r = client.post(
        "/api/positions",
        json={
            "account_id": ids["acc_b"],
            "stock_id": ids["stock_a"],
            "cost_price": 1,
            "quantity": 1,
        },
        headers=hdr,
    )
    assert r.status_code == 400 and "账户不存在" in r.text

    r2 = client.post(
        "/api/positions",
        json={
            "account_id": ids["acc_a"],
            "stock_id": ids["stock_b"],
            "cost_price": 1,
            "quantity": 1,
        },
        headers=hdr,
    )
    assert r2.status_code == 400 and "股票不存在" in r2.text


def test_position_create_attributed_to_creator_tenant(mt_env):
    """③ A 建持仓：tenant_id=A、A 的列表可见、B 不可见。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    ids = _seed_holdings(Session)
    s = Session()
    extra_stock = M.Stock(tenant_id=2, symbol="300750", name="宁德时代", market="CN")
    s.add(extra_stock)
    s.commit()
    extra_id = extra_stock.id
    s.close()

    r = client.post(
        "/api/positions",
        json={
            "account_id": ids["acc_a"],
            "stock_id": extra_id,
            "cost_price": 200.0,
            "quantity": 50,
        },
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 200, r.text

    s = Session()
    row = s.query(M.Position).filter_by(stock_id=extra_id).one()
    assert row.tenant_id == 2
    s.close()

    listed = client.get("/api/positions", headers=_hdr(client, "u2")).json()
    assert "300750" in {p["stock_symbol"] for p in listed}


def test_trades_cross_tenant_404(mt_env):
    """② A 对 B 的持仓查/录流水 → 404（不泄露存在性），B 的流水不受影响。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    ids = _seed_holdings(Session)
    s = Session()
    s.add(
        M.PositionTrade(
            tenant_id=1, position_id=ids["pos_b"], direction="buy",
            price=1600.0, quantity=100, fee=0, traded_at=datetime(2025, 1, 2),
        )
    )
    s.commit()
    s.close()
    hdr = _hdr(client, "u2")

    assert (
        client.get(f"/api/positions/{ids['pos_b']}/trades", headers=hdr).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/positions/{ids['pos_b']}/trades",
            json={"direction": "sell", "price": 1700, "quantity": 100},
            headers=hdr,
        ).status_code
        == 404
    )

    s = Session()
    trades = s.query(M.PositionTrade).filter_by(position_id=ids["pos_b"]).all()
    assert len(trades) == 1 and trades[0].price == 1600.0, "B 的流水不得被篡改"
    pos_b = s.get(M.Position, ids["pos_b"])
    assert pos_b.quantity == 100, "B 的持仓数量不得被 A 的减仓请求改动"
    s.close()


def test_trade_create_attributed_to_creator_tenant(mt_env):
    """③ A 录流水：PositionTrade.tenant_id=A 且 A 可查回。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    ids = _seed_holdings(Session)
    hdr = _hdr(client, "u2")

    r = client.post(
        f"/api/positions/{ids['pos_a']}/trades",
        json={"direction": "buy", "price": 11.0, "quantity": 100, "fee": 5},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["trade"]["direction"] == "buy"
    assert r.json()["position"]["quantity"] == 800

    s = Session()
    trade = s.query(M.PositionTrade).filter_by(position_id=ids["pos_a"]).one()
    assert trade.tenant_id == 2
    s.close()

    r2 = client.get(f"/api/positions/{ids['pos_a']}/trades", headers=hdr)
    assert any(t["direction"] == "buy" for t in r2.json()), "A 必须能查回自己的流水"


def test_positions_reorder_cross_tenant_ignored(mt_env):
    """② 持仓 reorder 混入 B 的 id：只更新本租户行。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    ids = _seed_holdings(Session)

    r = client.put(
        "/api/positions/reorder/batch",
        json={
            "items": [
                {"id": ids["pos_a"], "sort_order": 7},
                {"id": ids["pos_b"], "sort_order": 9},
            ]
        },
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 200 and r.json()["updated"] == 1
    assert _get_unfiltered(Session, M.Position, ids["pos_a"]).sort_order == 7
    assert _get_unfiltered(Session, M.Position, ids["pos_b"]).sort_order == 1


def test_portfolio_summary_isolation(mt_env):
    """① portfolio/summary：A 的汇总只含 A 的账户/持仓；指定 B 的账户返回空。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    ids = _seed_holdings(Session)

    r = client.get(
        "/api/portfolio/summary?include_quotes=false", headers=_hdr(client, "u2")
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["id"] for a in body["accounts"]] == [ids["acc_a"]]
    acc = body["accounts"][0]
    assert acc["name"] == "A证券账户"
    assert acc["available_funds"] == 5000.0
    assert [p["symbol"] for p in acc["positions"]] == ["000001"]
    assert acc["positions"][0]["quantity"] == 700
    assert body["total"]["available_funds"] == 5000.0, "总资金不得并入 B 的 99999"
    assert "B机密账户" not in r.text and "600519" not in r.text

    # 指定 B 的账户 id → 空汇总（不 404、不返回 B 的数据）
    r2 = client.get(
        f"/api/portfolio/summary?include_quotes=false&account_id={ids['acc_b']}",
        headers=_hdr(client, "u2"),
    )
    assert r2.status_code == 200 and r2.json()["accounts"] == []


def test_portfolio_todos_isolation(mt_env):
    """① portfolio/todos：只按 A 的持仓生成待办，不含 B 的持仓股。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    _seed_holdings(Session)

    r = client.get("/api/portfolio/todos", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    todos = r.json()["todos"]
    symbols = {t["symbol"] for t in todos}
    assert "000001" in symbols, "A 的持仓股未设提醒应生成待办"
    assert "600519" not in symbols, "B 的持仓股不得进入 A 的待办"
    assert "贵州茅台" not in r.text


# ===========================================================================
# 3. suggestions.py —— 建议池（core.suggestion_pool 自建 SessionLocal）
# ===========================================================================


def _seed_suggestions(Session) -> dict:
    """同一 symbol 两租户各一条建议（字段值可区分）+ B 的另一条。"""
    from src.core.timezone import utc_now

    now = utc_now()
    s = Session()
    a = M.StockSuggestion(
        tenant_id=2, stock_symbol="600519", stock_market="CN", stock_name="贵州茅台",
        action="buy", action_label="建仓-A策略", signal="A的买入信号",
        reason="A租户的理由", agent_name="daily_report", agent_label="收盘复盘",
        expires_at=now + timedelta(hours=8),
    )
    b = M.StockSuggestion(
        tenant_id=1, stock_symbol="600519", stock_market="CN", stock_name="贵州茅台",
        action="sell", action_label="清仓-B策略", signal="B的卖出信号",
        reason="B租户的理由", agent_name="daily_report", agent_label="收盘复盘",
        expires_at=now + timedelta(hours=8),
    )
    b2 = M.StockSuggestion(
        tenant_id=1, stock_symbol="000001", stock_market="CN", stock_name="平安银行",
        action="watch", action_label="观望-B", agent_name="news_digest",
        expires_at=now + timedelta(hours=8),
    )
    s.add_all([a, b, b2])
    s.commit()
    ids = {"a": a.id, "b": b.id, "b2": b2.id}
    s.close()
    return ids


def test_suggestions_by_symbol_isolation(mt_env):
    """① GET /api/suggestions/{symbol}：A 只拿到 A 的建议，不含 B 的 action/reason。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    _seed_suggestions(Session)

    r = client.get(
        "/api/suggestions/600519?market=CN", headers=_hdr(client, "u2")
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["action_label"] == "建仓-A策略"
    assert body[0]["reason"] == "A租户的理由"
    assert "B的卖出信号" not in r.text and "B租户的理由" not in r.text

    r2 = client.get(
        "/api/suggestions/600519?market=CN", headers=_hdr(client, "admin")
    )
    labels = {x["action_label"] for x in r2.json()}
    assert labels == {"清仓-B策略"}


def test_suggestions_latest_isolation(mt_env):
    """① GET /api/suggestions：A 的最新建议字典只含 A 的行。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    _seed_suggestions(Session)

    r = client.get("/api/suggestions", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {"CN:600519"}
    assert body["CN:600519"]["action_label"] == "建仓-A策略"

    r2 = client.get("/api/suggestions", headers=_hdr(client, "admin"))
    body2 = r2.json()
    assert set(body2.keys()) == {"CN:600519", "CN:000001"}
    assert body2["CN:600519"]["action_label"] == "清仓-B策略"
    assert "A的买入信号" not in r2.text


def test_suggestions_cleanup_tenant_scoped(mt_env):
    """② DELETE /cleanup：bulk delete 必须只清本租户（docs/22 机制点 DML 分支）。"""
    from src.core.timezone import utc_now

    client, Session = mt_env
    _mk_two_tenants(Session)
    now = utc_now()
    s = Session()
    old_a = M.StockSuggestion(
        tenant_id=2, stock_symbol="111AAA", action="buy", action_label="旧-A",
        agent_name="daily_report", created_at=now - timedelta(days=30),
    )
    old_b = M.StockSuggestion(
        tenant_id=1, stock_symbol="222BBB", action="sell", action_label="旧-B",
        agent_name="daily_report", created_at=now - timedelta(days=30),
    )
    fresh_a = M.StockSuggestion(
        tenant_id=2, stock_symbol="333CCC", action="buy", action_label="新-A",
        agent_name="daily_report", created_at=now,
    )
    s.add_all([old_a, old_b, fresh_a])
    s.commit()
    old_b_id = old_b.id
    fresh_a_id = fresh_a.id
    s.close()

    r = client.delete(
        "/api/suggestions/cleanup?days=7", headers=_hdr(client, "u2")
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1, "只应删除 A 的一条过期建议"

    s = Session()
    remaining = {x.id for x in s.query(M.StockSuggestion).all()}
    assert old_b_id in remaining, "B 的过期建议不得被 A 的清理删除"
    assert fresh_a_id in remaining, "A 的未过期建议不得被删除"
    s.close()


# ===========================================================================
# 4. playbooks.py —— 方案档案
# ===========================================================================


def _seed_playbooks(Session, stock_a: int, stock_b: int) -> dict:
    s = Session()
    a1 = M.StockPlaybook(
        tenant_id=2, stock_id=stock_a, version=1, is_active=True,
        payload={"schema_version": 1, "thesis": "A的多头方案"},
        summary="A方案摘要", note="A注",
    )
    b1 = M.StockPlaybook(
        tenant_id=1, stock_id=stock_b, version=1, is_active=True,
        payload={"schema_version": 1, "thesis": "B的机密方案"},
        summary="B方案摘要", note="B注",
    )
    s.add_all([a1, b1])
    s.commit()
    ids = {"a1": a1.id, "b1": b1.id}
    s.close()
    return ids


def test_playbook_get_list_cross_tenant_404(mt_env):
    """② A 访问 B 股票的方案端点 → 404（股票级拦截，不泄露存在性）。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    stock_a, stock_b = _seed_stocks(Session)
    pbs = _seed_playbooks(Session, stock_a, stock_b)
    hdr = _hdr(client, "u2")

    assert (
        client.get(f"/api/stocks/{stock_b}/playbook", headers=hdr).status_code == 404
    )
    assert (
        client.get(f"/api/stocks/{stock_b}/playbooks", headers=hdr).status_code == 404
    )
    # 直接按 id 激活 B 的方案 → 404 且状态不变
    assert (
        client.post(f"/api/playbooks/{pbs['b1']}/activate", headers=hdr).status_code
        == 404
    )
    row = _get_unfiltered(Session, M.StockPlaybook, pbs["b1"])
    assert row.is_active in (True, 1)


def test_playbook_list_isolation_own(mt_env):
    """① A 查本租户股票方案：只见 A 的版本，payload 全文不含 B 的内容。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    stock_a, stock_b = _seed_stocks(Session)
    _seed_playbooks(Session, stock_a, stock_b)
    hdr = _hdr(client, "u2")

    r = client.get(f"/api/stocks/{stock_a}/playbooks", headers=hdr)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    assert body[0]["summary"] == "A方案摘要"
    assert body[0]["is_active"] is True

    r2 = client.get(f"/api/stocks/{stock_a}/playbook", headers=hdr)
    assert r2.status_code == 200, r2.text
    detail = r2.json()
    assert detail["payload"]["thesis"] == "A的多头方案"
    assert "B的机密方案" not in r2.text


def test_playbook_create_attributed_to_creator_tenant(mt_env):
    """③ A 建方案版本：tenant_id=A、激活切换不越租户、B 的方案不受影响。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    stock_a, stock_b = _seed_stocks(Session)
    pbs = _seed_playbooks(Session, stock_a, stock_b)
    hdr = _hdr(client, "u2")

    r = client.post(
        f"/api/stocks/{stock_a}/playbooks",
        json={"payload": {"schema_version": 1, "thesis": "A的第二版"}, "note": "v2"},
        headers=hdr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert r.json()["is_active"] is True

    s = Session()
    row = s.query(M.StockPlaybook).filter_by(stock_id=stock_a, version=2).one()
    assert row.tenant_id == 2, "新版本必须归属创建者租户"
    # B 的方案激活态不得被 A 的创建动作（bulk update 机制点）波及
    b_row = s.get(M.StockPlaybook, pbs["b1"])
    assert b_row.is_active in (True, 1)
    s.close()

    r2 = client.get(f"/api/stocks/{stock_a}/playbook", headers=hdr)
    assert r2.json()["version"] == 2, "A 必须能读回自己新建的激活版本"


# ===========================================================================
# 5. recommendations.py —— 入场候选 / 策略信号（哨兵表 tenant_id IN (ctx, 0)）
# ===========================================================================


def _seed_entry_candidates(Session) -> None:
    """A(t2)=AAA111 watchlist；B(t1)=BBB222 watchlist；哨兵(t0)=MKT000 market_scan。"""
    s = Session()
    rows = [
        M.EntryCandidate(
            tenant_id=2, stock_symbol="AAA111", stock_market="CN", stock_name="A候选",
            snapshot_date=SNAP, status="active", score=80,
            candidate_source="watchlist", action="buy", action_label="建仓",
        ),
        M.EntryCandidate(
            tenant_id=1, stock_symbol="BBB222", stock_market="CN", stock_name="B候选",
            snapshot_date=SNAP, status="active", score=90,
            candidate_source="watchlist", action="buy", action_label="建仓",
        ),
        M.EntryCandidate(
            tenant_id=0, stock_symbol="MKT000", stock_market="CN", stock_name="市场扫描股",
            snapshot_date=SNAP, status="active", score=70,
            candidate_source="market_scan", action="watch", action_label="观望",
        ),
    ]
    s.add_all(rows)
    s.commit()
    s.close()


def _seed_signal_runs(Session) -> None:
    """A(t2)/B(t1) watchlist 信号 + 哨兵(t0) market_scan 信号。"""
    s = Session()
    rows = [
        M.StrategySignalRun(
            tenant_id=2, snapshot_date=SNAP, stock_symbol="AAA111", stock_market="CN",
            strategy_code="market_scan", source_pool="watchlist", status="active",
            score=80, rank_score=80,
        ),
        M.StrategySignalRun(
            tenant_id=1, snapshot_date=SNAP, stock_symbol="BBB222", stock_market="CN",
            strategy_code="market_scan", source_pool="watchlist", status="active",
            score=90, rank_score=90,
        ),
        M.StrategySignalRun(
            tenant_id=0, snapshot_date=SNAP, stock_symbol="MKT000", stock_market="CN",
            strategy_code="market_scan", source_pool="market_scan", status="active",
            score=70, rank_score=70,
        ),
    ]
    s.add_all(rows)
    s.commit()
    s.close()


def test_entry_candidates_sentinel_isolation(mt_env):
    """① entry-candidates：A 见 A + 市场级哨兵(tenant 0)，不见 B 的私有行。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    _seed_entry_candidates(Session)

    r = client.get(
        "/api/recommendations/entry-candidates", headers=_hdr(client, "u2")
    )
    assert r.status_code == 200, r.text
    symbols = {x["stock_symbol"] for x in r.json()["items"]}
    assert symbols == {"AAA111", "MKT000"}, "哨兵行可见、B 的私有行必须不可见"
    assert "B候选" not in r.text

    r2 = client.get(
        "/api/recommendations/entry-candidates", headers=_hdr(client, "admin")
    )
    symbols2 = {x["stock_symbol"] for x in r2.json()["items"]}
    assert symbols2 == {"BBB222", "MKT000"}


def test_strategy_signals_sentinel_isolation(mt_env):
    """① strategy-signals：同哨兵语义——A 见 A + tenant 0，不见 B。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    _seed_signal_runs(Session)

    r = client.get(
        "/api/recommendations/strategy-signals", headers=_hdr(client, "u2")
    )
    assert r.status_code == 200, r.text
    symbols = {x["stock_symbol"] for x in r.json()["items"]}
    assert symbols == {"AAA111", "MKT000"}
    assert "BBB222" not in r.text

    r2 = client.get(
        "/api/recommendations/strategy-signals", headers=_hdr(client, "admin")
    )
    symbols2 = {x["stock_symbol"] for x in r2.json()["items"]}
    assert symbols2 == {"BBB222", "MKT000"}


def test_strategy_factor_snapshot_shared_by_design(mt_env):
    """设计意图留证：strategy_factor_snapshots 为市场级共享表（docs/20 M2），
    跨租户可读不算泄漏；本用例固化该语义，防误改注册表。"""
    client, Session = mt_env
    _mk_two_tenants(Session)
    _seed_signal_runs(Session)
    s = Session()
    b_run = s.query(M.StrategySignalRun).filter_by(stock_symbol="BBB222").one()
    s.add(
        M.StrategyFactorSnapshot(
            signal_run_id=b_run.id, snapshot_date=SNAP, stock_symbol="BBB222",
            stock_market="CN", strategy_code="market_scan", final_score=88.0,
        )
    )
    s.commit()
    run_id = b_run.id
    s.close()

    r = client.get(
        f"/api/recommendations/strategy-factors/{run_id}",
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["stock_symbol"] == "BBB222", "市场级共享表按设计跨租户可读"


def test_candidate_feedback_attributed_to_creator_tenant(mt_env):
    """③ A 提交候选反馈：tenant_id=A（entry_candidate_feedback 为租户私有表）。"""
    client, Session = mt_env
    _mk_two_tenants(Session)

    r = client.post(
        "/api/recommendations/entry-candidates/feedback",
        json={
            "snapshot_date": SNAP,
            "stock_symbol": "AAA111",
            "stock_market": "CN",
            "useful": True,
            "reason": "A的反馈",
        },
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 200 and r.json()["ok"] is True

    s = Session()
    row = s.query(M.EntryCandidateFeedback).filter_by(stock_symbol="AAA111").one()
    assert row.tenant_id == 2, "反馈必须归属提交者租户"
    s.close()
