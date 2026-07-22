"""认证 API - MT-P1 多租户身份骨架

- users/tenants 表为唯一身份源（app_settings 的 auth_* 旧凭证仅用于一次性引导迁移）
- JWT claims = {sub, tenant_id, role, pwd_at, iat, exp}，必填 claim 缺失即 401（旧 token 一次性踢出，T20）
- bcrypt 哈希；旧 SHA-256 凭据校验通过后透明重哈希写回（以 $2 前缀判别 bcrypt）
- get_current_user 实时查库（T5），并通过 contextvar 设置当前租户上下文
"""
import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.web.database import SessionLocal, get_db
from src.web.models import AppSettings

try:  # bcrypt 依赖由部署环境提供（requirements 需含 bcrypt）
    import bcrypt
except ImportError:  # pragma: no cover - 环境缺依赖时显式报错而非静默
    bcrypt = None  # type: ignore[assignment]

try:  # User/Tenant 模型由数据层（models.py）提供；未就绪时降级，保证模块可独立导入
    from src.web.models import Tenant, User
except ImportError:  # pragma: no cover
    Tenant = None  # type: ignore[assignment]
    User = None  # type: ignore[assignment]

try:  # 租户上下文（并行开发模块，签名固定）；缺失时降级为 no-op
    from src.web.tenant_context import (
        TenantCtx,
        reset_current_tenant,
        set_current_tenant,
    )
except ImportError:  # pragma: no cover

    @dataclass
    class TenantCtx:  # type: ignore[no-redef]
        tenant_id: int
        user_id: int
        role: str

    def set_current_tenant(ctx: "TenantCtx") -> Any:  # type: ignore[no-redef]
        return None

    def reset_current_tenant(token: Any) -> None:  # type: ignore[no-redef]
        return None


router = APIRouter()
security = HTTPBearer(auto_error=False)

# JWT 配置
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30
JWT_REQUIRED_CLAIMS = ["sub", "tenant_id", "role", "pwd_at", "iat", "exp"]

# 环境变量配置（Docker 部署用）
ENV_AUTH_USERNAME = os.getenv("AUTH_USERNAME")
ENV_AUTH_PASSWORD = os.getenv("AUTH_PASSWORD")

# 设置项 key（jwt_secret 实例级，留在 app_settings，T20）
JWT_SECRET_KEY = "jwt_secret"
# 旧版单用户凭据 key（仅用于一次性引导迁移进 users 表）
LEGACY_USERNAME_KEY = "auth_username"
LEGACY_PASSWORD_HASH_KEY = "auth_password_hash"

DEFAULT_TENANT_ID = 1
DEFAULT_TENANT_NAME = "默认租户"
VALID_ROLES = ("admin", "user")

# JWT Secret 缓存
_jwt_secret: Optional[str] = None


def is_single_tenant() -> bool:
    """单租户直通开关（T20 回退 flag，MT-P1 默认开启）"""
    return os.getenv("PANWATCH_SINGLE_TENANT", "1") == "1"


def get_jwt_secret() -> str:
    """获取 JWT Secret（持久化到数据库，实例级）"""
    global _jwt_secret
    if _jwt_secret:
        return _jwt_secret

    if os.getenv("JWT_SECRET"):
        _jwt_secret = os.getenv("JWT_SECRET")
        assert _jwt_secret is not None
        return _jwt_secret

    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == JWT_SECRET_KEY).first()
        if setting:
            _jwt_secret = setting.value
        else:
            _jwt_secret = secrets.token_hex(32)
            db.add(
                AppSettings(
                    key=JWT_SECRET_KEY,
                    value=_jwt_secret,
                    description="JWT签名密钥(自动生成)",
                )
            )
            db.commit()
        return _jwt_secret
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 密码哈希（bcrypt + 旧 SHA-256 透明重哈希）
# ---------------------------------------------------------------------------


def _require_bcrypt() -> None:
    if bcrypt is None:
        raise HTTPException(500, "服务器缺少 bcrypt 依赖，无法处理密码")


def hash_password(password: str) -> str:
    """bcrypt 哈希（$2b$ 前缀）"""
    _require_bcrypt()
    raw = password.encode("utf-8")
    if len(raw) > 72:
        raise HTTPException(400, "密码过长（bcrypt 上限 72 字节）")
    assert bcrypt is not None
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def _sha256_hex(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, stored_hash: str) -> bool:
    """校验密码。bcrypt 以 $2 前缀判别；64 位 hex 按旧 SHA-256 处理。"""
    if stored_hash.startswith("$2"):
        _require_bcrypt()
        assert bcrypt is not None
        try:
            return bool(
                bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("ascii"))
            )
        except ValueError:
            return False
    if len(stored_hash) == 64 and all(c in "0123456789abcdef" for c in stored_hash):
        return _sha256_hex(password) == stored_hash
    return False


