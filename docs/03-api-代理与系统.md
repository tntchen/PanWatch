# PanWatch 源码阅读 · 03 — 代理 / 系统 / 设置类 API 路由

> 范围：`src/web/api/` 下的 agents、chat、channels、history、settings、providers、auth、logs、health、feedback、templates、suggestions、recommendations、price_alerts、paper_trading、factors、datasources 共 17 个文件。
> 面向二次开发者，只写已确认的事实。

## 一、模块职责一句话总结

这组路由是 PanWatch 的"管理平面"：Agent 调度配置与手动触发、AI 对话、AI 服务商/模型管理、通知渠道、数据源、全局设置、单用户认证、日志中心、分析历史、建议池与反馈、配置包导入导出、价格提醒、模拟盘、策略/因子权重等系统级 CRUD 与运维接口。

## 二、路由注册点（src/web/app.py）

- 除 `auth`（`/api/auth`）与 `market` 外，**所有本组路由都挂在 `protected = [Depends(get_current_user)]` 下**，即认证生效后需 Bearer Token。（多租户，2026-07：旧"未设置密码直接放行"行为已废除——启动时 bootstrap 必引导初始管理员，见「四·3」。）
- 前缀映射：`/api/agents`、`/api/chat`、`/api/channels`、`/api/history`（注意：history.router 自带 `prefix="/history"`，app 里再加 `/api`）、`/api/settings`、`/api/providers`、`/api/logs`、`/api/health`（自检）+ 顶层 `@app.get("/api/health")`（裸活探针，无鉴权）、`/api/feedback`、`/api/templates`、`/api/suggestions`、`/api/recommendations`、`/api/price-alerts`、`/api/paper-trading`、`/api/factors`、`/api/datasources`。
- 全局 `ResponseWrapperMiddleware` 会把 JSON 响应包成 `{code,data,message}`；非 JSON（如 PDF 导出）原样放行。factors.py、datasources.py 中有针对该中间件的注释/规避（test 端点不用 `success`/`data` 作顶层字段，见 `src/web/response.py:59` 特殊分支）。

## 三、关键文件清单

| 文件 | 职责 |
|---|---|
| `agents.py`（1167 行，最大） | Agent 配置 CRUD、调度健康概览、schedule 预览、手动触发、TradingAgents 深度分析的运行状态/进度/结果/预算/PDF 导出、盘中扫描 `POST /intraday/scan` |
| `chat.py` | AI 对话：会话 CRUD + 发消息（带 5 个 tool-call 工具的循环，最多 5 轮） |
| `channels.py` | 通知渠道 CRUD + 类型列表 + 测试发送 |
| `history.py` | `AnalysisHistory` 分析历史列表/详情/删除，按 agent_kind 过滤 |
| `settings.py` | 全局设置（KV）、头像上传（data URL→文件）、版本号、更新检查 |
| `providers.py` | AI 服务商（AIService）与模型（AIModel）CRUD、模型连通性测试、`/discover-models` 嗅探、批量加模型 |
| `auth.py` | 单用户 JWT 认证：setup/login/change-password/me/status |
| `logs.py` | 日志中心：`LogEntry` 多条件查询（cursor/offset 双分页）、清空、聚合 meta、写入健康 |
| `health.py` | 系统自检 `GET /api/health/selfcheck`（数据源/AI/通知逐项探测） |
| `feedback.py` | 建议反馈（有用/无用）提交 + 按天/按 agent 统计 |
| `templates.py` | 配置包导出/导入（settings + agents + stocks + stock_agents，merge/replace） |
| `suggestions.py` | 建议池读取（按股/最新/批量）+ 过期清理 |
| `recommendations.py` | 入场候选榜 + 策略信号/市场状态/风险快照/权重再平衡/因子 IC 等策略引擎 API（后台线程刷新） |
| `price_alerts.py` | 价格提醒规则 CRUD、命中记录、测试/手动扫描 |
| `paper_trading.py` | 模拟盘账户/持仓/成交/指标/诊断、手动平仓/扫描、跟单通知设置 |
| `factors.py` | 因子权重只读列表 + 手动覆盖（weight/pin/auto_calibrate） |
| `datasources.py` | 数据源 CRUD、健康快照、孤儿判定、reset-to-seed 对账、连通性测试 |

