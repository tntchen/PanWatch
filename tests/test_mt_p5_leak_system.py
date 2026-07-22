"""MT-P5-B 跨租户泄漏断言套件·系统面（告警/模拟盘/设置/日志/历史/上下文/看板/洞察）。

断言模式（docs/14 双租户对照）：
  ① list/detail 不串租户；② 持 B 的 id 越权访问 → 404/403/空集（不泄露存在性）；
  ③ 写入归属正确（tenant_id 归属当前租户）。

覆盖 router：price_alerts / paper_trading / settings / logs / history / context /
dashboard / insights（挂载前缀与 src/web/app.py 一致）。

经核实为「实例级共享 / 引擎路由」端点（设计上不做行级隔离，本套件不强行断言）：
  - GET /api/settings、/api/settings/version、/api/settings/update-check：
    app_settings 在注册表中属实例级 SHARED_TABLES，读面全实例一份（T20 三分后
    实例级键本就共享；租户级键的写面问题见 xfail 用例）。
  - GET /api/paper-trading/backends（回测后端探测，进程级）、
    GET /api/paper-trading/diagnostics（组合诊断，引擎按租户遍历内部处理）、
    POST /api/paper-trading/scan、/account/reset、/positions/{id}/close、
    /notify-test、/premarket-plan、/daily-summary：均为引擎入口，
    租户隔离在引擎/调度层（MT-P3 已按租户扇出），API 面无行数据直出。
  - POST /api/price-alerts/{id}/test、/scan：引擎扫描入口，dry_run 不写库。
  - POST /api/context/predictions/evaluate：后验评估引擎入口，
    内部经 context_store 按 ctx 租户解析，且需行情网络访问。
  - POST /api/insights/add-position-eval、/announcement-eval、/api/dashboard/curate：
    AI 代理类端点，无行级数据直出；announcement 缓存 key 已按租户前缀（MT-P2 J11）。
  - GET /api/logs/health 的 writer 字段：log handler 进程级统计。

历史缺陷登记（D1/D2 第一批已修、D3/D4/D5 第二批已修，xfail 均已摘除）：
  D1. price_alerts 新建规则不落 tenant_id → server_default 归租户 1，
      创建者不可见且污染租户 1 数据面。
  D2. paper_trading GET /account 懒建账户不落 tenant_id → 归租户 1；
      租户 1 已有账户时他租户首访必 500（uq_paper_account_tenant 冲突）。
  D3. settings 租户级键（notify_*/avatar 等）曾写共享 app_settings，
      跨租户互相覆盖可见 → 已修：多租户下 upsert tenant_settings。
  D4. paper_trading /notify-settings 曾读写共享 app_settings（运行时读 tenant_settings
      优先）→ 已修：读写均切 tenant_settings 优先口径。
  D5. Query.count()（匿名子查询形态）绕过 do_orm_execute 实体判定 → total 计数
      跨租户泄漏 → 已修：logs list/meta、paper_trading trades 的 count 前
      显式补 tenant 谓词。func.count(X.id) 直查形态
      （dashboard kpis）实体可判定，不受影响（本套件有对照用例）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M
import src.core.context_store as context_store
import src.core.suggestion_pool as suggestion_pool
import src.web.tenant_context as tc
from src.web.api import (
    auth,
    context,
    dashboard,
    history,
    insights,
    logs,
    paper_trading,
    price_alerts,
    settings,
)
from src.web.api.auth import get_current_user
from src.web.database import Base, get_db

SECRET = "mt-p5-system-secret-0123456789abcdef"
OLD_PWD_AT = datetime(2020, 1, 1, 0, 0, 0)
NOW = datetime.now(timezone.utc).replace(tzinfo=None)
TODAY = datetime.now().strftime("%Y-%m-%d")
OLD_DATE = "2020-01-01"

_ALERT_GROUP = {"op": "and", "items": [{"type": "price", "op": ">=", "value": 100}]}


@pytest.fixture
def mt_env(monkeypatch):
    """内存库 + do_orm_execute 事件 + 固定 JWT secret + 系统面 routers。

    context/insights 端点不走 get_db，而是经 context_store / suggestion_pool
    自建 SessionLocal()，这里把两处模块级 SessionLocal 一并替换为内存工厂
    （工厂已挂 do_orm_execute 事件，等价生产 SessionLocal 的过滤机制）。
    """
    monkeypatch.setattr(auth, "_jwt_secret", SECRET)
    monkeypatch.setattr(auth, "ENV_AUTH_USERNAME", None)
    monkeypatch.setattr(auth, "ENV_AUTH_PASSWORD", None)

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

    monkeypatch.setattr(context_store, "SessionLocal", Session)
    monkeypatch.setattr(suggestion_pool, "SessionLocal", Session)

    protected = [Depends(get_current_user)]
    app = FastAPI(redirect_slashes=False)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(price_alerts.router, prefix="/api/price-alerts", dependencies=protected)
    app.include_router(paper_trading.router, prefix="/api/paper-trading", dependencies=protected)
    app.include_router(settings.router, prefix="/api/settings", dependencies=protected)
    app.include_router(logs.router, prefix="/api/logs", dependencies=protected)
    app.include_router(history.router, prefix="/api", dependencies=protected)
    app.include_router(context.router, prefix="/api", dependencies=protected)
    app.include_router(dashboard.router, prefix="/api/dashboard", dependencies=protected)
    app.include_router(insights.router, prefix="/api/insights", dependencies=protected)

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


def _mkuser(Session, username: str, *, tenant_id: int, role: str = "user") -> None:
    s = Session()
    if s.get(M.Tenant, tenant_id) is None:
        s.add(M.Tenant(id=tenant_id, name=f"租户{tenant_id}", is_default=(tenant_id == 1)))
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


def _hdr(client: TestClient, username: str) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": "secret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _std_users(Session):
    """租户1 admin + 租户2 u2 + 租户3 u3。"""
    _mkuser(Session, "admin", tenant_id=1, role="admin")
    _mkuser(Session, "u2", tenant_id=2)
    _mkuser(Session, "u3", tenant_id=3)


def _seed_stock(Session, tenant_id: int, symbol: str, name: str) -> int:
    s = Session()
    row = M.Stock(tenant_id=tenant_id, symbol=symbol, name=name, market="CN")
    s.add(row)
    s.commit()
    sid = row.id
    s.close()
    return sid


def _seed_rule(Session, tenant_id: int, stock_id: int, name: str) -> int:
    s = Session()
    row = M.PriceAlertRule(
        tenant_id=tenant_id, stock_id=stock_id, name=name, enabled=True,
        condition_group=dict(_ALERT_GROUP),
    )
    s.add(row)
    s.commit()
    rid = row.id
    s.close()
    return rid


# ---------------------------------------------------------------------------
# 1. price_alerts：告警规则 / 触发记录
# ---------------------------------------------------------------------------


def test_alert_rules_list_isolated(mt_env, monkeypatch):
    """多租户：规则列表只出本租户行，关联股票名也不串。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    s1 = _seed_stock(Session, 1, "600000", "浦发")
    s2 = _seed_stock(Session, 2, "600519", "茅台")
    r1 = _seed_rule(Session, 1, s1, "T1规则")
    r2 = _seed_rule(Session, 2, s2, "T2规则")

    body = client.get("/api/price-alerts", headers=_hdr(client, "u2")).json()
    assert [x["id"] for x in body] == [r2]
    assert body[0]["stock_name"] == "茅台"
    assert r1 not in [x["id"] for x in body]


