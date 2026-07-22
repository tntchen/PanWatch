import os

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
from src.web.tenant_context import TenantContextMiddleware

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
# 鉴权与 server.py 的静态文件服务保持一致（不强制登录）。
# ---------------------------------------------------------------------------
REPORTS_DIR = os.path.join(os.path.dirname(DB_PATH), "reports")


@app.get("/reports/{filename}")
async def download_report(filename: str):
    """从 data/reports/ 下载研究报告附件（如 docx）。

    安全：仅允许 basename，拒绝含 ".." / "/" / "\\" 的输入，防路径穿越。
    """
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