## 四、核心机制与数据流

### 1. 存储位置

- **SQLite（SQLAlchemy，`src/web/models.py`）**：`AgentConfig`/`AgentRun`、`AnalysisHistory`、`LogEntry`、`AppSettings`（KV 设置，含 jwt_secret、auth_*、模拟盘通知配置、头像文件名）、`AIService`/`AIModel`、`NotifyChannel`、`DataSource`、`Stock`/`StockAgent`、`StockSuggestion`/`SuggestionFeedback`、`PriceAlertRule`/`PriceAlertHit`、`PaperTradingAccount/Position/Trade`、`StrategySignalRun`、`ChatConversation`/`ChatMessage`。
- **文件系统**：头像图片存 `DATA_DIR/avatars/avatar.*`（DB 只存文件名）；版本号读 `VERSION` 文件或 `APP_VERSION` 环境变量。
- **内存**：agents.py 的盘中扫描缓存 `_SCAN_CACHE`（TTL 12s/25s，线程锁保护）；auth.py 的 `_jwt_secret` 缓存；recommendations.py 的 `_refresh_state`（刷新任务状态）。

### 2. Agent 体系关键概念

- Agent 分两类（`src/core/agent_catalog.py`）：`workflow`（定时调度）与 `capability`（仅手动调用，保存时强制 `enabled=False, schedule=""`）。`kind` 为空时用 `infer_agent_kind(name)` 推断。
- 触发：`POST /api/agents/{name}/trigger` 调 `server.trigger_agent()`；`daily_report`/`premarket_outlook` 默认后台线程异步执行（`wait=true` 可同步等待）。
- 调度表达式解析在 `src/core/schedule_parser.py`（`preview_schedule`/`count_runs_within`），时区取 `Settings().app_timezone`。
- TradingAgents 深度分析的状态判断是**后端权威**：用 `log_entries(event=ta_progress, trace_id LIKE %-{symbol}-%)` + `agent_runs` 推断 running/success/failed，5 分钟无新日志判 `stale`（僵尸运行，server 重启或线程死掉场景）。
- 时间约定：DB 里 naive datetime 一律按 UTC 解释，再转 `app_timezone` 输出（各文件重复实现 `_format_datetime`）。

### 3. 认证模型（多租户，2026-07 重写）

- **多用户 + 邀请制**：身份唯一源是 `users`/`tenants` 表（`app_settings` 的 `auth_*` 旧凭据仅用于一次性引导迁移）。无开放注册——`POST /api/auth/setup` 仅在 users 空表时可用，之后只能由管理员邀请创建（T12）。启动时 `src/web/bootstrap.py` 幂等保证默认租户（id=1）+ 初始管理员存在（env `AUTH_USERNAME/PASSWORD` > 旧 app_settings 凭据 > 随机密码）；旧"未设密码完全放行"行为已废除。
- **端点清单**：`GET /status`、`POST /setup`、`POST /login`、`POST /change-password`（校验旧密码）、`GET /me`；用户管理（全部 `require_admin`）：`GET /users`、`POST /users`（邀请创建，可选「与管理员共享 AI 配额」，T13）、`PATCH /users/{user_id}`（启停/角色/重置密码，带防自我降级与保底一名管理员守卫）。
- **JWT**：HS256、30 天，claims = `{sub, tenant_id, role, pwd_at, iat, exp}`，必填 claim 缺失即 401（旧格式 token 一次性踢出，T20）；密钥优先级 env `JWT_SECRET` > DB 自动生成（`secrets.token_hex(32)`，实例级 app_settings）。密码用 bcrypt；旧 SHA-256 凭据校验通过后首次登录透明重哈希。
- `get_current_user` 为 async generator 依赖，**每请求实时查库**（T5）：用户被禁用/删除或 `pwd_at` 与改密时间不匹配即 401；同时把 `TenantCtx(tenant_id, user_id, role)` set 进 contextvar、请求结束 reset（机制见 docs/01「五·补」）。

