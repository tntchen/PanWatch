from contextlib import contextmanager
from typing import Any, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.web.database import get_db
from src.web.models import AIService, AIModel, User
from src.core.ai_client import AIClient
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

    do_orm_execute 会在 ctx 非空时给已登记私有表追加 tenant 谓词；T13/T21
    可见集需要读管理员（tenant_id=1）托管行，必须短暂放行，读完立即恢复。
    仅用于只读查询，且结果在 API 层再做可见性判定与凭证掩码。
    """
    if _set_current_tenant is None:
        yield
        return
    token = _set_current_tenant(None)
    try:
        yield
    finally:
        _reset_current_tenant(token)


def _reset_default_models(db: Session) -> None:
    """复位全部模型的 is_default（排他默认模型前置步骤），按租户收口。"""
    query = db.query(AIModel)
    if _current_tenant is not None and hasattr(AIModel, "tenant_id"):
        try:
            ctx = _current_tenant()
        except Exception:  # pragma: no cover - 防御性兜底
            ctx = None
        if ctx is not None:
            query = query.filter(AIModel.tenant_id == ctx.tenant_id)
    query.update({"is_default": False})


# --- Service ---

class ServiceCreate(BaseModel):
    name: str
    base_url: str
    api_key: str = ""


class ServiceUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class ModelResponse(BaseModel):
    id: int
    name: str
    service_id: int
    model: str
    is_default: bool

    class Config:
        from_attributes = True


class ServiceResponse(BaseModel):
    id: int
    name: str
    base_url: str
    api_key: str
    tenant_id: int = 1
    is_managed: bool = False
    models: list[ModelResponse] = []

    class Config:
        from_attributes = True


@router.get("/services", response_model=list[ServiceResponse])
def list_services(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """T13 可见集：本租户自建服务（含真实 api_key）+ 共享配额租户追加管理员
    托管服务（api_key 掩码为空，密钥不出网）。单租户直通返回全表，等价现状。"""
    ctx = _mt_ctx()
    if ctx is None:
        services = db.query(AIService).order_by(AIService.id).all()
        return [_service_to_response(s) for s in services]

    own = (
        db.query(AIService)
        .filter(AIService.tenant_id == ctx.tenant_id)
        .order_by(AIService.id)
        .all()
    )
    result = [_service_to_response(s) for s in own]
    if ctx.tenant_id != _DEFAULT_TENANT_ID and bool(
        getattr(user, "quota_shared_with_admin", False)
    ):
        with _unscoped_read():
            managed = (
                db.query(AIService)
                .filter(AIService.tenant_id == _DEFAULT_TENANT_ID)
                .order_by(AIService.id)
                .all()
            )
            result.extend(_service_to_response(s, managed=True) for s in managed)
    return result


def _service_to_response(service: AIService, managed: bool = False) -> dict:
    return {
        "id": service.id,
        "name": service.name,
        "base_url": service.base_url,
        # 托管服务凭证不出网：一律掩码为空串
        "api_key": "" if managed else (service.api_key or ""),
        "tenant_id": getattr(service, "tenant_id", None) or 1,
        "is_managed": managed,
        "models": [
            {"id": m.id, "name": m.name, "service_id": m.service_id, "model": m.model, "is_default": m.is_default}
            for m in service.models
        ],
    }


@router.post("/services", response_model=ServiceResponse)
def create_service(body: ServiceCreate, db: Session = Depends(get_db)):
    # tenant_id 由服务端按当前租户归属，客户端禁止指定（ServiceCreate 无该字段）
    data = body.model_dump()
    ctx = _raw_ctx()
    if ctx is not None:
        data["tenant_id"] = ctx.tenant_id
    service = AIService(**data)
    db.add(service)
    db.commit()
    db.refresh(service)
    return _service_to_response(service)


def _get_own_service(db: Session, service_id: int) -> AIService | None:
    """按 id 取本租户服务；多租户下他租户/托管行一律 None（404，不泄露存在性）。"""
    query = db.query(AIService).filter(AIService.id == service_id)
    ctx = _mt_ctx()
    if ctx is not None:
        query = query.filter(AIService.tenant_id == ctx.tenant_id)
    return query.first()


@router.put("/services/{service_id}", response_model=ServiceResponse)
def update_service(service_id: int, body: ServiceUpdate, db: Session = Depends(get_db)):
    service = _get_own_service(db, service_id)
    if not service:
        raise HTTPException(404, "AI 服务商不存在")

    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(service, key, value)

    db.commit()
    db.refresh(service)
    return _service_to_response(service)


@router.delete("/services/{service_id}")
def delete_service(service_id: int, db: Session = Depends(get_db)):
    service = _get_own_service(db, service_id)
    if not service:
        raise HTTPException(404, "AI 服务商不存在")
    db.delete(service)
    db.commit()
    return {"ok": True}


# --- Model ---

class ModelCreate(BaseModel):
    name: str = ""
    service_id: int
    model: str
    is_default: bool = False


class ModelUpdate(BaseModel):
    name: str | None = None
    service_id: int | None = None
    model: str | None = None
    is_default: bool | None = None


class BatchModelItem(BaseModel):
    name: str = ""
    model: str
    is_default: bool = False


class BatchModelCreate(BaseModel):
    models: list[BatchModelItem] = []


@router.get("/models", response_model=list[ModelResponse])
def list_models(db: Session = Depends(get_db)):
    return db.query(AIModel).order_by(AIModel.id).all()


@router.post("/models", response_model=ModelResponse)
def create_model(body: ModelCreate, db: Session = Depends(get_db)):
    service = db.query(AIService).filter(AIService.id == body.service_id).first()
    if not service:
        raise HTTPException(400, "AI 服务商不存在")

    if body.is_default:
        _reset_default_models(db)

    data = body.model_dump()
    if not data["name"]:
        data["name"] = data["model"]
    model = AIModel(**data)
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


@router.put("/models/{model_id}", response_model=ModelResponse)
def update_model(model_id: int, body: ModelUpdate, db: Session = Depends(get_db)):
    model = db.query(AIModel).filter(AIModel.id == model_id).first()
    if not model:
        raise HTTPException(404, "AI 模型不存在")

    data = body.model_dump(exclude_unset=True)
    if data.get("is_default"):
        _reset_default_models(db)

    for key, value in data.items():
        setattr(model, key, value)

    db.commit()
    db.refresh(model)
    return model


@router.delete("/models/{model_id}")
def delete_model(model_id: int, db: Session = Depends(get_db)):
    model = db.query(AIModel).filter(AIModel.id == model_id).first()
    if not model:
        raise HTTPException(404, "AI 模型不存在")
    db.delete(model)
    db.commit()
    return {"ok": True}


@router.post("/models/{model_id}/test")
async def test_model(model_id: int, db: Session = Depends(get_db)):
    model = db.query(AIModel).filter(AIModel.id == model_id).first()
    if not model:
        raise HTTPException(404, "AI 模型不存在")

    service = db.query(AIService).filter(AIService.id == model.service_id).first()
    if not service:
        raise HTTPException(400, "关联的服务商不存在")

    try:
        client = AIClient(
            base_url=service.base_url,
            api_key=service.api_key,
            model=model.model,
        )
        # 测试连通性时不下发 temperature:部分模型(如 o1/claude-opus 等)不接受该参数,
        # 省略后对所有模型都安全,避免因 temperature 报错而误判模型不可用。
        reply = await client.chat(
            system_prompt="You are a helpful assistant.",
            user_content="Say 'OK' in one word.",
            temperature=None,
        )
        return {"ok": True, "reply": reply.strip()}
    except Exception as e:
        raise HTTPException(400, f"测试失败: {e}")


@router.post("/services/{service_id}/discover-models")
async def discover_models(service_id: int, db: Session = Depends(get_db)):
    service = db.query(AIService).filter(AIService.id == service_id).first()
    if not service:
        raise HTTPException(404, "AI 服务商不存在")
    try:
        client = AIClient(base_url=service.base_url, api_key=service.api_key)
        models = await client.list_models()
        return {"models": models}
    except Exception as e:
        raise HTTPException(400, f"嗅探失败: {e}")


@router.post("/services/{service_id}/models/batch")
def batch_add_models(service_id: int, body: BatchModelCreate, db: Session = Depends(get_db)):
    service = db.query(AIService).filter(AIService.id == service_id).first()
    if not service:
        raise HTTPException(404, "AI 服务商不存在")

    existing = {m.model for m in service.models}
    added = 0
    for item in body.models:
        if not item.model or item.model in existing:
            continue
        if item.is_default:
            _reset_default_models(db)
        db.add(AIModel(
            name=item.name or item.model,
            service_id=service_id,
            model=item.model,
            is_default=item.is_default,
        ))
        existing.add(item.model)
        added += 1
    db.commit()
    return {"added": added}