def _is_legacy_hash(stored_hash: str) -> bool:
    return not stored_hash.startswith("$2")


# ---------------------------------------------------------------------------
# JWT 签发 / 校验
# ---------------------------------------------------------------------------


def _pwd_ts(pwd_changed_at: Optional[datetime]) -> int:
    if pwd_changed_at is None:
        return 0
    if pwd_changed_at.tzinfo is None:
        pwd_changed_at = pwd_changed_at.replace(tzinfo=timezone.utc)
    return int(pwd_changed_at.timestamp())


def create_token(user: "User", expires_days: int = JWT_EXPIRE_DAYS) -> tuple[str, datetime]:
    """按 MT-P1 claims 契约签发 JWT"""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=expires_days)
    payload = {
        "sub": str(user.id),
        "tenant_id": int(user.tenant_id),
        "role": user.role,
        "pwd_at": _pwd_ts(user.pwd_changed_at),
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return token, expires_at


def decode_token(token: str) -> Optional[dict]:
    """解码并强制校验必填 claim（缺失即拒绝 → 旧 token 一次性踢出，T20）"""
    try:
        return jwt.decode(
            token,
            get_jwt_secret(),
            algorithms=[JWT_ALGORITHM],
            options={"require": JWT_REQUIRED_CLAIMS},
        )
    except jwt.InvalidTokenError:
        return None


# ---------------------------------------------------------------------------
# 用户/租户引导（env 凭证 + 旧 app_settings 凭证一次性迁移）
# ---------------------------------------------------------------------------


def _require_models() -> None:
    if User is None or Tenant is None:
        raise HTTPException(503, "用户模型未就绪（models 未提供 User/Tenant）")


def _ensure_default_tenant(db: Session) -> "Tenant":
    tenant = db.get(Tenant, DEFAULT_TENANT_ID)
    if tenant is None:
        tenant = Tenant(id=DEFAULT_TENANT_ID, name=DEFAULT_TENANT_NAME)
        db.add(tenant)
        db.flush()
    return tenant


def _new_user(
    *,
    tenant_id: int,
    username: str,
    password_hash: str,
    role: str,
    quota_shared_with_admin: bool,
    invited_by: Optional[int] = None,
) -> "User":
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return User(
        tenant_id=tenant_id,
        username=username,
        password_hash=password_hash,
        role=role,
        quota_shared_with_admin=1 if quota_shared_with_admin else 0,
        is_active=1,
        invited_by=invited_by,
        pwd_changed_at=now,
    )


def bootstrap_initial_admin(db: Session) -> bool:
    """users 空表时引导初始管理员：env 凭证优先，其次旧 app_settings 凭据（原样搬入待透明重哈希）。

    Returns:
        True 表示本次创建了初始管理员。
    """
    _require_models()
    if db.query(User).count() > 0:
        return False

    username: Optional[str] = None
    password_hash: Optional[str] = None

    if ENV_AUTH_USERNAME and ENV_AUTH_PASSWORD:
        username = ENV_AUTH_USERNAME
        password_hash = hash_password(ENV_AUTH_PASSWORD)
    else:
        legacy_user = (
            db.query(AppSettings).filter(AppSettings.key == LEGACY_USERNAME_KEY).first()
        )
        legacy_hash = (
            db.query(AppSettings)
            .filter(AppSettings.key == LEGACY_PASSWORD_HASH_KEY)
            .first()
        )
        if legacy_user and legacy_hash and legacy_hash.value:
            username = legacy_user.value
            password_hash = legacy_hash.value  # sha256_legacy，首次登录透明重哈希

    if not username or not password_hash:
        return False

    tenant = _ensure_default_tenant(db)
    db.add(
        _new_user(
            tenant_id=tenant.id,
            username=username,
            password_hash=password_hash,
            role="admin",
            quota_shared_with_admin=True,
        )
    )
    db.commit()
    return True


def init_auth_from_env(db: Session) -> bool:
    """从环境变量初始化认证（Docker 部署用）。保持 server.py 现有调用点签名。

    Returns:
        True if initialized from env, False otherwise
    """
    if not ENV_AUTH_USERNAME or not ENV_AUTH_PASSWORD:
        return False
    if User is None or Tenant is None:
        return False
    if db.query(User).count() > 0:
        return False
    return bootstrap_initial_admin(db)


# ---------------------------------------------------------------------------
# 依赖：当前用户 / 管理员
# ---------------------------------------------------------------------------


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> AsyncGenerator["User", None]:
    """验证当前用户（yield 依赖）：实时查库（T5），并设置/复位租户上下文。

    必须是 async generator：sync generator 依赖会被 FastAPI 扔进 threadpool，
    enter/exit 落在不同 context，导致 contextvar Token 跨 context reset 报错、
    且租户上下文无法传播到 handler；async generator 的 enter/exit 均在请求
    任务的同一 context 内，set/reset 配对安全。
    """
    _require_models()

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = decode_token(credentials.credentials)
    if claims is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 改密吊销：token 内 pwd_at 与 users.pwd_changed_at 不一致即 401
    if int(claims["pwd_at"]) != _pwd_ts(user.pwd_changed_at):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    tenant_token = set_current_tenant(
        TenantCtx(tenant_id=user.tenant_id, user_id=user.id, role=user.role)
    )
    try:
        yield user
    finally:
        reset_current_tenant(tenant_token)


def require_admin(user: "User" = Depends(get_current_user)) -> "User":
    """管理员判定：role 实时查库（T5），非管理员 403"""
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class SetupRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"
    tenant_name: Optional[str] = None
    quota_shared_with_admin: bool = False


class PatchUserRequest(BaseModel):
    is_active: Optional[bool] = None
    role: Optional[str] = None
    quota_shared_with_admin: Optional[bool] = None
    reset_password: Optional[str] = None


def _tenant_name(db: Session, tenant_id: int) -> str:
    tenant = db.get(Tenant, tenant_id)
    return tenant.name if tenant else ""


def _user_payload(db: Session, user: "User") -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "tenant_id": user.tenant_id,
        "tenant_name": _tenant_name(db, user.tenant_id),
    }


