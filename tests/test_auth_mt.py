"""MT-P1 认证体系测试：JWT 生命周期、bcrypt/legacy 透明重哈希、改密吊销、setup、用户管理守卫。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401  注册所有 ORM 模型到 Base
from src.web.api import auth
from src.web.database import Base, get_db

SECRET = "mt-test-secret-0123456789abcdef0123"
OLD_PWD_AT = datetime(2020, 1, 1, 0, 0, 0)  # 远古时间戳，避免与改密同秒导致 pwd_at 相等


def _sha256(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


@pytest.fixture
def env(monkeypatch):
    """内存库 + 固定 JWT secret + 关闭 env 凭证引导的独立 auth app。"""
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

    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app), Session


def _mkuser(
    Session,
    username: str,
    password: str,
    *,
    role: str = "user",
    tenant_id: int = 1,
    algo: str = "bcrypt",
    pwd_at: datetime | None = OLD_PWD_AT,
    is_active: bool = True,
) -> int:
    s = Session()
    if s.get(M.Tenant, tenant_id) is None:
        s.add(
            M.Tenant(id=tenant_id, name=f"租户{tenant_id}", is_default=(tenant_id == 1))
        )
    ph = auth.hash_password(password) if algo == "bcrypt" else _sha256(password)
    u = M.User(
        tenant_id=tenant_id,
        username=username,
        password_hash=ph,
        password_algo=algo,
        role=role,
        is_active=is_active,
        pwd_changed_at=pwd_at,
    )
    s.add(u)
    s.commit()
    s.refresh(u)
    uid = u.id
    s.close()
    return uid


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _craft(**overrides) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "1",
        "tenant_id": 1,
        "role": "admin",
        "pwd_at": 0,
        "iat": now,
        "exp": now + timedelta(days=1),
    }
    payload.update(overrides)
    return jwt.encode(payload, SECRET, algorithm="HS256")


# ---------------------------------------------------------------------------
# JWT 生命周期
# ---------------------------------------------------------------------------


def test_login_success_claims_and_me(env):
    """登录成功：token 含全部必填 claims，/me 返回身份。"""
    uid = _mkuser(env[1], "admin", "secret123", role="admin")
    client = env[0]

    r = _login(client, "admin", "secret123")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == "admin"
    assert body["user"]["tenant_id"] == 1

    claims = jwt.decode(body["token"], SECRET, algorithms=["HS256"])
    for key in ("sub", "tenant_id", "role", "pwd_at", "iat", "exp"):
        assert key in claims, f"缺少 claim: {key}"
    assert claims["sub"] == str(uid)
    assert claims["role"] == "admin"

    me = client.get("/api/auth/me", headers=_hdr(body["token"]))
    assert me.status_code == 200, me.text
    assert me.json()["username"] == "admin"
    assert me.json()["role"] == "admin"


def test_missing_required_claim_token_401(env):
    """缺必填 claim 的旧格式 token 一律 401。"""
    _mkuser(env[1], "admin", "secret123", role="admin")
    # 旧格式 token：无 pwd_at / tenant_id
    now = datetime.now(timezone.utc)
    legacy = jwt.encode(
        {"sub": "1", "iat": now, "exp": now + timedelta(days=1)},
        SECRET,
        algorithm="HS256",
    )
    r = env[0].get("/api/auth/me", headers=_hdr(legacy))
    assert r.status_code == 401


def test_tampered_pwd_at_token_401(env):
    """pwd_at 与库中 pwd_changed_at 不一致的 token 401。"""
    _mkuser(env[1], "admin", "secret123", role="admin")
    bad = _craft(pwd_at=999999999)
    r = env[0].get("/api/auth/me", headers=_hdr(bad))
    assert r.status_code == 401


def test_expired_token_401(env):
    """过期 token 401。"""
    _mkuser(env[1], "admin", "secret123", role="admin")
    now = datetime.now(timezone.utc)
    expired = _craft(iat=now - timedelta(days=40), exp=now - timedelta(hours=1))
    r = env[0].get("/api/auth/me", headers=_hdr(expired))
    assert r.status_code == 401


def test_no_token_401(env):
    """不带凭证访问受保护端点 401。"""
    assert env[0].get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# 密码哈希：bcrypt + sha256_legacy 透明重哈希
# ---------------------------------------------------------------------------


def test_bcrypt_login_and_hash_prefix(env):
    """bcrypt 用户登录成功，库中哈希为 $2 前缀。"""
    _mkuser(env[1], "u1", "secret123")
    r = _login(env[0], "u1", "secret123")
    assert r.status_code == 200, r.text

    s = env[1]()
    u = s.query(M.User).filter_by(username="u1").one()
    assert u.password_hash.startswith("$2")
    s.close()


def test_sha256_legacy_transparent_rehash(env):
    """sha256_legacy 用户登录成功且被透明重哈希为 bcrypt，pwd_changed_at 更新。"""
    _mkuser(env[1], "legacy", "secret123", algo="sha256_legacy")
    r = _login(env[0], "legacy", "secret123")
    assert r.status_code == 200, r.text

    s = env[1]()
    u = s.query(M.User).filter_by(username="legacy").one()
    assert u.password_hash.startswith("$2"), "旧 SHA-256 应被透明重哈希为 bcrypt"
    assert u.pwd_changed_at is not None and u.pwd_changed_at > OLD_PWD_AT
    s.close()


def test_wrong_password_401(env):
    """错误密码 401。"""
    _mkuser(env[1], "u1", "secret123")
    r = _login(env[0], "u1", "wrong-pass")
    assert r.status_code == 401


def test_unknown_username_401(env):
    """不存在的用户名 401。"""
    r = _login(env[0], "ghost", "secret123")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# change-password
# ---------------------------------------------------------------------------


def test_change_password_wrong_old_400(env):
    """旧密码错误 400。"""
    _mkuser(env[1], "admin", "secret123", role="admin")
    token = _login(env[0], "admin", "secret123").json()["token"]
    r = env[0].post(
        "/api/auth/change-password",
        json={"old_password": "nope-nope", "new_password": "newpass123"},
        headers=_hdr(token),
    )
    assert r.status_code == 400


def test_change_password_revokes_old_token(env):
    """改密成功后旧 token 立即失效，新密码可登录。"""
    _mkuser(env[1], "admin", "secret123", role="admin")
    client = env[0]
    old_token = _login(client, "admin", "secret123").json()["token"]

    r = client.post(
        "/api/auth/change-password",
        json={"old_password": "secret123", "new_password": "newpass123"},
        headers=_hdr(old_token),
    )
    assert r.status_code == 200, r.text

    # 旧 token 被 pwd_at 吊销
    assert client.get("/api/auth/me", headers=_hdr(old_token)).status_code == 401
    # 旧密码不可登录，新密码可登录
    assert _login(client, "admin", "secret123").status_code == 401
    assert _login(client, "admin", "newpass123").status_code == 200


def test_change_password_too_short_400(env):
    """新密码长度不足 400。"""
    _mkuser(env[1], "admin", "secret123", role="admin")
    token = _login(env[0], "admin", "secret123").json()["token"]
    r = env[0].post(
        "/api/auth/change-password",
        json={"old_password": "secret123", "new_password": "123"},
        headers=_hdr(token),
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def test_setup_empty_users_ok(env):
    """users 空表时 setup 创建初始管理员并返回 token。"""
    client = env[0]
    r = client.post(
        "/api/auth/setup", json={"username": "admin", "password": "secret123"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == "admin"
    assert body["user"]["tenant_id"] == 1
    assert body["token"]

    # token 立即可用
    me = client.get("/api/auth/me", headers=_hdr(body["token"]))
    assert me.status_code == 200


def test_setup_non_empty_users_400(env):
    """users 非空时 setup 拒绝（400）。"""
    _mkuser(env[1], "admin", "secret123", role="admin")
    r = env[0].post(
        "/api/auth/setup", json={"username": "second", "password": "secret123"}
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 用户管理（管理员端点）
# ---------------------------------------------------------------------------


def _admin_token(client, Session) -> str:
    _mkuser(Session, "admin", "secret123", role="admin")
    return _login(client, "admin", "secret123").json()["token"]


def test_create_user_with_new_tenant_multi_tenant(env, monkeypatch):
    """多租户模式：管理员创建用户并新建租户成功。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client = env[0]
    token = _admin_token(client, env[1])

    r = client.post(
        "/api/auth/users",
        json={
            "username": "u2",
            "password": "secret123",
            "role": "user",
            "tenant_name": "租户B",
        },
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text
    u = r.json()["user"]
    assert u["tenant_id"] != 1
    assert u["tenant_name"] == "租户B"
    assert u["role"] == "user"

    # 新用户可登录
    assert _login(client, "u2", "secret123").status_code == 200


def test_create_user_same_tenant_default(env):
    """不传 tenant_name 时新用户归属管理员所在租户。"""
    client = env[0]
    token = _admin_token(client, env[1])
    r = client.post(
        "/api/auth/users",
        json={"username": "u2", "password": "secret123"},
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["tenant_id"] == 1


def test_create_user_non_admin_403(env):
    """非管理员调用用户管理端点 403。"""
    client = env[0]
    _mkuser(env[1], "plain", "secret123", role="user")
    token = _login(client, "plain", "secret123").json()["token"]

    r = client.post(
        "/api/auth/users",
        json={"username": "u9", "password": "secret123"},
        headers=_hdr(token),
    )
    assert r.status_code == 403
    assert client.get("/api/auth/users", headers=_hdr(token)).status_code == 403


def test_create_user_new_tenant_blocked_single_tenant(env, monkeypatch):
    """单租户模式（默认）创建新租户 400。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    client = env[0]
    token = _admin_token(client, env[1])
    r = client.post(
        "/api/auth/users",
        json={"username": "u2", "password": "secret123", "tenant_name": "新租户"},
        headers=_hdr(token),
    )
    assert r.status_code == 400


def test_create_user_duplicate_username_400(env):
    """重复用户名 400。"""
    client = env[0]
    token = _admin_token(client, env[1])
    r = client.post(
        "/api/auth/users",
        json={"username": "admin", "password": "secret123"},
        headers=_hdr(token),
    )
    assert r.status_code == 400


def test_patch_user_deactivate_blocks_login(env):
    """PATCH 停用用户后该用户登录被拒（403）。"""
    client = env[0]
    token = _admin_token(client, env[1])
    uid = _mkuser(env[1], "u2", "secret123")

    r = client.patch(
        f"/api/auth/users/{uid}", json={"is_active": False}, headers=_hdr(token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["is_active"] is False
    assert _login(client, "u2", "secret123").status_code == 403


def test_patch_user_reset_password_revokes_tokens(env):
    """PATCH 重置密码后旧 token 失效、新密码可登录。"""
    client = env[0]
    token = _admin_token(client, env[1])
    uid = _mkuser(env[1], "u2", "secret123")
    old_token = _login(client, "u2", "secret123").json()["token"]

    r = client.patch(
        f"/api/auth/users/{uid}",
        json={"reset_password": "newpass123"},
        headers=_hdr(token),
    )
    assert r.status_code == 200, r.text

    assert client.get("/api/auth/me", headers=_hdr(old_token)).status_code == 401
    assert _login(client, "u2", "secret123").status_code == 401
    assert _login(client, "u2", "newpass123").status_code == 200


def test_patch_self_demote_or_deactivate_guard(env):
    """防自我降级/禁用守卫：管理员不能禁用或降级自己。"""
    client = env[0]
    token = _admin_token(client, env[1])
    me = client.get("/api/auth/me", headers=_hdr(token)).json()

    r1 = client.patch(
        f"/api/auth/users/{me['id']}", json={"is_active": False}, headers=_hdr(token)
    )
    assert r1.status_code == 400

    r2 = client.patch(
        f"/api/auth/users/{me['id']}", json={"role": "user"}, headers=_hdr(token)
    )
    assert r2.status_code == 400


def test_patch_other_admin_allowed_when_admin_remains(env):
    """存在另一名启用管理员时，可停用其他管理员账号。"""
    client = env[0]
    token = _admin_token(client, env[1])
    uid2 = _mkuser(env[1], "admin2", "secret123", role="admin")

    r = client.patch(
        f"/api/auth/users/{uid2}", json={"is_active": False}, headers=_hdr(token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["is_active"] is False


def test_patch_nonexistent_user_404(env):
    """PATCH 不存在的用户 404。"""
    client = env[0]
    token = _admin_token(client, env[1])
    r = client.patch(
        "/api/auth/users/9999", json={"is_active": False}, headers=_hdr(token)
    )
    assert r.status_code == 404


def test_list_users_admin(env):
    """管理员可获取用户列表。"""
    client = env[0]
    token = _admin_token(client, env[1])
    _mkuser(env[1], "u2", "secret123")
    r = client.get("/api/auth/users", headers=_hdr(token))
    assert r.status_code == 200, r.text
    names = [u["username"] for u in r.json()["users"]]
    assert names == ["admin", "u2"]
