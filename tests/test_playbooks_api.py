"""方案档案 API 测试（src/web/api/playbooks.py，契约 B + doc/14 §1 P2a）。

覆盖：
- POST 创建版本：version 自增、新版本置 active、其余版本 is_active=false、summary 后端生成；
- GET /stocks/{id}/playbook：返回激活版本完整 payload；无档案返回 None；
- GET /stocks/{id}/playbooks：版本列表不含 payload、含 summary；
- POST /playbooks/{id}/activate：激活切换；
- 404：股票不存在 / 档案不存在；400：空 payload；
- 路由已挂载到 app。

测试模式：内存 sqlite + 直接调用端点函数（同 test_position_trades.py）。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M
from src.web.api import playbooks as playbooks_api
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


def _make_stock(db) -> M.Stock:
    st = M.Stock(symbol="688110", name="东芯股份", market="CN")
    db.add(st)
    db.commit()
    return st


def _payload(tag: str = "v1") -> dict:
    return {
        "schema_version": 1,
        "meta": {
            "name": "东芯股份抄底实施方案",
            "version_label": tag,
            "strategy_mode": "激进·满仓单票",
            "base_date": "2026-07-20",
            "base_price": 100.38,
        },
        "price_levels": [{"label": "防线", "value": 98}],
        "batches": [{"name": "①", "trigger": "120±3", "status": "executed"}],
        "defense": {"rule": "连续2日收盘<98", "action": "减仓1/3~1/2"},
        "raw_markdown": f"# 原始方案 {tag}",
    }


# ---------------------------------------------------------------- create


def test_create_first_version_active_with_summary(db):
    """首个版本 version=1、is_active=True，summary 由后端从 payload 生成。"""
    st = _make_stock(db)
    res = playbooks_api.create_playbook(
        st.id, playbooks_api.PlaybookCreate(payload=_payload("v1"), note="首版"), db=db
    )
    assert res["version"] == 1
    assert res["is_active"] is True
    assert res["note"] == "首版"
    assert res["payload"]["meta"]["version_label"] == "v1"
    assert "激进·满仓单票" in res["summary"]
    assert "防线98" in res["summary"]
    assert res["created_at"]


def test_create_increments_version_and_deactivates_previous(db):
    """第二版 version=2 并置 active，v1 自动 is_active=false。"""
    st = _make_stock(db)
    v1 = playbooks_api.create_playbook(
        st.id, playbooks_api.PlaybookCreate(payload=_payload("v1")), db=db
    )
    v2 = playbooks_api.create_playbook(
        st.id, playbooks_api.PlaybookCreate(payload=_payload("v2")), db=db
    )
    assert v2["version"] == 2
    assert v2["is_active"] is True

    rows = db.query(M.StockPlaybook).filter(M.StockPlaybook.stock_id == st.id).all()
    active_map = {r.version: bool(r.is_active) for r in rows}
    assert active_map == {1: False, 2: True}

    # GET 激活版本应为 v2
    active = playbooks_api.get_active_playbook(st.id, db=db)
    assert active["id"] == v2["id"]
    assert active["payload"]["meta"]["version_label"] == "v2"
    assert v1["id"] != v2["id"]


def test_create_defaults_missing_schema_version(db):
    """payload 缺 schema_version 时后端补默认值 1（契约 A 信封）。"""
    st = _make_stock(db)
    payload = _payload("v1")
    del payload["schema_version"]
    res = playbooks_api.create_playbook(
        st.id, playbooks_api.PlaybookCreate(payload=payload), db=db
    )
    assert res["payload"]["schema_version"] == 1


def test_create_empty_payload_rejected_400(db):
    """空 payload 对象 → 400。"""
    st = _make_stock(db)
    with pytest.raises(HTTPException) as ei:
        playbooks_api.create_playbook(
            st.id, playbooks_api.PlaybookCreate(payload={}), db=db
        )
    assert ei.value.status_code == 400


def test_create_unknown_stock_404(db):
    """股票不存在 → 404。"""
    with pytest.raises(HTTPException) as ei:
        playbooks_api.create_playbook(
            99999, playbooks_api.PlaybookCreate(payload=_payload()), db=db
        )
    assert ei.value.status_code == 404


# ------------------------------------------------------------------- get


def test_get_active_returns_none_when_no_playbook(db):
    """无档案股票 GET 激活版本返回 None（ResponseWrapper 包装后 data=null）。"""
    st = _make_stock(db)
    assert playbooks_api.get_active_playbook(st.id, db=db) is None


def test_get_active_unknown_stock_404(db):
    """股票不存在 → 404。"""
    with pytest.raises(HTTPException) as ei:
        playbooks_api.get_active_playbook(99999, db=db)
    assert ei.value.status_code == 404


def test_list_playbooks_excludes_payload_includes_summary(db):
    """版本列表不含 payload 全文，含 summary，按版本倒序。"""
    st = _make_stock(db)
    playbooks_api.create_playbook(
        st.id, playbooks_api.PlaybookCreate(payload=_payload("v1")), db=db
    )
    playbooks_api.create_playbook(
        st.id, playbooks_api.PlaybookCreate(payload=_payload("v2")), db=db
    )
    rows = playbooks_api.list_playbooks(st.id, db=db)
    assert [r["version"] for r in rows] == [2, 1]
    for r in rows:
        assert "payload" not in r
        assert r["summary"]
        assert r["is_active"] == (r["version"] == 2)


# -------------------------------------------------------------- activate


def test_activate_switches_active_version(db):
    """activate 切换激活版本：目标置 active，其余置 false。"""
    st = _make_stock(db)
    v1 = playbooks_api.create_playbook(
        st.id, playbooks_api.PlaybookCreate(payload=_payload("v1")), db=db
    )
    v2 = playbooks_api.create_playbook(
        st.id, playbooks_api.PlaybookCreate(payload=_payload("v2")), db=db
    )

    res = playbooks_api.activate_playbook(v1["id"], db=db)
    assert res["is_active"] is True
    assert res["version"] == 1

    active = playbooks_api.get_active_playbook(st.id, db=db)
    assert active["id"] == v1["id"]

    rows = db.query(M.StockPlaybook).filter(M.StockPlaybook.stock_id == st.id).all()
    assert {r.version: bool(r.is_active) for r in rows} == {1: True, 2: False}

    # 再切回 v2
    playbooks_api.activate_playbook(v2["id"], db=db)
    assert playbooks_api.get_active_playbook(st.id, db=db)["id"] == v2["id"]


def test_activate_unknown_playbook_404(db):
    """档案不存在 → 404。"""
    with pytest.raises(HTTPException) as ei:
        playbooks_api.activate_playbook(99999, db=db)
    assert ei.value.status_code == 404


def test_activate_does_not_touch_other_stocks(db):
    """激活切换只影响同股票版本，不波及其他股票的激活状态。"""
    st1 = _make_stock(db)
    st2 = M.Stock(symbol="600519", name="贵州茅台", market="CN")
    db.add(st2)
    db.commit()

    a1 = playbooks_api.create_playbook(
        st1.id, playbooks_api.PlaybookCreate(payload=_payload("v1")), db=db
    )
    a2 = playbooks_api.create_playbook(
        st1.id, playbooks_api.PlaybookCreate(payload=_payload("v2")), db=db
    )
    b1 = playbooks_api.create_playbook(
        st2.id, playbooks_api.PlaybookCreate(payload=_payload("b1")), db=db
    )

    playbooks_api.activate_playbook(a1["id"], db=db)
    assert playbooks_api.get_active_playbook(st2.id, db=db)["id"] == b1["id"]
    assert playbooks_api.get_active_playbook(st1.id, db=db)["id"] == a1["id"]
    assert a2["id"] != a1["id"]


# --------------------------------------------------------------- mounted


def test_playbooks_router_mounted():
    """契约 B 四条路由已挂载到 app（走 OpenAPI schema）。"""
    from src.web.app import app

    paths = set(app.openapi().get("paths", {}).keys())
    assert "/api/stocks/{stock_id}/playbook" in paths
    assert "/api/stocks/{stock_id}/playbooks" in paths
    assert "/api/playbooks/{playbook_id}/activate" in paths
