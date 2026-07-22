import os
import time

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from src.web.database import DB_PATH

from src.web.api import (
    stocks,
    agents,
    settings,
    logs,
    providers,
    channels,
    datasources,
    accounts,
    history,
    news,
    market,
    auth,
    suggestions,
    quotes,
    klines,
    templates,
    feedback,
    discovery,
    price_alerts,
    context,
    recommendations,
    dashboard,
    paper_trading,
    chat,
    playbooks,
)
from src.web.api import factors
from src.web.api import health
from src.web.api import insights
from src.web.api.auth import get_current_user
from src.web.api.settings import get_app_version
from src.web.response import ResponseWrapperMiddleware
from src.web.tenant_context import TenantContextMiddleware, single_tenant_mode
from src.core.report_link import verify_report_signature

app = FastAPI(
    title="PanWatch API",
    version="0.1.0",
    redirect_slashes=False,  # 避免重定向丢失 Authorization header
)

app.add_middleware(ResponseWrapperMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MT-P1 身份穿透：每请求强制清空租户 contextvar（防 anyio 线程复用串租户）。
# 最后 add_middleware = 最外层，确保先于 ResponseWrapper/CORS 执行清理。
app.add_middleware(TenantContextMiddleware)

# 认证路由（无需登录）
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
# 市场指数（公共数据，无需登录）
app.include_router(market.router, prefix="/api/market", tags=["market"])

# 需要登录的路由
protected = [Depends(get_current_user)]
app.include_router(
    stocks.router, prefix="/api/stocks", tags=["stocks"], dependencies=protected
)
app.include_router(
    quotes.router, prefix="/api/quotes", tags=["quotes"], dependencies=protected
)
app.include_router(
    klines.router, prefix="/api/klines", tags=["klines"], dependencies=protected
)
app.include_router(
    insights.router, prefix="/api/insights", tags=["insights"], dependencies=protected
)
app.include_router(
    accounts.router, prefix="/api", tags=["accounts"], dependencies=protected
)
app.include_router(
    agents.router, prefix="/api/agents", tags=["agents"], dependencies=protected
)
app.include_router(
    providers.router,
    prefix="/api/providers",
    tags=["providers"],
    dependencies=protected,
)
app.include_router(
    channels.router, prefix="/api/channels", tags=["channels"], dependencies=protected
)
app.include_router(
    datasources.router,
    prefix="/api/datasources",
    tags=["datasources"],
    dependencies=protected,
)
app.include_router(
    settings.router, prefix="/api/settings", tags=["settings"], dependencies=protected
)
app.include_router(
    logs.router, prefix="/api/logs", tags=["logs"], dependencies=protected
)
app.include_router(
    history.router, prefix="/api", tags=["history"], dependencies=protected
)
app.include_router(
    context.router, prefix="/api", tags=["context"], dependencies=protected
)
app.include_router(
    news.router, prefix="/api/news", tags=["news"], dependencies=protected
)
app.include_router(
    suggestions.router,
    prefix="/api/suggestions",
    tags=["suggestions"],
    dependencies=protected,
)
app.include_router(
    templates.router,
    prefix="/api/templates",
    tags=["templates"],
    dependencies=protected,
)
app.include_router(
    feedback.router,
    prefix="/api/feedback",
    tags=["feedback"],
    dependencies=protected,
)

app.include_router(
    discovery.router,
    prefix="/api/discovery",
    tags=["discovery"],
    dependencies=protected,
)
app.include_router(
    price_alerts.router,
    prefix="/api/price-alerts",
    tags=["price-alerts"],
    dependencies=protected,
)
app.include_router(
    recommendations.router,
    prefix="/api/recommendations",
    tags=["recommendations"],
    dependencies=protected,
)
app.include_router(
    dashboard.router,
    prefix="/api/dashboard",
    tags=["dashboard"],
    dependencies=protected,
)
app.include_router(
    factors.router,
    prefix="/api/factors",
    tags=["factors"],
    dependencies=protected,
)
app.include_router(
    health.router,
    prefix="/api/health",
    tags=["health"],
    dependencies=protected,
)
app.include_router(
    paper_trading.router,
    prefix="/api/paper-trading",
    tags=["paper-trading"],
    dependencies=protected,
)
app.include_router(
    chat.router,
    prefix="/api/chat",
    tags=["chat"],
    dependencies=protected,
)
app.include_router(
    playbooks.router,
    prefix="/api",
    tags=["playbooks"],
    dependencies=protected,
)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/version")
async def version():
    """获取应用版本号（公开接口）"""
    return {"version": get_app_version()}


# ---------------------------------------------------------------------------
# 研究报告下载（P4）：必须避开 /api/ 前缀——ResponseWrapper 会整体缓冲大响应。
# 鉴权（MT-P4，docs/25 §6）：HMAC 签名 URL，不加登录态——
#   GET /reports/{tenant_id}/{filename}?exp=<unix_ts>&sig=<hex>
#   sig = HMAC_SHA256(key=jwt_secret, msg=f"{tenant_id}|{filename}|{exp}")
# exp 过期 → 410；sig 不匹配 → 403。单租户直通（PANWATCH_SINGLE_TENANT=1）下
# 旧形态无签名 /reports/{filename} 按 tenant=1 放行（存量通知外链不断）；
# 多租户模式下旧无签名链接一次性失效（不设兼容期，docs/25 §6.2 已裁决）。
# ---------------------------------------------------------------------------
REPORTS_DIR = os.path.join(os.path.dirname(DB_PATH), "reports")


def _serve_report_file(filename: str) -> FileResponse:
    """从 data/reports/ 取报告文件（防穿越：basename + realpath 双保险）。"""
    if (
        not filename
        or filename in (".", "..")
        or ".." in filename
        or "/" in filename
        or "\\" in filename
        or os.path.basename(filename) != filename
    ):
        raise HTTPException(status_code=400, detail="非法文件名")
    file_path = os.path.join(REPORTS_DIR, filename)
    # 双保险：解析后的真实路径必须仍在 reports 目录内
    if not os.path.realpath(file_path).startswith(os.path.realpath(REPORTS_DIR)):
        raise HTTPException(status_code=400, detail="非法文件名")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(file_path, filename=filename)


@app.get("/reports/{tenant_id}/{filename}")
async def download_report_signed(
    tenant_id: int, filename: str, exp: str = "", sig: str = ""
):
    """签名下载（docs/25 §6）：先验签（失败 403），再验期（过期 410）。

    签名绑定 tenant_id + filename + exp，跨租户挪用他租户签名即 sig 不匹配。
    有效期 7 天；过期后用户登录系统从报告页重取新签名链接。
    """
    if not verify_report_signature(tenant_id, filename, exp, sig):
        raise HTTPException(status_code=403, detail="签名无效或链接已被篡改")
    if int(exp) < int(time.time()):
        raise HTTPException(status_code=410, detail="签名链接已过期，请重新获取")
    return _serve_report_file(filename)


@app.get("/reports/{filename}")
async def download_report(filename: str):
    """旧形态无签名下载（单租户直通专用）。

    单租户模式（PANWATCH_SINGLE_TENANT=1）：按 tenant=1 放行，行为等价现状；
    多租户模式：403，必须使用 /reports/{tenant_id}/{filename} 签名链接。
    """
    if not single_tenant_mode():
        raise HTTPException(status_code=403, detail="报告下载需使用带签名的链接")
    return _serve_report_file(filename)
