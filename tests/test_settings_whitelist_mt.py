"""MT-P1 设置写入白名单测试：未知 key 400、实例级仅管理员、租户级普通可写。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401
from src.web.api import settings as settings_api
from src.web.database import Base, get_db


@pytest.fixture
def env():
    """内存库 + 注入假用户的 settings app；fake_user.role 可由用例改写。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    fake_user = SimpleNamespace(role="user")

    app = FastAPI()
    app.include_router(settings_api.router, prefix="/api/settings")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[settings_api.get_current_user] = lambda: fake_user
    return TestClient(app), Session, fake_user


def test_unknown_key_400(env):
    """未知配置项一律 400。"""
    client = env[0]
    r = client.put("/api/settings/definitely_unknown", json={"value": "x"})
    assert r.status_code == 400


def test_single_tenant_instance_key_writable(env, monkeypatch):
    """单租户模式（默认）：实例级 key 放行（视为管理员等价单用户）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    client, Session, fake_user = env
    fake_user.role = "user"  # 角色在单租户直通下被忽略

    r = client.put("/api/settings/jwt_secret", json={"value": "new-secret"})
    assert r.status_code == 200, r.text

    s = Session()
    row = s.query(M.AppSettings).filter_by(key="jwt_secret").one()
    assert row.value == "new-secret"
    s.close()


def test_multi_tenant_non_admin_instance_key_403(env, monkeypatch):
    """多租户模式：非管理员写实例级 key 403。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, _, fake_user = env
    fake_user.role = "user"

    r = client.put("/api/settings/jwt_secret", json={"value": "new-secret"})
    assert r.status_code == 403

    r2 = client.put("/api/settings/panwatch_base_url", json={"value": "https://x"})
    assert r2.status_code == 403


def test_multi_tenant_admin_instance_key_ok(env, monkeypatch):
    """多租户模式：管理员可写实例级 key。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session, fake_user = env
    fake_user.role = "admin"

    r = client.put("/api/settings/panwatch_base_url", json={"value": "https://x"})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "https://x"


def test_multi_tenant_tenant_level_key_writable_by_user(env, monkeypatch):
    """多租户模式：租户级 key 普通用户可写。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, _, fake_user = env
    fake_user.role = "user"

    r = client.put("/api/settings/stock_link_platform", json={"value": "futunn"})
    assert r.status_code == 200, r.text

    r2 = client.put("/api/settings/notify_quiet_hours", json={"value": "22:00-08:00"})
    assert r2.status_code == 200, r.text


def test_unknown_key_400_even_for_admin(env, monkeypatch):
    """白名单外 key 即使管理员也 400。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, _, fake_user = env
    fake_user.role = "admin"
    r = client.put("/api/settings/evil_key", json={"value": "x"})
    assert r.status_code == 400