def test_alert_rule_cross_tenant_write_404(mt_env, monkeypatch):
    """多租户：越权改/启停/删他租户规则一律 404，行不受影响。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    s1 = _seed_stock(Session, 1, "600000", "浦发")
    s2 = _seed_stock(Session, 2, "600519", "茅台")
    r1 = _seed_rule(Session, 1, s1, "T1规则")
    r2 = _seed_rule(Session, 2, s2, "T2规则")
    hdr = _hdr(client, "u2")

    assert client.put(f"/api/price-alerts/{r1}", json={"name": "x"}, headers=hdr).status_code == 404
    assert client.post(f"/api/price-alerts/{r1}/toggle", json={"enabled": False}, headers=hdr).status_code == 404
    assert client.delete(f"/api/price-alerts/{r1}", headers=hdr).status_code == 404

    s = Session()
    row = s.get(M.PriceAlertRule, r1)
    assert row.name == "T1规则" and row.enabled in (True, 1)
    s.close()

    # 本租户规则可正常操作
    assert client.post(f"/api/price-alerts/{r2}/toggle", json={"enabled": False}, headers=hdr).status_code == 200
    assert client.delete(f"/api/price-alerts/{r2}", headers=hdr).status_code == 200


def test_alert_create_with_foreign_stock_404(mt_env, monkeypatch):
    """多租户：持他租户 stock_id 建规则 → 404（不泄露股票存在性），且不产生行。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    s1 = _seed_stock(Session, 1, "600000", "浦发")

    r = client.post(
        "/api/price-alerts",
        json={"stock_id": s1, "name": "越权规则", "condition_group": dict(_ALERT_GROUP)},
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 404
    s = Session()
    assert s.query(M.PriceAlertRule).count() == 0
    s.close()


def test_alert_create_attributed_to_current_tenant(mt_env, monkeypatch):
    """多租户：新建规则应归属当前租户且创建者立即可见。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    s2 = _seed_stock(Session, 2, "600519", "茅台")

    r = client.post(
        "/api/price-alerts",
        json={"stock_id": s2, "name": "T2规则", "condition_group": dict(_ALERT_GROUP)},
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    s = Session()
    row = s.get(M.PriceAlertRule, rid)
    assert row is not None and row.tenant_id == 2, f"规则应归属租户2，实际 tenant_id={row.tenant_id if row else None}"
    s.close()
    assert rid in [x["id"] for x in client.get("/api/price-alerts", headers=_hdr(client, "u2")).json()]


def test_alert_hits_isolated(mt_env, monkeypatch):
    """多租户：他租户规则的命中记录返回空集；本租户正常返回。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    s1 = _seed_stock(Session, 1, "600000", "浦发")
    s2 = _seed_stock(Session, 2, "600519", "茅台")
    r1 = _seed_rule(Session, 1, s1, "T1规则")
    r2 = _seed_rule(Session, 2, s2, "T2规则")

    s = Session()
    s.add_all([
        M.PriceAlertHit(tenant_id=1, rule_id=r1, stock_id=s1, trigger_time=NOW, trigger_bucket="b1"),
        M.PriceAlertHit(tenant_id=2, rule_id=r2, stock_id=s2, trigger_time=NOW, trigger_bucket="b2"),
    ])
    s.commit()
    s.close()
    hdr = _hdr(client, "u2")

    # 他租户规则 id：端点设计为不 404（rule 查询结果弃用），但必须空集
    assert client.get(f"/api/price-alerts/{r1}/hits", headers=hdr).json() == []
    own = client.get(f"/api/price-alerts/{r2}/hits", headers=hdr).json()
    assert len(own) == 1 and own[0]["rule_id"] == r2

    today = client.get("/api/price-alerts/hits/today", headers=hdr).json()
    assert [h["rule_id"] for h in today] == [r2]


def test_single_tenant_alerts_passthrough(mt_env, monkeypatch):
    """单租户直通：全表可见、可改他租户行（等价改造前）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    client, Session = mt_env
    _std_users(Session)
    s1 = _seed_stock(Session, 1, "600000", "浦发")
    r1 = _seed_rule(Session, 1, s1, "T1规则")

    hdr = _hdr(client, "u2")
    assert r1 in [x["id"] for x in client.get("/api/price-alerts", headers=hdr).json()]
    assert client.put(f"/api/price-alerts/{r1}", json={"name": "renamed"}, headers=hdr).status_code == 200


# ---------------------------------------------------------------------------
# 2. paper_trading：模拟盘账户/持仓/交易
# ---------------------------------------------------------------------------


def _seed_pt_account(Session, tenant_id: int, capital: float) -> int:
    s = Session()
    row = M.PaperTradingAccount(
        tenant_id=tenant_id, initial_capital=capital,
        current_capital=capital, peak_capital=capital,
    )
    s.add(row)
    s.commit()
    aid = row.id
    s.close()
    return aid


def test_pt_account_isolated(mt_env, monkeypatch):
    """多租户：各租户只见本租户模拟盘账户。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    _seed_pt_account(Session, 1, 1000000.0)
    a2 = _seed_pt_account(Session, 2, 500000.0)

    body = client.get("/api/paper-trading/account", headers=_hdr(client, "u2")).json()
    assert body["id"] == a2
    assert body["initial_capital"] == 500000.0

    body1 = client.get("/api/paper-trading/account", headers=_hdr(client, "admin")).json()
    assert body1["initial_capital"] == 1000000.0


def test_pt_account_autocreate_attributed_to_current_tenant(mt_env, monkeypatch):
    """多租户：无账户租户首访懒建的账户应归属本租户且可复访。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    _seed_pt_account(Session, 1, 1000000.0)  # 租户1已有账户

    hdr = _hdr(client, "u2")
    r = client.get("/api/paper-trading/account", headers=hdr)
    assert r.status_code == 200, r.text  # 现缺陷：与租户1账户唯一冲突 → 500
    # 复访应稳定返回同一账户
    r2 = client.get("/api/paper-trading/account", headers=hdr)
    assert r2.status_code == 200 and r2.json()["id"] == r.json()["id"]
    s = Session()
    row = s.query(M.PaperTradingAccount).filter_by(tenant_id=2).one_or_none()
    assert row is not None, "懒建账户应归属租户2"
    s.close()


def test_pt_positions_and_trades_isolated(mt_env, monkeypatch):
    """多租户：持仓与已平仓交易列表不串租户。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    a1 = _seed_pt_account(Session, 1, 1000000.0)
    a2 = _seed_pt_account(Session, 2, 500000.0)

    s = Session()
    s.add_all([
        M.PaperTradingPosition(tenant_id=1, account_id=a1, stock_symbol="600000", entry_price=10.0, status="open"),
        M.PaperTradingPosition(tenant_id=2, account_id=a2, stock_symbol="600519", entry_price=1700.0, status="open"),
        M.PaperTradingTrade(tenant_id=1, account_id=a1, stock_symbol="600000", entry_price=10.0, exit_price=11.0, pnl=100.0, pnl_pct=10.0, closed_at=NOW),
        M.PaperTradingTrade(tenant_id=2, account_id=a2, stock_symbol="600519", entry_price=1700.0, exit_price=1600.0, pnl=-100.0, pnl_pct=-5.88, closed_at=NOW),
    ])
    s.commit()
    s.close()
    hdr = _hdr(client, "u2")

    positions = client.get("/api/paper-trading/positions?status=all", headers=hdr).json()
    assert [p["stock_symbol"] for p in positions] == ["600519"]

    trades = client.get("/api/paper-trading/trades", headers=hdr).json()
    assert [t["stock_symbol"] for t in trades["items"]] == ["600519"]
    assert all(t["stock_symbol"] != "600000" for t in trades["items"])


def test_pt_trades_total_count_not_leaked(mt_env, monkeypatch):
    """多租户：trades total 应只计本租户（D5 已修复：count 前显式 tenant 谓词）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    a1 = _seed_pt_account(Session, 1, 1000000.0)
    a2 = _seed_pt_account(Session, 2, 500000.0)
    s = Session()
    s.add_all([
        M.PaperTradingTrade(tenant_id=1, account_id=a1, stock_symbol="600000", entry_price=10.0, exit_price=11.0, pnl=100.0, pnl_pct=10.0, closed_at=NOW),
        M.PaperTradingTrade(tenant_id=2, account_id=a2, stock_symbol="600519", entry_price=1700.0, exit_price=1600.0, pnl=-100.0, pnl_pct=-5.88, closed_at=NOW),
    ])
    s.commit()
    s.close()

    trades = client.get("/api/paper-trading/trades", headers=_hdr(client, "u2")).json()
    assert trades["total"] == 1, f"total 泄漏他租户计数: {trades['total']}"


def test_pt_account_write_scoped_to_own_tenant(mt_env, monkeypatch):
    """多租户：toggle/settings 只作用本租户账户，他租户账户行不变。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    a1 = _seed_pt_account(Session, 1, 1000000.0)
    _seed_pt_account(Session, 2, 500000.0)
    hdr = _hdr(client, "u2")

    r = client.post("/api/paper-trading/account/toggle", json={"enabled": False}, headers=hdr)
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = client.post("/api/paper-trading/account/settings", json={"initial_capital": 600000.0}, headers=hdr)
    assert r.status_code == 200 and r.json()["initial_capital"] == 600000.0

    s = Session()
    acc1 = s.get(M.PaperTradingAccount, a1)
    assert acc1.enabled in (True, 1), "租户1账户不应被租户2操作影响"
    assert acc1.initial_capital == 1000000.0
    s.close()


def test_pt_notify_settings_tenant_scoped(mt_env, monkeypatch):
    """多租户：模拟盘通知设置应租户级隔离（tenant_settings），不污染共享行（D4 已修复）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    hdr2 = _hdr(client, "u2")

    r = client.post("/api/paper-trading/notify-settings", json={"pt_notify_enabled": "true"}, headers=hdr2)
    assert r.status_code == 200, r.text

    # 期望：写入落 tenant_settings(2, ...)，共享 app_settings 无此行
    s = Session()
    shared = s.query(M.AppSettings).filter_by(key="pt_notify_enabled").one_or_none()
    assert shared is None, f"共享 app_settings 被租户2写入污染: {shared.value if shared else None}"
    ts = s.query(M.TenantSettings).filter_by(tenant_id=2, key="pt_notify_enabled").one_or_none()
    assert ts is not None and ts.value == "true"
    s.close()

    # 且租户1 读面不受租户2 设置影响
    body1 = client.get("/api/paper-trading/notify-settings", headers=_hdr(client, "admin")).json()
    assert body1["settings"]["pt_notify_enabled"] == "false"


def test_pt_notify_settings_channels_isolated(mt_env, monkeypatch):
    """多租户：通知设置页的可用渠道列表只出本租户渠道。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    s = Session()
    s.add_all([
        M.NotifyChannel(tenant_id=1, name="T1渠道", type="webhook", config={"url": "http://t1"}, enabled=True),
        M.NotifyChannel(tenant_id=2, name="T2渠道", type="webhook", config={"url": "http://t2"}, enabled=True),
    ])
    s.commit()
    s.close()

    body = client.get("/api/paper-trading/notify-settings", headers=_hdr(client, "u2")).json()
    assert [c["name"] for c in body["channels"]] == ["T2渠道"]


def test_single_tenant_pt_passthrough(mt_env, monkeypatch):
    """单租户直通：u2 见租户1账户与全部持仓（等价改造前单用户）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    client, Session = mt_env
    _std_users(Session)
    a1 = _seed_pt_account(Session, 1, 1000000.0)
    s = Session()
    s.add(M.PaperTradingPosition(tenant_id=1, account_id=a1, stock_symbol="600000", entry_price=10.0, status="open"))
    s.commit()
    s.close()

    hdr = _hdr(client, "u2")
    assert client.get("/api/paper-trading/account", headers=hdr).json()["id"] == a1
    assert len(client.get("/api/paper-trading/positions?status=all", headers=hdr).json()) == 1


# ---------------------------------------------------------------------------
# 3. settings：实例级/租户级键权限三分（T20）
# ---------------------------------------------------------------------------


def test_settings_instance_key_forbidden_for_non_admin(mt_env, monkeypatch):
    """多租户：实例级键普通用户写 → 403，行不变。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)

    for key in ("panwatch_base_url", "http_proxy", "jwt_secret"):
        r = client.put(f"/api/settings/{key}", json={"value": "x"}, headers=_hdr(client, "u2"))
        assert r.status_code == 403, f"{key} 应拒普通用户"
    s = Session()
    assert s.query(M.AppSettings).filter(M.AppSettings.key.in_(["panwatch_base_url", "jwt_secret"])).count() == 0
    s.close()


