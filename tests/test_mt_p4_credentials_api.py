"""MT-P4-A 凭证 API 属主化测试（providers.py / channels.py 租户化与凭证掩码）。

覆盖（docs/17 R8 T3/T13/T21）：
1. AI 服务：共享配额租户可见管理员托管服务但 api_key 掩码为空；非共享租户仅见自建；
   越权改/删他租户（含托管）服务 404 不泄露存在性；新建归属当前租户且客户端无法注入 tenant_id；
2. 通知渠道：托管共享渠道 config 掩码不出网；普通租户建/改 is_shared 被拒（403）；
   越权改/删 404；托管渠道 /test 用服务端 config 发送且不回显；
3. PANWATCH_SINGLE_TENANT='1' 单租户直通：全部路径行为与改造前等价。
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
from src.web.api import auth, channels, providers
from src.web.api.auth import get_current_user
from src.web.database import Base, get_db

SECRET = "mt-p4-cred-secret-0123456789abcdef"
OLD_PWD_AT = datetime(2020, 1, 1, 0, 0, 0)


@pytest.fixture
def mt_env(monkeypatch):
    """内存库 + do_orm_execute 事件 + 固定 JWT secret + providers/channels app。"""
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

    protected = [Depends(get_current_user)]
    app = FastAPI(redirect_slashes=False)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(
        providers.router, prefix="/api/providers", dependencies=protected
    )
    app.include_router(
        channels.router, prefix="/api/channels", dependencies=protected
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
    quota_shared: bool = False,
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
            quota_shared_with_admin=quota_shared,
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


def _seed_services(Session) -> tuple[int, int, int]:
    """租户 1/2/3 各一个服务，返回 (svc1, svc2, svc3) id。"""
    s = Session()
    a1 = M.AIService(
        tenant_id=1, name="admin-svc", base_url="https://a.example", api_key="sk-admin"
    )
    a2 = M.AIService(
        tenant_id=2, name="own-svc", base_url="https://b.example", api_key="sk-own"
    )
    a3 = M.AIService(
        tenant_id=3, name="other-svc", base_url="https://c.example", api_key="sk-other"
    )
    s.add_all([a1, a2, a3])
    s.commit()
    ids = (a1.id, a2.id, a3.id)
    s.close()
    return ids


def _seed_channels(Session) -> tuple[int, int, int]:
    """租户 1 托管共享渠道 + 租户 2 私有渠道 + 租户 3 私有渠道。"""
    s = Session()
    c1 = M.NotifyChannel(
        tenant_id=1, name="managed", type="webhook",
        config={"url": "http://managed-hook/secret"}, enabled=True, is_shared=True,
    )
    c2 = M.NotifyChannel(
        tenant_id=2, name="own", type="webhook",
        config={"url": "http://own-hook"}, enabled=True,
    )
    c3 = M.NotifyChannel(
        tenant_id=3, name="other", type="webhook",
        config={"url": "http://other-hook"}, enabled=True,
    )
    s.add_all([c1, c2, c3])
    s.commit()
    ids = (c1.id, c2.id, c3.id)
    s.close()
    return ids


class _FakeNotifier:
    """记录 add_channel 调用的 NotifierManager 替身（不触网）。"""

    instances: list["_FakeNotifier"] = []

    def __init__(self):
        self.channels: list[tuple[str, dict]] = []
        _FakeNotifier.instances.append(self)

    def add_channel(self, channel_type: str, config: dict) -> None:
        self.channels.append((channel_type, config))

    async def notify_with_result(self, **kwargs) -> dict:
        return {"success": True}


# ---------------------------------------------------------------------------
# 1. AI 服务可见集与凭证掩码（T13）
# ---------------------------------------------------------------------------


def test_shared_quota_tenant_sees_managed_service_masked(mt_env, monkeypatch):
    """多租户：共享配额租户可见管理员托管服务，api_key 掩码为空、is_managed=True。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2, quota_shared=True)
    svc1, svc2, svc3 = _seed_services(Session)

    r = client.get("/api/providers/services", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {s["id"]: s for s in body}
    assert set(by_id) == {svc1, svc2}, "共享配额租户应见自建 + 托管服务，不见租户3"

    own = by_id[svc2]
    assert own["api_key"] == "sk-own"
    assert own["tenant_id"] == 2
    assert own["is_managed"] is False

    managed = by_id[svc1]
    assert managed["api_key"] == "", "托管服务 api_key 必须掩码"
    assert managed["tenant_id"] == 1
    assert managed["is_managed"] is True
    assert managed["base_url"] == "https://a.example"


def test_non_shared_tenant_sees_only_own_services(mt_env, monkeypatch):
    """多租户：未共享配额租户仅见本租户自建服务。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u3", tenant_id=3, quota_shared=False)
    svc1, svc2, svc3 = _seed_services(Session)

    r = client.get("/api/providers/services", headers=_hdr(client, "u3"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["id"] for s in body] == [svc3]
    assert body[0]["api_key"] == "sk-other"
    assert body[0]["is_managed"] is False


def test_admin_sees_own_services_unmasked(mt_env, monkeypatch):
    """多租户：管理员（租户1）只见本租户服务、密钥明文、无重复托管行。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    svc1, svc2, svc3 = _seed_services(Session)

    r = client.get("/api/providers/services", headers=_hdr(client, "admin"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["id"] for s in body] == [svc1]
    assert body[0]["api_key"] == "sk-admin"
    assert body[0]["is_managed"] is False


def test_cross_tenant_service_update_delete_404(mt_env, monkeypatch):
    """多租户：越权改/删他租户（含托管）服务一律 404，且行不受影响。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2, quota_shared=True)
    svc1, svc2, svc3 = _seed_services(Session)
    hdr = _hdr(client, "u2")

    for target in (svc1, svc3):
        assert (
            client.put(
                f"/api/providers/services/{target}",
                json={"api_key": "hacked"},
                headers=hdr,
            ).status_code
            == 404
        )
        assert (
            client.delete(f"/api/providers/services/{target}", headers=hdr).status_code
            == 404
        )

    s = Session()
    rows = {r_.id: r_ for r_ in s.query(M.AIService).all()}
    assert rows[svc1].api_key == "sk-admin"
    assert rows[svc3].api_key == "sk-other"
    s.close()

    # 本租户服务可正常改/删
    assert (
        client.put(
            f"/api/providers/services/{svc2}",
            json={"api_key": "sk-own-2"},
            headers=hdr,
        ).status_code
        == 200
    )
    assert client.delete(f"/api/providers/services/{svc2}", headers=hdr).status_code == 200


def test_service_create_attributed_to_current_tenant(mt_env, monkeypatch):
    """多租户：新建服务归属当前租户；客户端注入 tenant_id 被忽略。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2, quota_shared=False)

    r = client.post(
        "/api/providers/services",
        json={
            "name": "new-svc",
            "base_url": "https://n.example",
            "api_key": "sk-new",
            "tenant_id": 1,  # 注入尝试：应被忽略
        },
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == 2
    assert body["is_managed"] is False
    assert body["api_key"] == "sk-new"

    s = Session()
    row = s.query(M.AIService).filter_by(name="new-svc").one()
    assert row.tenant_id == 2
    s.close()


def test_single_tenant_services_passthrough(mt_env, monkeypatch):
    """单租户直通：全表返回、密钥明文、is_managed 恒 False、可改任意行（等价现状）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2, quota_shared=False)
    svc1, svc2, svc3 = _seed_services(Session)

    r = client.get("/api/providers/services", headers=_hdr(client, "admin"))
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {s["id"]: s for s in body}
    assert set(by_id) == {svc1, svc2, svc3}
    assert by_id[svc1]["api_key"] == "sk-admin"
    assert by_id[svc3]["api_key"] == "sk-other"
    assert all(s["is_managed"] is False for s in body)

    # 直通模式下普通登录用户也见全表（改造前行为）
    r2 = client.get("/api/providers/services", headers=_hdr(client, "u2"))
    assert len(r2.json()) == 3

    # 可改他租户行（改造前无属主校验）
    assert (
        client.put(
            f"/api/providers/services/{svc3}",
            json={"name": "renamed"},
            headers=_hdr(client, "admin"),
        ).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# 2. 通知渠道可见集与凭证掩码（T21）
# ---------------------------------------------------------------------------


def test_managed_channel_config_not_leaked(mt_env, monkeypatch):
    """多租户：托管共享渠道可见但 config 掩码为空 dict；本租户渠道 config 明文。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2)
    c1, c2, c3 = _seed_channels(Session)

    r = client.get("/api/channels", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    by_id = {c["id"]: c for c in r.json()}
    assert set(by_id) == {c1, c2}, "应见本租户私有 + 托管共享渠道，不见租户3私有"

    own = by_id[c2]
    assert own["config"] == {"url": "http://own-hook"}
    assert own["is_managed"] is False
    assert own["is_shared"] is False
    assert own["tenant_id"] == 2

    managed = by_id[c1]
    assert managed["config"] == {}, "托管渠道 config 必须掩码不出网"
    assert managed["is_managed"] is True
    assert managed["is_shared"] is True
    assert managed["tenant_id"] == 1


def test_non_admin_cannot_create_shared_channel(mt_env, monkeypatch):
    """多租户：普通租户建 is_shared 渠道 403；管理员可建。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2)

    r = client.post(
        "/api/channels",
        json={"name": "s", "type": "webhook", "config": {}, "is_shared": True},
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 403

    r2 = client.post(
        "/api/channels",
        json={
            "name": "s",
            "type": "webhook",
            "config": {"url": "http://x"},
            "is_shared": True,
        },
        headers=_hdr(client, "admin"),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["is_shared"] is True
    assert r2.json()["tenant_id"] == 1


def test_non_admin_cannot_flip_is_shared(mt_env, monkeypatch):
    """多租户：普通租户不得改 is_shared 标志（403），行保持不变。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2)
    _, c2, _ = _seed_channels(Session)

    r = client.put(
        f"/api/channels/{c2}", json={"is_shared": True}, headers=_hdr(client, "u2")
    )
    assert r.status_code == 403
    s = Session()
    assert s.get(M.NotifyChannel, c2).is_shared in (False, 0)
    s.close()

    # 普通字段可正常改
    r2 = client.put(
        f"/api/channels/{c2}", json={"name": "renamed"}, headers=_hdr(client, "u2")
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == "renamed"


def test_cross_tenant_channel_update_delete_404(mt_env, monkeypatch):
    """多租户：越权改/删他租户（含托管共享）渠道一律 404。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2)
    c1, c2, c3 = _seed_channels(Session)
    hdr = _hdr(client, "u2")

    for target in (c1, c3):
        assert (
            client.put(
                f"/api/channels/{target}", json={"name": "x"}, headers=hdr
            ).status_code
            == 404
        )
        assert client.delete(f"/api/channels/{target}", headers=hdr).status_code == 404

    s = Session()
    assert s.query(M.NotifyChannel).count() == 3
    s.close()
    assert client.delete(f"/api/channels/{c2}", headers=hdr).status_code == 200


def test_channel_test_on_managed_uses_server_config(mt_env, monkeypatch):
    """多租户：可对可见托管渠道发测试（服务端 config）；他人私有渠道 404。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2)
    c1, c2, c3 = _seed_channels(Session)

    _FakeNotifier.instances = []
    monkeypatch.setattr(channels, "NotifierManager", _FakeNotifier)

    r = client.post(f"/api/channels/{c1}/test", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    # 测试发送用服务端真实 config（GET 已掩码，这里验证服务端仍持有密钥）
    assert _FakeNotifier.instances[0].channels == [
        ("webhook", {"url": "http://managed-hook/secret"})
    ]

    # 他人私有渠道不可测（404 不泄露存在性）
    assert (
        client.post(f"/api/channels/{c3}/test", headers=_hdr(client, "u2")).status_code
        == 404
    )


def test_channel_create_attributed_to_current_tenant(mt_env, monkeypatch):
    """多租户：新建渠道归属当前租户；客户端注入 tenant_id 被忽略。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2)

    r = client.post(
        "/api/channels",
        json={
            "name": "mine",
            "type": "webhook",
            "config": {"url": "http://mine"},
            "tenant_id": 1,  # 注入尝试：应被忽略
        },
        headers=_hdr(client, "u2"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == 2
    assert r.json()["is_shared"] is False

    s = Session()
    row = s.query(M.NotifyChannel).filter_by(name="mine").one()
    assert row.tenant_id == 2
    s.close()


def test_single_tenant_channels_passthrough(mt_env, monkeypatch):
    """单租户直通：全表返回、config 明文、is_managed 恒 False、可改任意行（等价现状）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")
    client, Session = mt_env
    _mkuser(Session, "admin", tenant_id=1, role="admin", quota_shared=True)
    _mkuser(Session, "u2", tenant_id=2)
    c1, c2, c3 = _seed_channels(Session)

    r = client.get("/api/channels", headers=_hdr(client, "u2"))
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {c["id"]: c for c in body}
    assert set(by_id) == {c1, c2, c3}
    assert by_id[c1]["config"] == {"url": "http://managed-hook/secret"}
    assert by_id[c3]["config"] == {"url": "http://other-hook"}
    assert all(c["is_managed"] is False for c in body)

    # 可改他租户行（改造前无属主校验）
    assert (
        client.put(
            f"/api/channels/{c3}", json={"name": "renamed"}, headers=_hdr(client, "u2")
        ).status_code
        == 200
    )
