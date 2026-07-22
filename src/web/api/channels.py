from contextlib import contextmanager
from typing import Any, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.web.database import get_db
from src.web.models import NotifyChannel, User
from src.core.notifier import NotifierManager, CHANNEL_TYPES
from src.web.api.auth import get_current_user

router = APIRouter()

# ── 排他 is_default 全表 update 租户收口（MT-P2，docs/22 §2.x / docs/26-J11）──
# do_orm_execute 已对 SessionLocal 全局生效，会给 ORM bulk UPDATE 注入 tenant
# 谓词；此处叠加显式 tenant 条件做双保险。模型尚未映射 tenant_id（迁移双轨
# 窗口期）或无 ctx 时不加条件，保持单租户行为等价。
try:  # 防御：tenant_context 不可用时退化为不加条件（等价单租户）
    from src.web.tenant_context import (
        DEFAULT_TENANT_ID as _DEFAULT_TENANT_ID,
        current_tenant as _current_tenant,
        reset_current_tenant as _reset_current_tenant,
        set_current_tenant as _set_current_tenant,
        single_tenant_mode as _single_tenant_mode,
    )
except Exception:  # pragma: no cover - 防御性兜底
    _DEFAULT_TENANT_ID = 1  # type: ignore[assignment]
    _current_tenant = None  # type: ignore[assignment]
    _set_current_tenant = None  # type: ignore[assignment]
    _reset_current_tenant = None  # type: ignore[assignment]
    _single_tenant_mode = None  # type: ignore[assignment]


def _raw_ctx() -> Optional[Any]:
    """读取当前租户 ctx（不区分单/多租户）；模块缺失或异常返回 None。"""
    if _current_tenant is None:
        return None
    try:
        return _current_tenant()
    except Exception:  # pragma: no cover - 防御性兜底
        return None


def _mt_ctx() -> Optional[Any]:
    """多租户模式下的当前租户 ctx；单租户直通/无 ctx 返回 None（等价现状全表）。"""
    if _single_tenant_mode is None:
        return None
    try:
        if _single_tenant_mode():
            return None
    except Exception:  # pragma: no cover - 防御性兜底
        return None
    return _raw_ctx()


@contextmanager
def _unscoped_read() -> Iterator[None]:
    """临时清空租户 ctx：读取跨租户可见的托管行时绕开 do_orm_execute 自动过滤。

    仅用于只读查询，结果在 API 层再做可见性判定（本租户 / is_shared）与
    凭证掩码（config 不出网）。
    """
    if _set_current_tenant is None:
        yield
        return
    token = _set_current_tenant(None)
    try:
        yield
    finally:
        _reset_current_tenant(token)


def _reset_default_channels(db: Session) -> None:
    """复位全部渠道的 is_default（排他默认渠道前置步骤），按租户收口。"""
    query = db.query(NotifyChannel)
    if _current_tenant is not None and hasattr(NotifyChannel, "tenant_id"):
        try:
            ctx = _current_tenant()
        except Exception:  # pragma: no cover - 防御性兜底
            ctx = None
        if ctx is not None:
            query = query.filter(NotifyChannel.tenant_id == ctx.tenant_id)
    query.update({"is_default": False})


class ChannelCreate(BaseModel):
    name: str
    type: str = "telegram"
    config: dict = {}
    enabled: bool = True
    is_default: bool = False
    is_shared: bool = False  # 仅管理员可置 True（托管渠道，T21）


class ChannelUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    config: dict | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    is_shared: bool | None = None  # 仅管理员可改（T21）


class ChannelResponse(BaseModel):
    id: int
    name: str
    type: str
    config: dict
    enabled: bool
    is_default: bool
    tenant_id: int = 1
    is_shared: bool = False
    is_managed: bool = False

    class Config:
        from_attributes = True


def _channel_to_response(channel: NotifyChannel, managed: bool = False) -> dict:
    return {
        "id": channel.id,
        "name": channel.name,
        "type": channel.type,
        # 托管渠道凭证不出网：config 掩码为空 dict
        "config": {} if managed else (channel.config or {}),
        "enabled": bool(channel.enabled),
        "is_default": bool(channel.is_default),
        "tenant_id": getattr(channel, "tenant_id", None) or 1,
        "is_shared": bool(getattr(channel, "is_shared", False)),
        "is_managed": managed,
    }