def test_settings_instance_key_admin_ok(mt_env, monkeypatch):
    """多租户：实例级键管理员可写。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)

    r = client.put(
        "/api/settings/panwatch_base_url",
        json={"value": "https://panwatch.example.com"},
        headers=_hdr(client, "admin"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "https://panwatch.example.com"


def test_settings_unknown_key_400(mt_env, monkeypatch):
    """多租户：白名单外键一律 400（含管理员）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)

    assert client.put("/api/settings/evil_key", json={"value": "x"}, headers=_hdr(client, "admin")).status_code == 400
    assert client.put("/api/settings/evil_key", json={"value": "x"}, headers=_hdr(client, "u2")).status_code == 400


def test_settings_tenant_key_allowed_for_user(mt_env, monkeypatch):
    """多租户：租户级键普通用户可写（写面归属问题见 D3 xfail）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)

    r = client.put("/api/settings/notify_retry_attempts", json={"value": "5"}, headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text


def test_settings_tenant_key_not_shared_across_tenants(mt_env, monkeypatch):
    """多租户：租户级键写入应落 tenant_settings，共享读面不被他租户污染（D3 已修复）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)

    r = client.put("/api/settings/notify_quiet_hours", json={"value": "22:00-07:00"}, headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text

    s = Session()
    shared = s.query(M.AppSettings).filter_by(key="notify_quiet_hours").one_or_none()
    assert shared is None or shared.value != "22:00-07:00", (
        f"共享 app_settings 被租户2污染: {shared.value if shared else None}"
    )
    s.close()

    body1 = client.get("/api/settings", headers=_hdr(client, "admin")).json()
    v1 = next(x["value"] for x in body1 if x["key"] == "notify_quiet_hours")
    assert v1 != "22:00-07:00", "租户1 不应看到租户2 的租户级设置"


def test_single_tenant_settings_passthrough(mt_env, monkeypatch):
    """单租户直通：已认证用户视同 admin，实例级键可写（等价改造前）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    client, Session = mt_env
    _std_users(Session)

    r = client.put("/api/settings/panwatch_base_url", json={"value": "https://x"}, headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 4. logs：运行日志查询/清空
# ---------------------------------------------------------------------------


def _seed_logs(Session) -> tuple[list[int], list[int]]:
    """租户1 三条（含1条ERROR）、租户2 两条（含1条ERROR），返回 (t1_ids, t2_ids)。"""
    s = Session()
    rows = [
        M.LogEntry(tenant_id=1, timestamp=NOW, level="INFO", logger_name="agent", message="T1日志甲", agent_name="daily_report"),
        M.LogEntry(tenant_id=1, timestamp=NOW, level="ERROR", logger_name="agent", message="T1错误", agent_name="daily_report"),
        M.LogEntry(tenant_id=1, timestamp=NOW, level="INFO", logger_name="httpx", message="T1基础设施"),
        M.LogEntry(tenant_id=2, timestamp=NOW, level="INFO", logger_name="agent", message="T2日志甲", agent_name="daily_report"),
        M.LogEntry(tenant_id=2, timestamp=NOW, level="ERROR", logger_name="agent", message="T2错误", agent_name="daily_report"),
    ]
    s.add_all(rows)
    s.commit()
    ids = [r.id for r in rows]
    s.close()
    return ids[:3], ids[3:]


def test_logs_list_isolated(mt_env, monkeypatch):
    """多租户：日志列表/搜索只见本租户行。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    t1_ids, t2_ids = _seed_logs(Session)
    hdr = _hdr(client, "u2")

    body = client.get("/api/logs", headers=hdr).json()
    got_ids = [x["id"] for x in body["items"]]
    assert set(got_ids) == set(t2_ids)
    assert not set(got_ids) & set(t1_ids)

    # 关键词搜索不越界
    body = client.get("/api/logs", params={"q": "T1"}, headers=hdr).json()
    assert body["items"] == []


def test_logs_total_count_not_leaked(mt_env, monkeypatch):
    """多租户：logs total/meta.total 应只计本租户（D5 已修复：count 前显式 tenant 谓词）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    _seed_logs(Session)
    hdr = _hdr(client, "u2")

    body = client.get("/api/logs", headers=hdr).json()
    assert body["total"] == 2, f"list total 泄漏: {body['total']}"
    meta = client.get("/api/logs/meta", headers=hdr).json()
    assert meta["total"] == 2, f"meta total 泄漏: {meta['total']}"


def test_logs_meta_isolated(mt_env, monkeypatch):
    """多租户：meta 聚合只统计本租户。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    _seed_logs(Session)

    body = client.get("/api/logs/meta", headers=_hdr(client, "u2")).json()
    assert body["levels"] == {"INFO": 1, "ERROR": 1}
    assert all("T1" not in (t.get("logger_name") or "") for t in body["top_loggers"])


def test_logs_clear_non_admin_403(mt_env, monkeypatch):
    """多租户：普通用户清空日志 403，行不动。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    _seed_logs(Session)

    assert client.delete("/api/logs", headers=_hdr(client, "u2")).status_code == 403
    s = Session()
    assert s.query(M.LogEntry).count() == 5
    s.close()


def test_logs_clear_admin_scoped_to_own_tenant(mt_env, monkeypatch):
    """多租户：管理员清空只删本租户行（bulk DELETE 机制点注入租户谓词）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    _, t2_ids = _seed_logs(Session)

    r = client.delete("/api/logs", headers=_hdr(client, "admin"))
    assert r.status_code == 200 and r.json()["deleted"] == 3
    s = Session()
    remaining = [r_.id for r_ in s.query(M.LogEntry).all()]
    assert sorted(remaining) == sorted(t2_ids), "租户2 日志不应被管理员清空波及"
    s.close()


def test_single_tenant_logs_passthrough(mt_env, monkeypatch):
    """单租户直通：u2 见全量日志且可清空（等价改造前）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    client, Session = mt_env
    _std_users(Session)
    _seed_logs(Session)

    hdr = _hdr(client, "u2")
    assert client.get("/api/logs", headers=hdr).json()["total"] == 5
    assert client.delete("/api/logs", headers=hdr).json()["deleted"] == 5


# ---------------------------------------------------------------------------
# 5. history：分析历史
# ---------------------------------------------------------------------------


def _seed_history(Session, tenant_id: int, symbol: str, title: str, date: str = TODAY) -> int:
    s = Session()
    row = M.AnalysisHistory(
        tenant_id=tenant_id, agent_name="daily_report", stock_symbol=symbol,
        analysis_date=date, title=title, content=f"{title}内容",
        raw_data={}, agent_kind_snapshot="workflow",
    )
    s.add(row)
    s.commit()
    hid = row.id
    s.close()
    return hid


def test_history_list_isolated(mt_env, monkeypatch):
    """多租户：历史列表只出本租户行。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    _seed_history(Session, 1, "600000", "T1复盘")
    h2 = _seed_history(Session, 2, "600519", "T2复盘")

    body = client.get("/api/history", headers=_hdr(client, "u2")).json()
    assert [x["id"] for x in body] == [h2]
    assert body[0]["title"] == "T2复盘"


def test_history_detail_cross_tenant_404(mt_env, monkeypatch):
    """多租户：持他租户历史 id 取详情 → 404。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    h1 = _seed_history(Session, 1, "600000", "T1复盘")
    h2 = _seed_history(Session, 2, "600519", "T2复盘")
    hdr = _hdr(client, "u2")

    assert client.get(f"/api/history/{h1}", headers=hdr).status_code == 404
    assert client.get(f"/api/history/{h2}", headers=hdr).status_code == 200


def test_history_delete_cross_tenant_404(mt_env, monkeypatch):
    """多租户：越权删他租户历史 → 404 且行保留；本租户可删。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    h1 = _seed_history(Session, 1, "600000", "T1复盘")
    h2 = _seed_history(Session, 2, "600519", "T2复盘")
    hdr = _hdr(client, "u2")

    assert client.delete(f"/api/history/{h1}", headers=hdr).status_code == 404
    s = Session()
    assert s.get(M.AnalysisHistory, h1) is not None
    s.close()
    assert client.delete(f"/api/history/{h2}", headers=hdr).status_code == 200


# ---------------------------------------------------------------------------
# 6. context：上下文快照/主题/运行/后验（context_store 显式 tenant 谓词）
# ---------------------------------------------------------------------------


def test_context_snapshots_isolated(mt_env, monkeypatch):
    """多租户：同 symbol 的上下文快照按租户隔离。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    s = Session()
    s.add_all([
        M.StockContextSnapshot(tenant_id=1, symbol="600519", market="CN", snapshot_date=TODAY, context_type="daily_report", payload={"who": "T1"}),
        M.StockContextSnapshot(tenant_id=2, symbol="600519", market="CN", snapshot_date=TODAY, context_type="daily_report", payload={"who": "T2"}),
    ])
    s.commit()
    s.close()

    body = client.get("/api/context/snapshots/600519", headers=_hdr(client, "u2")).json()
    assert len(body) == 1
    assert body[0]["payload"] == {"who": "T2"}


def test_context_topics_latest_isolated(mt_env, monkeypatch):
    """多租户：最新主题快照取本租户最新，不取他租户更新的行。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    s = Session()
    s.add_all([
        M.NewsTopicSnapshot(tenant_id=1, snapshot_date=TODAY, window_days=7, summary="T1今日主题"),
        M.NewsTopicSnapshot(tenant_id=2, snapshot_date=yesterday, window_days=7, summary="T2昨日主题"),
    ])
    s.commit()
    s.close()

    body = client.get("/api/context/topics/latest", headers=_hdr(client, "u2")).json()
    assert body["exists"] is True
    assert body["summary"] == "T2昨日主题"
    assert body["snapshot_date"] == yesterday


def test_context_runs_isolated(mt_env, monkeypatch):
    """多租户：agent 上下文运行记录按租户隔离。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    s = Session()
    s.add_all([
        M.AgentContextRun(tenant_id=1, agent_name="daily_report", stock_symbol="*", analysis_date=TODAY, context_payload={"who": "T1"}),
        M.AgentContextRun(tenant_id=2, agent_name="daily_report", stock_symbol="*", analysis_date=TODAY, context_payload={"who": "T2"}),
    ])
    s.commit()
    s.close()

    body = client.get("/api/context/runs", params={"agent_name": "daily_report"}, headers=_hdr(client, "u2")).json()
    assert len(body) == 1
    assert body[0]["context_payload"] == {"who": "T2"}


def test_context_predictions_isolated(mt_env, monkeypatch):
    """多租户：后验评估记录按租户隔离。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    s = Session()
    s.add_all([
        M.AgentPredictionOutcome(tenant_id=1, agent_name="daily_report", stock_symbol="600000", prediction_date=TODAY, action="buy", action_label="建仓"),
        M.AgentPredictionOutcome(tenant_id=2, agent_name="daily_report", stock_symbol="600519", prediction_date=TODAY, action="sell", action_label="清仓"),
    ])
    s.commit()
    s.close()

    body = client.get("/api/context/predictions", headers=_hdr(client, "u2")).json()
    assert len(body) == 1
    assert body[0]["stock_symbol"] == "600519"


def test_context_cleanup_scoped_to_current_tenant(mt_env, monkeypatch):
    """多租户：cleanup 虽为全局函数签名，web 调用经机制点只清本租户旧行。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    s = Session()
    s.add_all([
        M.StockContextSnapshot(tenant_id=1, symbol="600000", market="CN", snapshot_date=OLD_DATE, context_type="daily_report"),
        M.StockContextSnapshot(tenant_id=2, symbol="600519", market="CN", snapshot_date=OLD_DATE, context_type="daily_report"),
        M.StockContextSnapshot(tenant_id=2, symbol="600519", market="CN", snapshot_date=TODAY, context_type="daily_report"),
    ])
    s.commit()
    s.close()

    r = client.post("/api/context/cleanup", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    assert r.json()["stock_context_snapshots"] == 1, "只应清掉租户2的旧行"

    s = Session()
    remaining = s.query(M.StockContextSnapshot).all()
    assert {(r_.tenant_id, r_.snapshot_date) for r_ in remaining} == {(1, OLD_DATE), (2, TODAY)}
    s.close()


# ---------------------------------------------------------------------------
# 7. dashboard：首页聚合 / 简报
# ---------------------------------------------------------------------------


def test_dashboard_brief_isolated(mt_env, monkeypatch):
    """多租户：盘后简报取本租户最新报告，不取他租户更新的行。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    _seed_history(Session, 1, "*", "T1今日复盘", date=TODAY)
    _seed_history(Session, 2, "*", "T2昨日复盘", date=yesterday)

    body = client.get("/api/dashboard/brief", params={"type": "eod"}, headers=_hdr(client, "u2")).json()
    assert body["title"] == "T2昨日复盘"
    assert body["date"] == yesterday


def test_dashboard_overview_tenant_scoped(mt_env, monkeypatch):
    """多租户：overview 的自选股/持仓/资金/错误计数/洞察/主题全部按租户口径。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)

    # 市场级信号链（strategy_engine 直连 SessionLocal）与本断言无关，打桩置空
    monkeypatch.setattr(dashboard, "get_strategy_stats", lambda days=45: {"coverage": {}, "by_strategy": []})
    monkeypatch.setattr(dashboard, "list_strategy_signals", lambda **kw: {"items": []})

    # 租户1：2 自选股 + 1 账户(9999) + 1 持仓 + 3 条今日ERROR + 今日主题 + 今日盘前报告
    s = Session()
    st1a = M.Stock(tenant_id=1, symbol="600000", name="浦发", market="CN")
    st1b = M.Stock(tenant_id=1, symbol="000001", name="平安", market="CN")
    acc1 = M.Account(tenant_id=1, name="T1账户", available_funds=9999.0, enabled=True)
    s.add_all([st1a, st1b, acc1])
    s.flush()
    s.add(M.Position(tenant_id=1, account_id=acc1.id, stock_id=st1a.id, cost_price=10.0, quantity=100))
    for i in range(3):
        s.add(M.LogEntry(tenant_id=1, timestamp=NOW, level="ERROR", logger_name="agent", message=f"T1错误{i}"))
    s.add(M.NewsTopicSnapshot(tenant_id=1, snapshot_date=TODAY, window_days=7, topics=[{"topic": "T1话题", "score": 9}]))
    s.add(M.AnalysisHistory(tenant_id=1, agent_name="premarket_outlook", stock_symbol="*", analysis_date=TODAY, title="T1盘前", content="c", agent_kind_snapshot="workflow"))
    # 租户2：1 自选股 + 1 账户(5000) + 1 条今日ERROR + 今日主题 + 今日盘前报告
    st2 = M.Stock(tenant_id=2, symbol="600519", name="茅台", market="CN")
    acc2 = M.Account(tenant_id=2, name="T2账户", available_funds=5000.0, enabled=True)
    s.add_all([st2, acc2])
    s.add(M.LogEntry(tenant_id=2, timestamp=NOW, level="ERROR", logger_name="agent", message="T2错误"))
    s.add(M.NewsTopicSnapshot(tenant_id=2, snapshot_date=TODAY, window_days=7, topics=[{"topic": "T2话题", "score": 8}]))
    s.add(M.AnalysisHistory(tenant_id=2, agent_name="premarket_outlook", stock_symbol="*", analysis_date=TODAY, title="T2盘前", content="c", agent_kind_snapshot="workflow"))
    s.commit()
    s.close()

    body = client.get("/api/dashboard/overview", headers=_hdr(client, "u2")).json()
    k = body["kpis"]
    assert k["watchlist_count"] == 1
    assert k["positions_count"] == 0
    assert k["available_funds"] == 5000.0
    assert k["errors_24h"] == 1
    assert [t["name"] for t in body["market_pulse"]["hot_topics"]] == ["T2话题"]
    assert [i["title"] for i in body["insights"]] == ["T2盘前"]

    body1 = client.get("/api/dashboard/overview", headers=_hdr(client, "admin")).json()
    k1 = body1["kpis"]
    assert k1["watchlist_count"] == 2
    assert k1["positions_count"] == 1
    assert k1["available_funds"] == 9999.0
    assert k1["errors_24h"] == 3
    assert [t["name"] for t in body1["market_pulse"]["hot_topics"]] == ["T1话题"]


# ---------------------------------------------------------------------------
# 8. insights：批量洞察（建议池租户隔离）
# ---------------------------------------------------------------------------


class _FakeKlineCollector:
    def __init__(self, market):
        self.market = market

    def get_kline_summary(self, symbol):
        return {}


def _fake_quote_rows(symbols, market):
    return [{"symbol": s, "name": s, "current_price": 1.0, "change_pct": 0.0} for s in symbols]


def test_insights_batch_suggestion_isolated(mt_env, monkeypatch):
    """多租户：/insights/batch 的建议池只回本租户建议；无建议租户返回 None。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _std_users(Session)
    monkeypatch.setattr(insights, "md_quote_rows", _fake_quote_rows)
    monkeypatch.setattr(insights, "KlineCollector", _FakeKlineCollector)

    s = Session()
    s.add_all([
        M.StockSuggestion(tenant_id=1, stock_symbol="600519", stock_market="CN", action="buy", action_label="建仓", reason="T1理由", agent_name="daily_report"),
        M.StockSuggestion(tenant_id=2, stock_symbol="600519", stock_market="CN", action="sell", action_label="清仓", reason="T2理由", agent_name="daily_report"),
    ])
    s.commit()
    s.close()

    payload = {"items": [{"symbol": "600519", "market": "CN"}]}
    r2 = client.post("/api/insights/batch", json=payload, headers=_hdr(client, "u2"))
    assert r2.status_code == 200, r2.text
    sug2 = r2.json()[0]["suggestion"]
    assert sug2 is not None and sug2["reason"] == "T2理由"

    r3 = client.post("/api/insights/batch", json=payload, headers=_hdr(client, "u3"))
    assert r3.status_code == 200, r3.text
    assert r3.json()[0]["suggestion"] is None, "租户3 不应看到其他租户的建议"