def _validate_username(username: str) -> None:
    if not username or len(username) < 2:
        raise HTTPException(400, "用户名长度至少 2 位")


def _validate_password(password: str) -> None:
    if len(password) < 6:
        raise HTTPException(400, "密码长度至少 6 位")


def _validate_role(role: str) -> None:
    if role not in VALID_ROLES:
        raise HTTPException(400, "role 必须是 admin 或 user")


# ---------------------------------------------------------------------------
# 公开端点
# ---------------------------------------------------------------------------


@router.get("/status")
async def auth_status(db: Session = Depends(get_db)):
    """获取认证状态"""
    if User is None:
        return {"initialized": False}
    bootstrap_initial_admin(db)
    return {"initialized": db.query(User).count() > 0}


@router.post("/setup")
async def setup_password(data: SetupRequest, db: Session = Depends(get_db)):
    """首次设置：仅 users 空表可用，创建初始管理员（tenant=1）"""
    _require_models()
    bootstrap_initial_admin(db)  # env/旧凭据优先占用 bootstrap 名额
    if db.query(User).count() > 0:
        raise HTTPException(400, "已设置过账号，请使用登录接口")

    _validate_username(data.username)
    _validate_password(data.password)

    tenant = _ensure_default_tenant(db)
    user = _new_user(
        tenant_id=tenant.id,
        username=data.username,
        password_hash=hash_password(data.password),
        role="admin",
        quota_shared_with_admin=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token, expires_at = create_token(user)
    return {
        "token": token,
        "expires_at": expires_at.isoformat(),
        "user": _user_payload(db, user),
    }


@router.post("/login")
async def login(data: LoginRequest, db: Session = Depends(get_db)):
    """登录"""
    _require_models()
    bootstrap_initial_admin(db)

    user = db.query(User).filter(User.username == data.username).first()
    if user is None:
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已被禁用")

    if not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")

    # 旧 SHA-256 凭据校验通过 → 透明重哈希为 bcrypt 并更新 pwd_changed_at（T20）
    if _is_legacy_hash(user.password_hash):
        user.password_hash = hash_password(data.password)
        user.pwd_changed_at = datetime.now(timezone.utc).replace(tzinfo=None)

    user.last_login_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.refresh(user)

    token, expires_at = create_token(user)
    return {
        "token": token,
        "expires_at": expires_at.isoformat(),
        "user": _user_payload(db, user),
    }


# ---------------------------------------------------------------------------
# 登录用户端点
# ---------------------------------------------------------------------------


@router.post("/change-password")
async def change_password(
    data: ChangePasswordRequest,
    db: Session = Depends(get_db),
    user: "User" = Depends(get_current_user),
):
    """修改密码（必须校验旧密码，J10）；成功后旧 token 全部失效（pwd_at 吊销）"""
    _validate_password(data.new_password)

    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(400, "旧密码错误")

    user.password_hash = hash_password(data.new_password)
    user.pwd_changed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()

    return {"message": "密码已更新"}


@router.get("/me")
async def get_me(
    db: Session = Depends(get_db),
    user: "User" = Depends(get_current_user),
):
    """获取当前用户信息（role 以库实时值为准，T5）"""
    return {
        **_user_payload(db, user),
        "quota_shared_with_admin": bool(user.quota_shared_with_admin),
    }


# ---------------------------------------------------------------------------
# 管理员端点（T12 邀请制，无公开注册）
# ---------------------------------------------------------------------------


@router.get("/users")
async def list_users(
    db: Session = Depends(get_db),
    _: "User" = Depends(require_admin),
):
    """用户列表（管理员）"""
    users = db.query(User).order_by(User.id).all()
    return {
        "users": [
            {
                **_user_payload(db, u),
                "is_active": bool(u.is_active),
                "quota_shared_with_admin": bool(u.quota_shared_with_admin),
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ]
    }


@router.post("/users")
async def create_user(
    data: CreateUserRequest,
    db: Session = Depends(get_db),
    admin: "User" = Depends(require_admin),
):
    """邀请创建用户（管理员）。tenant_name 不存在则新建租户（T12）；单租户模式禁止新建租户。"""
    _validate_username(data.username)
    _validate_password(data.password)
    _validate_role(data.role)

    if db.query(User).filter(User.username == data.username).first():
        raise HTTPException(400, "用户名已存在")

    if data.tenant_name:
        tenant = db.query(Tenant).filter(Tenant.name == data.tenant_name).first()
        if tenant is None:
            if is_single_tenant():
                raise HTTPException(400, "单租户模式下不可创建新租户")
            tenant = Tenant(name=data.tenant_name)
            db.add(tenant)
            db.flush()
    else:
        tenant = db.get(Tenant, admin.tenant_id)
        if tenant is None:
            tenant = _ensure_default_tenant(db)

    user = _new_user(
        tenant_id=tenant.id,
        username=data.username,
        password_hash=hash_password(data.password),
        role=data.role,
        quota_shared_with_admin=data.quota_shared_with_admin,
        invited_by=admin.id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return {
        "user": {
            **_user_payload(db, user),
            "is_active": bool(user.is_active),
            "quota_shared_with_admin": bool(user.quota_shared_with_admin),
        }
    }


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: int,
    data: PatchUserRequest,
    db: Session = Depends(get_db),
    admin: "User" = Depends(require_admin),
):
    """更新用户（管理员）：启用/禁用、角色、配额共享、重置密码"""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")

    if data.role is not None:
        _validate_role(data.role)

    demoting = data.role is not None and data.role != "admin" and target.role == "admin"
    deactivating = data.is_active is not None and not data.is_active and target.is_active

    if target.id == admin.id and (demoting or deactivating):
        raise HTTPException(400, "不能降级或禁用当前登录的管理员账号")

    if demoting or deactivating:
        other_admins = (
            db.query(User)
            .filter(
                User.id != target.id,
                User.role == "admin",
                User.is_active == 1,
            )
            .count()
        )
        if target.role == "admin" and target.is_active and other_admins == 0:
            raise HTTPException(400, "至少保留一名启用状态的管理员")

    if data.is_active is not None:
        target.is_active = 1 if data.is_active else 0
    if data.role is not None:
        target.role = data.role
    if data.quota_shared_with_admin is not None:
        target.quota_shared_with_admin = 1 if data.quota_shared_with_admin else 0
    if data.reset_password:
        _validate_password(data.reset_password)
        target.password_hash = hash_password(data.reset_password)
        target.pwd_changed_at = datetime.now(timezone.utc).replace(tzinfo=None)

    db.commit()
    db.refresh(target)

    return {
        "user": {
            **_user_payload(db, target),
            "is_active": bool(target.is_active),
            "quota_shared_with_admin": bool(target.quota_shared_with_admin),
        }
    }
