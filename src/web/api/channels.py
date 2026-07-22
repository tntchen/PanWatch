from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.web.database import get_db
from src.web.models import NotifyChannel
from src.core.notifier import NotifierManager, CHANNEL_TYPES

router = APIRouter()

# ── 排他 is_default 全表 update 租户收口（MT-P2，docs/22 §2.x / docs/26-J11）──
# do_orm_execute 已对 SessionLocal 全局生效，会给 ORM bulk UPDATE 注入 tenant
# 谓词；此处叠加显式 tenant 条件做双保险。模型尚未映射 tenant_id（迁移双轨
# 窗口期）或无 ctx 时不加条件，保持单租户行为等价。
try:  # 防御：tenant_context 不可用时退化为不加条件（等价单租户）
    from src.web.tenant_context import current_tenant as _current_tenant
except Exception:  # pragma: no cover - 防御性兜底
    _current_tenant = None  # type: ignore[assignment]


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


class ChannelUpdate(BaseModel):
    name: str | None = None
    type: str | None = None
    config: dict | None = None
    enabled: bool | None = None
    is_default: bool | None = None


class ChannelResponse(BaseModel):
    id: int
    name: str
    type: str
    config: dict
    enabled: bool
    is_default: bool

    class Config:
        from_attributes = True


@router.get("", response_model=list[ChannelResponse])
def list_channels(db: Session = Depends(get_db)):
    return db.query(NotifyChannel).order_by(NotifyChannel.id).all()


@router.get("/types")
def list_channel_types():
    """返回支持的渠道类型及其字段"""
    return CHANNEL_TYPES


@router.post("", response_model=ChannelResponse)
def create_channel(body: ChannelCreate, db: Session = Depends(get_db)):
    if body.is_default:
        _reset_default_channels(db)
    channel = NotifyChannel(**body.model_dump())
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


@router.put("/{channel_id}", response_model=ChannelResponse)
def update_channel(channel_id: int, body: ChannelUpdate, db: Session = Depends(get_db)):
    channel = db.query(NotifyChannel).filter(NotifyChannel.id == channel_id).first()
    if not channel:
        raise HTTPException(404, "通知渠道不存在")

    data = body.model_dump(exclude_unset=True)
    if data.get("is_default"):
        _reset_default_channels(db)

    for key, value in data.items():
        setattr(channel, key, value)

    db.commit()
    db.refresh(channel)
    return channel


@router.delete("/{channel_id}")
def delete_channel(channel_id: int, db: Session = Depends(get_db)):
    channel = db.query(NotifyChannel).filter(NotifyChannel.id == channel_id).first()
    if not channel:
        raise HTTPException(404, "通知渠道不存在")
    db.delete(channel)
    db.commit()
    return {"ok": True}


@router.post("/{channel_id}/test")
async def test_channel(channel_id: int, db: Session = Depends(get_db)):
    """发送测试通知"""
    channel = db.query(NotifyChannel).filter(NotifyChannel.id == channel_id).first()
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
