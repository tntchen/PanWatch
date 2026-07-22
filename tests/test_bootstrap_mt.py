"""MT-P1 bootstrap 测试：默认租户 + 初始管理员的幂等初始化与凭证优先级。"""

from __future__ import annotations

import hashlib
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import bootstrap
from src.web import models as M  # noqa: F401
from src.web.api import auth
from src.web.database import Base


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def test_creates_default_tenant_and_admin_from_env(db, monkeypatch):
    """env 凭证优先：创建默认租户 id=1 + bcrypt 初始管理员。"""
    monkeypatch.setenv("AUTH_USERNAME", "boss")
    monkeypatch.setenv("AUTH_PASSWORD", "topsecret1")

    bootstrap.ensure_default_tenant_and_admin(db)

    tenant = db.get(M.Tenant, 1)
    assert tenant is not None and tenant.is_default

    user = db.query(M.User).filter_by(username="boss").one()
    assert user.role == "admin"
    assert user.tenant_id == 1
    assert user.password_algo == "bcrypt"
    assert user.password_hash.startswith("$2")
    assert auth.verify_password("topsecret1", user.password_hash)
    assert user.is_active


def test_idempotent_double_call(db, monkeypatch):
    """连续调用两次不产生重复租户/用户。"""
    monkeypatch.setenv("AUTH_USERNAME", "boss")
    monkeypatch.setenv("AUTH_PASSWORD", "topsecret1")

    bootstrap.ensure_default_tenant_and_admin(db)
    bootstrap.ensure_default_tenant_and_admin(db)

    assert db.query(M.Tenant).count() == 1
    assert db.query(M.User).count() == 1


def test_skip_admin_creation_when_users_exist(db, monkeypatch):
    """users 非空时跳过管理员创建（不覆盖已有账号），但仍补齐默认租户。"""
    monkeypatch.setenv("AUTH_USERNAME", "boss")
    monkeypatch.setenv("AUTH_PASSWORD", "topsecret1")

    db.add(M.Tenant(id=1, name="默认租户", is_default=True))
    db.add(
        M.User(
            tenant_id=1,
            username="existing",
            password_hash=auth.hash_password("secret123"),
            role="user",
        )
    )
    db.commit()

    bootstrap.ensure_default_tenant_and_admin(db)

    assert db.query(M.User).count() == 1
    assert db.query(M.User).filter_by(username="boss").first() is None


def test_legacy_settings_credentials_migrated_as_sha256(db, monkeypatch):
    """无 env 凭证时搬入 app_settings 旧凭据并标记 sha256_legacy。"""
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    legacy_hash = hashlib.sha256("pw123456".encode("utf-8")).hexdigest()
    db.add(M.AppSettings(key="auth_username", value="legacy_admin"))
    db.add(M.AppSettings(key="auth_password_hash", value=legacy_hash))
    db.commit()

    bootstrap.ensure_default_tenant_and_admin(db)

    user = db.query(M.User).filter_by(username="legacy_admin").one()
    assert user.password_algo == "sha256_legacy"
    assert user.password_hash == legacy_hash
    # 旧格式哈希仍可被登录链路校验（verify_password 兼容 sha256_legacy）
    assert auth.verify_password("pw123456", user.password_hash)


def test_random_password_when_no_credentials(db, monkeypatch, caplog):
    """无任何凭证时生成随机密码的初始管理员并告警一次。"""
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)

    with caplog.at_level(logging.WARNING, logger="src.web.bootstrap"):
        bootstrap.ensure_default_tenant_and_admin(db)

    user = db.query(M.User).filter_by(username="admin").one()
    assert user.role == "admin"
    assert user.password_algo == "bcrypt"
    assert any("随机初始密码" in r.getMessage() for r in caplog.records)


def test_tenant_created_even_when_user_exists_without_tenant(db, monkeypatch):
    """users 非空但默认租户缺失时仍补建默认租户。"""
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    db.add(M.Tenant(id=9, name="其他租户"))
    db.add(
        M.User(
            tenant_id=9,
            username="existing",
            password_hash=auth.hash_password("secret123"),
            role="admin",
        )
    )
    db.commit()

    bootstrap.ensure_default_tenant_and_admin(db)

    assert db.get(M.Tenant, 1) is not None
    assert db.query(M.User).count() == 1