@router.get("", response_model=list[ChannelResponse])
def list_channels(db: Session = Depends(get_db)):
    """T21 可见集：本租户私有渠道（含 config）+ is_shared 托管渠道（config
    掩码为空 dict，密钥不出网）。单租户直通返回全表，等价现状。"""
    ctx = _mt_ctx()
    if ctx is None:
        channels = db.query(NotifyChannel).order_by(NotifyChannel.id).all()
        return [_channel_to_response(c) for c in channels]

    own = (
        db.query(NotifyChannel)
        .filter(NotifyChannel.tenant_id == ctx.tenant_id)
        .order_by(NotifyChannel.id)
        .all()
    )
    result = [_channel_to_response(c) for c in own]
    with _unscoped_read():
        shared = (
            db.query(NotifyChannel)
            .filter(
                NotifyChannel.is_shared == True,  # noqa: E712
                NotifyChannel.tenant_id != ctx.tenant_id,
            )
            .order_by(NotifyChannel.id)
            .all()
        )
        result.extend(_channel_to_response(c, managed=True) for c in shared)
    return result


@router.get("/types")
def list_channel_types():
    """返回支持的渠道类型及其字段"""
    return CHANNEL_TYPES


@router.post("", response_model=ChannelResponse)
def create_channel(
    body: ChannelCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if body.is_shared and getattr(user, "role", None) != "admin":
        raise HTTPException(403, "仅管理员可创建托管共享渠道")
    if body.is_default:
        _reset_default_channels(db)
    # tenant_id 由服务端按当前租户归属，客户端禁止指定（ChannelCreate 无该字段）
    data = body.model_dump()
    ctx = _raw_ctx()
    if ctx is not None:
        data["tenant_id"] = ctx.tenant_id
    channel = NotifyChannel(**data)
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return _channel_to_response(channel)


def _get_own_channel(db: Session, channel_id: int) -> NotifyChannel | None:
    """按 id 取本租户渠道；多租户下他租户/托管行一律 None（404，不泄露存在性）。"""
    query = db.query(NotifyChannel).filter(NotifyChannel.id == channel_id)
    ctx = _mt_ctx()
    if ctx is not None:
        query = query.filter(NotifyChannel.tenant_id == ctx.tenant_id)
    return query.first()


@router.put("/{channel_id}", response_model=ChannelResponse)
def update_channel(
    channel_id: int,
    body: ChannelUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    channel = _get_own_channel(db, channel_id)
    if not channel:
        raise HTTPException(404, "通知渠道不存在")

    data = body.model_dump(exclude_unset=True)
    if "is_shared" in data and getattr(user, "role", None) != "admin":
        raise HTTPException(403, "仅管理员可修改托管共享标志")
    if data.get("is_default"):
        _reset_default_channels(db)

    for key, value in data.items():
        setattr(channel, key, value)

    db.commit()
    db.refresh(channel)
    return _channel_to_response(channel)


@router.delete("/{channel_id}")
def delete_channel(channel_id: int, db: Session = Depends(get_db)):
    channel = _get_own_channel(db, channel_id)
    if not channel:
        raise HTTPException(404, "通知渠道不存在")
    db.delete(channel)
    db.commit()
    return {"ok": True}


@router.post("/{channel_id}/test")
async def test_channel(channel_id: int, db: Session = Depends(get_db)):
    """发送测试通知。可见的托管共享渠道也可测试（用服务端 config，不回显）。"""
    ctx = _mt_ctx()
    if ctx is None:
        channel = (
            db.query(NotifyChannel).filter(NotifyChannel.id == channel_id).first()
        )
    else:
        with _unscoped_read():
            channel = (
                db.query(NotifyChannel)
                .filter(NotifyChannel.id == channel_id)
                .first()
            )
        # T21 可见性：本租户私有渠道或 is_shared 托管渠道；他人私有渠道 404
        if (
            channel is not None
            and channel.tenant_id != ctx.tenant_id
            and not channel.is_shared
        ):
            channel = None
    if not channel:
        raise HTTPException(404, "通知渠道不存在")

    notifier = NotifierManager()
    try:
        notifier.add_channel(channel.type, channel.config or {})
    except Exception as e:
        raise HTTPException(400, f"渠道配置无效: {e}")

    result = await notifier.notify_with_result(
        title="测试通知",
        content="这是一条来自盯盯的测试通知，如果您收到此消息说明通知渠道配置正确。",
        bypass_quiet_hours=True,
    )

    if result.get("success"):
        return {"ok": True, "message": "测试通知发送成功"}
    else:
        raise HTTPException(500, f"通知发送失败: {result.get('error', '未知错误')}")
