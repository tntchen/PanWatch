# PanWatch 源码阅读 · 03 — 代理 / 系统 / 设置类 API 路由

> 范围：`src/web/api/` 下的 agents、chat、channels、history、settings、providers、auth、logs、health、feedback、templates、suggestions、recommendations、price_alerts、paper_trading、factors、datasources 共 17 个文件。
> 面向二次开发者，只写已确认的事实。

## 一、模块职责一句话总结

这组路由是 PanWatch 的"管理平面"：Agent 调度配置与手动触发、AI 对话、AI 服务商/模型管理、通知渠道、数据源、全局设置、单用户认证、日志中心、分析历史、建议池与反馈、配置包导入导出、价格提醒、模拟盘、策略/因子权重等系统级 CRUD 与运维接口。

## 二、路由注册点（src/web/app.py）

- 除 `auth`（`/api/auth`）与 `market` 外，**所有本组路由都挂在 `protected = [Depends(get_current_user)]` 下**，即认证生效后需 Bearer Token。未设置密码时 `get_current_user` 直接放行（返回 None）。
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

### 3. 认证模型

- 单用户：用户名/SHA256 密码哈希存 `AppSettings`；JWT（HS256，30 天）密钥优先级：环境变量 `JWT_SECRET` > DB 自动生成（`secrets.token_hex(32)`）。
- 未设置密码 = 完全开放；Docker 部署可用 `AUTH_USERNAME`/`AUTH_PASSWORD` 首次初始化。

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
5. **API key 明文**：`providers.py` 的 `ServiceResponse` 直接返回 `api_key` 明文给前端，二次开发若加权限/审计需注意。
6. **密码哈希弱**：SHA256 无盐，仅适合单用户自托管场景；改认证方案时 `get_current_user` 是唯一依赖注入点。
7. **盘中扫描** `POST /api/agents/intraday/scan` 是大聚合端点：行情按市场并发采集 + K 线并发（信号量 6）+ AI 分析并发（信号量 3），且只在开市时段扫描；`analyze=True` 结果会写入建议池（`save_suggestion`，6 小时有效）。内存缓存 TTL 很短，压测时注意。
8. **PDF 导出**（`/tradingagents/analysis/pdf`）由 `src/core/pdf_export.py` 后端直出，不依赖前端 Chromium。
9. 未能确认项：`src/core/selfcheck.py`、`strategy_engine`、`paper_trading_engine`、`factor_weights` 等核心模块未在本范围内深读，其行为以各自模块为准。
