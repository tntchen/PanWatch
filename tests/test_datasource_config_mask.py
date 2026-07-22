"""D5 数据源 config 凭证掩码测试（docs/19 D5 / docs/27 §行31）。

覆盖：
1. 多租户普通用户（role != admin）GET list/detail 的 config 掩码为 {}；
2. 多租户 admin 用户 list/detail config 明文不变；
3. PANWATCH_SINGLE_TENANT='1' 单租户直通：config 明文，与改造前行为等价
   （含普通登录用户，单租户唯一用户即 admin 的场景由等价语义覆盖）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M
import src.web.tenant_context as tc
from src.web.api import auth, datasources
from src.web.api.auth import get_current_user
from src.web.database import Base, get_db

SECRET = "d5-datasource-mask-secret-0123456789"
OLD_PWD_AT = datetime(2020, 1, 1, 0, 0, 0)

SECRET_CONFIG = {"token": "ts-secret-token-abc123", "timeout": 30}


class _FakeMarketData:
    """list 端点健康快照替身（不触碰真实 marketdata 引擎）。"""

    def health(self) -> dict:
        return {}


@pytest.fixture
def mt_env(monkeypatch):
    """内存库 + do_orm_execute 事件 + 固定 JWT secret + auth/datasources app。"""
    monkeypatch.setattr(auth, "_jwt_secret", SECRET)
    monkeypatch.setattr(auth, "ENV_AUTH_USERNAME", None)
    monkeypatch.setattr(auth, "ENV_AUTH_PASSWORD", None)
    monkeypatch.setattr(
        "src.core.marketdata_client.get_market_data", lambda: _FakeMarketData()
    )

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

    app = FastAPI(redirect_slashes=False)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(
        datasources.router,
        prefix="/api/datasources",
        dependencies=[Depends(get_current_user)],
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


def _mkuser(
    Session,
    username: str,
    *,
    tenant_id: int,
    role: str = "user",
) -> None:
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


def _hdr(client: TestClient, username: str) -> dict:
    r = client.post(
        "/api/auth/login", json={"username": username, "password": "secret123"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _seed_source(Session) -> int:
    """种一条含 token 类凭证的数据源（data_sources 为实例级共享表），返回 id。"""
    s = Session()
    row = M.DataSource(
        name="Tushare K线",
        type="kline",
        provider="tushare",
        config=dict(SECRET_CONFIG),
        enabled=True,
        priority=0,
        supports_batch=False,
        test_symbols=[],
    )
    s.add(row)
    s.commit()
    row_id = row.id
    s.close()
    return row_id


# ---------------------------------------------------------------------------
# 1. 多租户：普通用户 list/detail config 掩码为 {}
# ---------------------------------------------------------------------------


def test_mt_non_admin_list_config_masked(mt_env, monkeypatch):
    """多租户：普通用户 GET /api/datasources 的 config 掩码为 {}，其余字段保留。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin")
    _mkuser(Session, "u2", tenant_id=2)
    src_id = _seed_source(Session)

    r = client.get("/api/datasources", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    row = body[0]
    assert row["id"] == src_id
    assert row["config"] == {}, "非 admin 用户 config 必须掩码为空 dict"
    assert row["name"] == "Tushare K线"
    assert row["provider"] == "tushare"


def test_mt_non_admin_detail_config_masked(mt_env, monkeypatch):
    """多租户：普通用户 GET /api/datasources/{id} 的 config 掩码为 {}。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin")
    _mkuser(Session, "u2", tenant_id=2)
    src_id = _seed_source(Session)

    r = client.get(f"/api/datasources/{src_id}", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    assert r.json()["config"] == {}, "非 admin 用户 detail config 必须掩码为空 dict"

    # 库内凭证不受影响（掩码仅作用于出网响应）
    s = Session()
    assert s.get(M.DataSource, src_id).config == SECRET_CONFIG
    s.close()


# ---------------------------------------------------------------------------
# 2. 多租户：admin 明文不变
# ---------------------------------------------------------------------------


def test_mt_admin_sees_plaintext_config(mt_env, monkeypatch):
    """多租户：admin 用户 list/detail config 明文不变。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin")
    src_id = _seed_source(Session)
    hdr = _hdr(client, "admin")

    r = client.get("/api/datasources", headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()[0]["config"] == SECRET_CONFIG

    r2 = client.get(f"/api/datasources/{src_id}", headers=hdr)
    assert r2.status_code == 200, r2.text
    assert r2.json()["config"] == SECRET_CONFIG


# ---------------------------------------------------------------------------
# 3. 单租户直通：明文等价现状
# ---------------------------------------------------------------------------


def test_single_tenant_passthrough_plaintext(mt_env, monkeypatch):
    """单租户直通：config 明文，admin 与普通登录用户均与改造前行为等价。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin")
    _mkuser(Session, "u2", tenant_id=2)
    src_id = _seed_source(Session)

    for username in ("admin", "u2"):
        hdr = _hdr(client, username)
        r = client.get("/api/datasources", headers=hdr)
        assert r.status_code == 200, r.text
        assert r.json()[0]["config"] == SECRET_CONFIG

        r2 = client.get(f"/api/datasources/{src_id}", headers=hdr)
        assert r2.status_code == 200, r2.text
        assert r2.json()["config"] == SECRET_CONFIG
