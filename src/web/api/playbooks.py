"""个股方案档案 API —— Phase 2 P2a（契约 B）。

路由（app.py 以 /api 前缀挂载，需登录）：
- GET  /api/stocks/{stock_id}/playbook   激活版本（含 payload 全文），无则 data=null
- GET  /api/stocks/{stock_id}/playbooks  版本列表（不含 payload，含 summary）
- POST /api/stocks/{stock_id}/playbooks  新建版本（version 自增并置为 active）
- POST /api/playbooks/{playbook_id}/activate  切换激活版本
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.playbook import load_active_playbook, summarize_playbook
from src.web.database import get_db
from src.web.models import Stock, StockPlaybook

logger = logging.getLogger(__name__)
router = APIRouter()


class PlaybookCreate(BaseModel):
    payload: dict[str, Any]
    note: str | None = None


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _to_detail(row: StockPlaybook) -> dict:
    return {
        "id": row.id,
        "stock_id": row.stock_id,
        "version": row.version,
        "is_active": bool(row.is_active),
        "payload": row.payload or {},
        "summary": row.summary or "",
        "note": row.note or "",
        "created_at": _iso(row.created_at),
    }


def _to_list_item(row: StockPlaybook) -> dict:
    """版本列表项：不含 payload 全文。"""
    return {
        "id": row.id,
        "stock_id": row.stock_id,
        "version": row.version,
        "is_active": bool(row.is_active),
        "summary": row.summary or "",
        "note": row.note or "",
        "created_at": _iso(row.created_at),
    }


def _get_stock_or_404(db: Session, stock_id: int) -> Stock:
    stock = db.query(Stock).filter(Stock.id == stock_id).first()
    if not stock:
        raise HTTPException(404, "股票不存在")
    return stock


@router.get("/stocks/{stock_id}/playbook")
def get_active_playbook(stock_id: int, db: Session = Depends(get_db)):
    _get_stock_or_404(db, stock_id)
    row = load_active_playbook(db, stock_id)
    return _to_detail(row) if row else None


@router.get("/stocks/{stock_id}/playbooks")
def list_playbooks(stock_id: int, db: Session = Depends(get_db)):
    _get_stock_or_404(db, stock_id)
    rows = (
        db.query(StockPlaybook)
        .filter(StockPlaybook.stock_id == stock_id)
        .order_by(StockPlaybook.version.desc(), StockPlaybook.id.desc())
        .all()
    )
    return [_to_list_item(r) for r in rows]


@router.post("/stocks/{stock_id}/playbooks")
def create_playbook(
    stock_id: int, body: PlaybookCreate, db: Session = Depends(get_db)
):
    _get_stock_or_404(db, stock_id)
    if not isinstance(body.payload, dict) or not body.payload:
        raise HTTPException(400, "payload 不能为空对象")

    payload = dict(body.payload)
    # 契约 A 信封：缺 schema_version 时补默认值 1（loader 侧本就容错，这里保证新建档案规范）
    payload.setdefault("schema_version", 1)

    max_version = (
        db.query(StockPlaybook.version)
        .filter(StockPlaybook.stock_id == stock_id)
        .order_by(StockPlaybook.version.desc())
        .first()
    )
    next_version = (max_version[0] if max_version else 0) + 1

    # 新版本置为 active，其余版本 is_active=false（同事务）
    db.query(StockPlaybook).filter(
        StockPlaybook.stock_id == stock_id,
        StockPlaybook.is_active.is_(True),
    ).update({"is_active": False}, synchronize_session="fetch")

    row = StockPlaybook(
        stock_id=stock_id,
        version=next_version,
        is_active=True,
        payload=payload,
        summary=summarize_playbook(payload),
        note=(body.note or "").strip(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_detail(row)


@router.post("/playbooks/{playbook_id}/activate")
def activate_playbook(playbook_id: int, db: Session = Depends(get_db)):
    row = db.query(StockPlaybook).filter(StockPlaybook.id == playbook_id).first()
    if not row:
        raise HTTPException(404, "方案档案不存在")

    db.query(StockPlaybook).filter(
        StockPlaybook.stock_id == row.stock_id,
        StockPlaybook.id != row.id,
        StockPlaybook.is_active.is_(True),
    ).update({"is_active": False}, synchronize_session="fetch")
    row.is_active = True
    db.commit()
    db.refresh(row)
    return _to_detail(row)
