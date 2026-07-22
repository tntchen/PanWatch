"""MT-P1 身份骨架 Bootstrap：默认租户 + 初始管理员的幂等初始化。

设计依据：
- docs/21-MT-P0-schema变更清单.md §2（tenants/users DDL + 初始管理员回填伪代码）
- docs/25-MT-P0-身份穿透设计.md §2.2/§2.3（bcrypt 透明重哈希、env 凭证迁移）
- docs/17 v1.1（T18 默认租户 id=1、T20 凭证优先级与回退语义）

凭证优先级（T20）：
1. env AUTH_USERNAME / AUTH_PASSWORD（明文密码直接 bcrypt 入库；bcrypt 不可用时
   降级 sha256_legacy，首次登录透明重哈希兜底）
2. app_settings 的 auth_username / auth_password_hash（sha256 原样搬入并标记
   password_algo='sha256_legacy'，待首次登录透明重哈希；本阶段不删除旧 KV，
   保持 MT-P1 行为等价单用户，清理由 MT-P2 v120 迁移负责）
3. 两者皆无 → 生成随机密码并 logger.warning 打印一次

幂等性：users 表非空即跳过管理员创建（"已有账号不覆盖"守卫）；
默认租户按主键 id=1 存在性判断，重复调用安全。
"""

import hashlib
import logging
import os
import secrets
from datetime import datetime

from sqlalchemy.orm import Session

from src.web.models import AppSettings, Tenant, User

logger = logging.getLogger(__name__)

DEFAULT_TENANT_ID = 1
DEFAULT_TENANT_NAME = "默认租户"
DEFAULT_ADMIN_USERNAME = "admin"

_AUTH_USERNAME_KEY = "auth_username"
_AUTH_PASSWORD_HASH_KEY = "auth_password_hash"

try:
    import bcrypt as _bcrypt
except ImportError:  # requirements 尚未引入 bcrypt 时降级（docs/25 §2.2 F15）
    _bcrypt = None


def _hash_bcrypt(password: str) -> str | None:
    """bcrypt 哈希；bcrypt 未安装时返回 None 让调用方降级。"""
    if _bcrypt is None:
        return None
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def _hash_sha256_legacy(password: str) -> str:
    """旧格式哈希（与 src/web/api/auth.py hash_password 口径一致）。"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _get_setting(db: Session, key: str) -> str:
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    return row.value if row and row.value else ""


def _resolve_admin_credentials(db: Session) -> tuple[str, str, str]:
    """按 T20 优先级解析初始管理员 (username, password_hash, password_algo)。"""
    username = (
        os.getenv("AUTH_USERNAME")
        or _get_setting(db, _AUTH_USERNAME_KEY)
        or DEFAULT_ADMIN_USERNAME
    )

    env_password = os.getenv("AUTH_PASSWORD")
    if env_password:
        hashed = _hash_bcrypt(env_password)
        if hashed is not None:
            return username, hashed, "bcrypt"
        logger.warning(
            "bcrypt 未安装，env AUTH_PASSWORD 暂以 sha256_legacy 入库，"
            "首次登录将透明重哈希为 bcrypt"
        )
        return username, _hash_sha256_legacy(env_password), "sha256_legacy"

    legacy_hash = _get_setting(db, _AUTH_PASSWORD_HASH_KEY)
    if legacy_hash:
        # sha256 哈希原样搬入，标记 legacy，首次登录透明重哈希（M10）
        return username, legacy_hash, "sha256_legacy"

    random_password = secrets.token_urlsafe(12)
    hashed = _hash_bcrypt(random_password)
    if hashed is None:
        hashed = _hash_sha256_legacy(random_password)
        algo = "sha256_legacy"
    else:
        algo = "bcrypt"
    logger.warning(
        "未配置任何管理员凭证（env AUTH_USERNAME/AUTH_PASSWORD 与 "
        "app_settings auth_* 均为空），已生成随机初始密码（仅打印一次）: %s",
        random_password,
    )
    return username, hashed, algo


def ensure_default_tenant_and_admin(db: Session) -> None:
    """确保默认租户(id=1)与初始管理员存在。幂等，可重复调用。

    - 默认租户必须保证 id=1（MT-P2 全部私有表 tenant_id DEFAULT 1，T18）。
    - users 表非空时跳过管理员创建，不覆盖任何已有账号。
    """
    tenant = db.get(Tenant, DEFAULT_TENANT_ID)
    if tenant is None:
        db.add(
            Tenant(
                id=DEFAULT_TENANT_ID,
                name=DEFAULT_TENANT_NAME,
                is_default=True,
            )
        )
        db.flush()
        logger.info("已创建默认租户 id=%d (%s)", DEFAULT_TENANT_ID, DEFAULT_TENANT_NAME)

    existing_user = db.query(User.id).limit(1).first()
    if existing_user is not None:
        db.commit()
        return

    username, password_hash, password_algo = _resolve_admin_credentials(db)
    admin = User(
        tenant_id=DEFAULT_TENANT_ID,
        username=username,
        password_hash=password_hash,
        password_algo=password_algo,
        role="admin",
        quota_shared_with_admin=True,  # T13：初始管理员默认共享管理员配额
        is_active=True,
        pwd_changed_at=datetime.now(),
    )
    db.add(admin)
    db.commit()
    logger.info(
        "已创建初始管理员 username=%r (tenant_id=%d, algo=%s)",
        username,
        DEFAULT_TENANT_ID,
        password_algo,
    )
