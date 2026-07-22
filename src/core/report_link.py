"""/reports 下载 HMAC 签名 URL（MT-P4，docs/25 §6）。

- URL 形态：``GET /reports/{tenant_id}/{filename}?exp=<unix_ts>&sig=<hex>``
- ``sig = HMAC_SHA256(key=jwt_secret, msg=f"{tenant_id}|{filename}|{exp}")``
- 有效期默认 7 天（报告是时点产物，覆盖通知点击窗口）；
  exp 过期 → 410，sig 不匹配 → 403（校验在 src/web/app.py 路由侧）。
- 签名 key 复用实例级 ``jwt_secret``（src/web/api/auth.py ``get_jwt_secret``），
  不引入新密钥管理面（T20）。
- 单租户回退：``PANWATCH_SINGLE_TENANT=1`` 下旧形态无签名 ``/reports/{filename}``
  按 tenant=1 放行（存量通知外链不断）；该回退在路由侧实现，本模块只做签名。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote

from src.core import analysis_link
from src.web.api.auth import get_jwt_secret

# 签名链接有效期：7 天（docs/25 §6.1）
REPORT_URL_TTL_SECONDS = 7 * 86400


def _sign(tenant_id: int, filename: str, exp: int) -> str:
    """计算 HMAC-SHA256 签名（hex）。msg 绑定 tenant_id + filename + exp。"""
    msg = f"{tenant_id}|{filename}|{exp}".encode("utf-8")
    return hmac.new(
        get_jwt_secret().encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()


def make_signed_report_url(
    tenant_id: int,
    filename: str,
    base_url: str = "",
    exp_seconds: int = REPORT_URL_TTL_SECONDS,
) -> str:
    """生成带签名的报告下载 URL。

    ``base_url`` 缺省走 ``analysis_link.get_base_url()``（全局设置
    panwatch_base_url）；未配置时返回相对路径（调用方自行决定是否外发）。
    """
    exp = int(time.time()) + int(exp_seconds)
    sig = _sign(tenant_id, filename, exp)
    path = f"/reports/{tenant_id}/{quote(filename)}?exp={exp}&sig={sig}"
    if not base_url:
        base_url = analysis_link.get_base_url()
    base = base_url.rstrip("/")
    return f"{base}{path}" if base else path


def verify_report_signature(
    tenant_id: int, filename: str, exp: object, sig: object
) -> bool:
    """校验签名（hmac.compare_digest 防时序侧信道）。

    仅验签不验期；过期判定（410）由路由侧负责，以便与 403 区分。
    exp/sig 缺失或 exp 非整数 → False。
    """
    try:
        exp_int = int(str(exp))
    except (TypeError, ValueError):
        return False
    if not sig:
        return False
    expected = _sign(tenant_id, filename, exp_int)
    return hmac.compare_digest(expected, str(sig))