### 3b. 权限分化与 require_admin（多租户，2026-07）

两级角色（admin/user，T5），`require_admin` 依赖（auth.py）对非管理员返回 403。已加管理员守卫的端点：

- **用户管理**：`GET/POST /api/auth/users`、`PATCH /api/auth/users/{id}`。
- **数据源写**：datasources.py 增/改/删与 reset-to-seed 对账（4 处）。
- **Agent 模板写**：agents.py 模板行 PUT/DELETE（租户 override 走另一路径，不受影响）。
- **设置与日志**：settings.py `PUT /{key}` 白名单——实例级 key（jwt_secret/http_proxy 等）仅管理员可写、未知 key 400；logs.py `DELETE`（清空日志）管理员限定。
- 前端同步分化：`/users` 页 RequireAdmin 守卫（非 admin 见 403 页）；DataSources 页非管理员隐藏优先级/启停/编辑/恢复默认；Agents 页隐藏启停/周期/深度配置（触发/运行状态/绑定保留给全部用户）。

### 3c. /reports HMAC 签名 URL（多租户，2026-07，docs/25 §6）

- 格式：`GET /reports/{tenant_id}/{filename}?exp=<unix_ts>&sig=<hex>`；`sig = HMAC_SHA256(key=jwt_secret, msg="{tenant_id}|{filename}|{exp}")`，`hmac.compare_digest` 防时序。实现 `src/core/report_link.py`，路由在 app.py（刻意避开 /api/ 前缀，防 ResponseWrapper 整体缓冲大文件）。
- 有效期 **7 天**；校验顺序：验签失败 403 → 过期 410 → 文件不存在 404。签名绑定 tenant_id + filename，跨租户挪用他租户链接即 sig 不匹配。
- 单租户放行：旧形态无签名 `/reports/{filename}` 仅在 `PANWATCH_SINGLE_TENANT=1` 下按 tenant=1 放行（存量通知外链不断）；多租户模式下旧链接一次性 403，无兼容期。
- 已知限制：reports 目录未按租户物理拆分（签名绑 tenant+filename 兜底）；`make_signed_report_url` 生成侧尚未接入通知链接（后续阶段）。

### 3d. 凭证可见性规则（T3/T13/T21，多租户，2026-07）

- providers.py / channels.py 列表按**可见集**返回：本租户行明文 + 管理员托管行**掩码**（`api_key=""` / `config={}`，密钥不出网），响应带 `tenant_id`/`is_managed`/`is_shared` 标志；机制点上用 `_unscoped_read()` 短开上下文读托管行（do_orm_execute 会挡 tenant=1 行）。
- **AI 配额两态**（T13）：创建用户时管理员可选「与管理员共享配额」——共享租户见托管服务只读卡片（运行时 resolve 可用，test/discover 404）；不共享租户须自建自定义模型，其 ai_services 行租户私有、密钥仅本租户可见。运行时 env key 回退（`_build_ai_client`）仅限管理员租户（F6），普通租户可见集为空时返回未配置占位客户端。
- **通知渠道**（T21）：行级租户私有（bot_token 等仅本租户可见）；租户可引用管理员托管渠道（`is_shared`），可测试发送但 config 不回显；新租户默认零渠道。
- 越权写语义：改/删他租户行返回 404（不暴露存在性）；普通租户建/改 `is_shared` 返回 403；新建行强制归属当前租户（请求体注入 tenant_id 无效）。

### 4. 通知链路

- 渠道类型与字段 schema 来自 `src/core/notifier.py` 的 `CHANNEL_TYPES`；测试发送走 `NotifierManager.notify_with_result(bypass_quiet_hours=True)`。
- 模拟盘跟单通知的开关/渠道存 `AppSettings`（`pt_notify_*` 键）。

### 5. 后台执行模式

两类反复出现的模式：
- `_spawn_async_run`（agents.py）：daemon 线程里 `asyncio.run()`，失败仅记日志。
- recommendations.py 的后台刷新：线程 + 全局 `_refresh_state` 单飞（已在跑则拒绝新任务），`/strategy-signals/refresh-status` 轮询进度。

### 6. 依赖 server.py 的反向 import

多处路由在函数体内 `from server import ...`（`trigger_agent`、`load_watchlist_for_agent`、`build_context`、`apply_proxy_env`、`reload_scheduler`、`reconcile_data_sources`、`_seed_providers_by_type`、`price_alert_scheduler`）。**api 层与 server.py 强耦合**，二开时移动/重构 server.py 顶层函数会波及这些路由。

## 五、二次开发扩展点

- **新增 Agent**：实现 `src/agents/*.py` 后在 `server.py` 的 `AGENT_REGISTRY` 注册并 `seed_agents()`；本组路由自动通过 `AgentConfig` 表暴露配置/触发/历史，无需改 api 代码。
- **新增通知渠道**：在 `src/core/notifier.py` 注册 `CHANNEL_TYPES`，channels.py 自动暴露。
- **新增设置项**：`settings.py` 的 `SETTING_DESCRIPTIONS` 是白名单，GET 列表只返回其中 key（缺失时按环境变量默认值自动落库）。注意 `PUT /{key}` 是通配路由，`/avatar`、`/version`、`/update-check` 必须注册在它前面。
- **新增数据源类型**：改 `datasources.py` 的 `TYPE_LABELS` + `_ENGINE_ATTACHED_TYPES`，并在 server.py seed；孤儿判定依赖 `marketdata.PACKAGE_VENDORS_BY_TYPE` ∪ seed 集合。
- **chat 工具扩展**：在 `CHAT_TOOLS` 定义 + `_execute_tool` 分发各加一支；`MAX_TOOL_ROUNDS=5`、`MAX_HISTORY_MESSAGES=20` 是硬编码。
- **配置包（templates）**：只覆盖 `_SETTINGS_KEYS` 白名单内的设置；导入后 best-effort 调 `reload_scheduler()` 使 schedule 立即生效。

## 六、注意事项 / 易踩坑

1. **路由顺序敏感**：settings.py 中 `/avatar` 必须在 `PUT /{key}` 前注册；suggestions.py 用双装饰器兼容带/不带尾斜杠（app 层 `redirect_slashes=False`，避免重定向丢 Authorization header）。
2. **路径参数 vs 固定路径**：agents.py 中 `/capabilities`、`/schedule/preview`、`/tradingagents/*`、`/intraday/scan` 等都抢在 `/{agent_name}` 之前注册，新增固定路径时注意放前面。
3. **PUT update 重置副作用**：price_alerts 修改规则会清零 `trigger_count_today`；providers/channels 设 `is_default` 时先把全表清 False（单默认约束靠代码维护，非 DB 约束）。
4. **时区**：所有 DB 时间按 UTC-naive 存取，输出才转 `app_timezone`；新增时间字段务必遵守同一约定，否则会出现 8 小时偏差。
5. **API key 明文**：`providers.py` 的 `ServiceResponse` 直接返回 `api_key` 明文给前端，二次开发若加权限/审计需注意。（多租户，2026-07：仅本租户行明文，管理员托管行已掩码，见「3d」。）
6. **密码哈希弱**：SHA256 无盐，仅适合单用户自托管场景；改认证方案时 `get_current_user` 是唯一依赖注入点。（多租户，2026-07：已改 bcrypt + 旧哈希首次登录透明重哈希，见「3」。）
7. **盘中扫描** `POST /api/agents/intraday/scan` 是大聚合端点：行情按市场并发采集 + K 线并发（信号量 6）+ AI 分析并发（信号量 3），且只在开市时段扫描；`analyze=True` 结果会写入建议池（`save_suggestion`，6 小时有效）。内存缓存 TTL 很短，压测时注意。
8. **PDF 导出**（`/tradingagents/analysis/pdf`）由 `src/core/pdf_export.py` 后端直出，不依赖前端 Chromium。
9. 未能确认项：`src/core/selfcheck.py`、`strategy_engine`、`paper_trading_engine`、`factor_weights` 等核心模块未在本范围内深读，其行为以各自模块为准。
