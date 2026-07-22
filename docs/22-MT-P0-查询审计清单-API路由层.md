# 22 · MT-P0 查询审计清单 · API 路由层

> 日期：2026-07-22 ｜ 阶段：MT-P0（设计稿，未改任何代码）
> 对应决策点：T2/T3/T4/T5/T9/T12/T13/T15/T16/T17/T18/T19/T20/T21
> 呼应评审：docs/20 高危 C2/C6/C7、中危 M6/M8/M10/M11/M13
> 范围：`src/web/api/` 下 26 个 protected router（`app.py:64` 统一 `Depends(get_current_user)`，挂载清单 `app.py:65-185`）+ `auth.py`（非 protected 但属身份体系），共 201 处 `db.query`（含 auth.py 5 处）。所有行号经 2026-07-22 逐文件核对。

---

## 1. 分类规则（每条查询标 a/b/c/d 之一）

| 类 | 含义 | 处置 |
|---|---|---|
| **a** | 租户私有表 SELECT | 走 T16 机制点（contextvar TenantContext + `do_orm_execute` 自动注入 `tenant_id == current`），handler 不改签名 |
| **b** | 需显式 tenant 条件的**写操作**（bulk `Query.update()/delete()`、upsert） | 机制点必须覆盖 ORM bulk（`is_update/is_delete` 分支），且 MT-P0.5 机制点验收用例必须逐类实测；机制覆盖不到处回退 default-deny helper（`tenant_update()/tenant_delete()`，无 TenantContext 即抛错） |
| **c** | 市场级/全局模板共享表 | **不加** tenant 过滤（T19：`market_scan_snapshots`/`entry_candidates`/`strategy_signal_runs`；T4：`agent_configs` 模板行；`data_sources`） |
| **d** | 特殊处置 | 逐条注明（缓存 key、LLM prompt 面、排他 `is_default`、托管∪私有双轨谓词、进程单例穿透、管理员专属、签名 URL 等） |

**机制点关键前提（写入 MT-P0.5 验收）**：
1. `do_orm_execute` 自动过滤对 SELECT/UPDATE/DELETE 三类都注入 tenant 谓词——排他 `is_default` 全表 update 由此自动变为租户作用域，但**必须用真实 SQL 断言验证**（`synchronize_session="fetch"` 与注入谓词的交互是风险点，`playbooks.py:112,138` 用了该参数）。
2. 事件必须挂在**全局 Session 工厂/engine** 而非 `get_db` 依赖返回的 session 上，否则自建 `SessionLocal()` 的 handler 绕过机制（路由层 2 处：`chat.py:462`、`recommendations.py:52`）。
3. 私有表谓词不全是 `tenant_id == current` 一等式：`ai_services`/`notify_channels` 需要「本租户 ∪ 管理员托管」双轨谓词（T3/T13/T21），机制点需支持 per-model 谓词注册表，不能只有默认等式。

---

## 2. 逐文件审计表

### 2.1 stocks.py（22 处）— 自选股 CRUD + Agent 绑定 + 手动触发 【大】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :181, :188 | `Stock` 列表 | a | |
| :222-224 | `Stock.symbol==X & market==Y .first()` | d | **T15 解析点 ①**（docs/20 C6 清单 stocks.py:223）；T15 每租户复制后命中本租户行，v121 加 `(tenant_id,symbol,market)` UQ |
| :228 | `func.max(Stock.sort_order)` | a | 自动过滤后聚合作用域正确 |
| :241 | `Stock.id.in_(ids)` | a | reorder 入参 id 被过滤后静默丢弃别租户 id（行为安全） |
| :256, :270, :308, :362 | `Stock.id==X .first()` | a | |
| :275 | `Position.stock_id==X` | a | |
| :282-285 | `PriceAlertRule.id` | a | |
| :287-289, :290-292 | `PriceAlertHit` bulk delete | b | 级联删（synchronize_session=False）——机制点 bulk 分支验收必测 |
| :293-295 | `PriceAlertRule` bulk delete | b | 同上 |
| :296-298, :321 | `StockAgent` bulk delete | b | 同上 |
| :313, :373, :396 | `AgentConfig.name==X` | c | T4 模板行，全局 |
| :366-368, :390-392 | `StockAgent` | a | |
| :386-388 | `Stock.symbol==X & market==Y .first()` | d | **T15 解析点 ②**（C6 清单 stocks.py:387）；`allow_unbound` 路径，命中不到落 `SimpleNamespace`(:399-404)，多租户下必须限定本租户 |
| :410, :483 | `trigger_agent_for_stock` | d | 后台链显式传 tenant_id（T16 后台链约定）；`:417-418` 幂等查 `find_active_tradingagents_trace` 需按 trace 归属校验本租户 |

### 2.2 quotes.py（0 处）/ klines.py（0 处）— 纯行情透传 【小/免改】

无 DB 查询，市场数据天然共享（docs/17 §1.3）。仅 klines/quotes 需在 T16 落地后回归验证不触发 default-deny。

### 2.3 insights.py（2 处 + 2 个缓存）【中】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :177 | `Stock.symbol==X .first()` | d | **T15 解析点**（C6 清单 insights.py:177） |
| :322 | `Stock.symbol==X .first()` | d | **T15 解析点**（C6 清单 insights.py:322） |
| :27, :317-318, :327, :359 | `_ANN_CACHE` TTLCache | d | key=`{market}:{symbol}`(:317) 无租户；公告本身是市场级、但 AI 解读消耗**本租户配额/模型**（T13），缓存结果跨租户复用会让 B 白嫖 A 的解读且提示语来自 A 的模型 → key 加 `{tenant}:` 前缀 |
| :73-91 | `_KLINE_CACHE` | — | K 线市场级共享，**不加**租户 |
| :11-16, :253, :341 | `_get_ai_client` 复用 chat.py | d | T13 租户化解析（见 chat.py 注） |

### 2.4 accounts.py（29 处 + 1 缓存）— 账户/持仓/流水/组合分析 【中】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :191-199 | `PositionTrade` 聚合 | a | `_trade_stats` helper，被多 handler 复用 |
| :242, :248, :268, :288 | `Account` CRUD | a | |
| :307, :331-334, :366, :412, :446, :503, :522 | `Position` | a | |
| :322, :449 | `Account.id==X` | a | |
| :326, :894, :918 | `Stock.id==X .first()` | a | |
| :338-340 | `func.max(Position.sort_order)` | a | 已按 account_id 限定，自动过滤后正确 |
| :417-420 | `func.count(PositionTrade.id)` | a | |
| :508-511 | `PositionTrade` | a | |
| :555, :557, :799, :884 | `Account.enabled==True` | a | |
| :579, :801 | `Stock.id.in_(...)` | a | |
| :786-790 | `Position join Account` | a | `_holdings_signature` |
| :889-891, :908-915 | `PriceAlertRule` | a | |
| :780, :864-865, :941-942 | `_PORTFOLIO_RESULT_CACHE` | d | **缓存 key 加租户**（docs/17 §3 风险最高项）：ckey `bench:{days}:{bcode}:{sig}`/`attr:...`(:864,:941) 前缀加 `{tenant}:`；M11 已注：双双空仓 sig 为空串必撞，加租户后彻底消除 |
| :956-1002 | `/portfolio/ai-review` → `_get_ai_client` | d | T13：AI 客户端解析必须租户化（本租户模型 or 托管配额） |

### 2.5 agents.py（21 处 + 1 缓存）【大】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :26-36, :877-879, :1169 | `_SCAN_CACHE` | d | **缓存 key 加租户**（docs/17 §3）：key=`intraday_scan:{analyze}:{symbols}`(:34-36)，两租户同 watchlist 必串且 payload 含 `available_funds` 持仓资金(:1166) → 前缀 `{tenant}:` |
| :110, :228, :262, :274, :317, :338, :362, :643, :848 | `AgentConfig` | c | T4 模板行全局；enabled/schedule 等租户偏好读侧走 override 合并（T4 优先级链 stock_agent>tenant_override>template>系统默认），**查询本身不过滤，响应组装层合并** |
| :134 | `AgentRun` | a | agent_runs 私有 |
| :345 | `StockAgent` bulk delete | b | 删除 agent 时清关联 |
| :369, :383 | `trigger_agent` | d | 进程级穿透点（docs/17 §3）：handler 须显式传 tenant_id 给运行时（T17 单 job 遍历下手动触发=指定租户单跑） |
| :416-425, :469-477, :704-712 | `LogEntry`（trace 进度） | d | log_entries 归因（T20）后按 tenant 过滤；但 infra 日志无 tenant，进度查询以 `AgentRun`/`trace_id` 归属为准：先查本租户 AgentRun(:434,:484,:752,:794 → a)，trace 属于本租户才放行查日志 |
| :514, :552, :596 | `AnalysisHistory` | a | 私有 |

### 2.6 providers.py（14 处 + 3 处 is_default update）— AI 服务/模型 【中】

T3+T13 双轨：管理员托管服务（含共享配额）∪ 租户自建私有服务。所有查询谓词 = `tenant_id == current OR is_managed`，属 d 双轨谓词（机制点 per-model 注册表）。

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :50, :78, :92 | `AIService` | d | 双轨谓词；`:59` `ServiceResponse.api_key` 明文回包——托管服务对普通租户**必须脱敏**（T3「只选不见密钥」），自建行仅本租户可见原文 |
| :128, :133, :152, :170, :180, :184, :208, :221 | `AIModel`/`AIService` | d | 同上 |
| :138, :158, :231 | `AIModel.update({"is_default": False})` | b+d | **排他 is_default 全表 update 3 处**（docs/17 §3 只列 :138,:231，**:158 漏列，此处补充**）；机制点 bulk 注入 tenant 谓词后自动变租户作用域，验收必测；且 is_default 语义在双轨下=「本租户默认」，托管行不受租户操作影响 |

### 2.7 channels.py（6 处 + 2 处 is_default update）— 通知渠道 【中】

T21：行租户私有 + 可引用管理员托管渠道。查询谓词同 providers 双轨（d）。

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :42 | `NotifyChannel` 列表 | d | 双轨谓词；托管渠道 `config`（bot_token）对普通租户脱敏 |
| :54, :70 | `NotifyChannel.update({"is_default": False})` | b+d | **排他 is_default 全表 update**（docs/17 §3 已点名 :54,:70）；租户作用域化 |
| :64, :82, :93 | `NotifyChannel.id==X` | d | 私有行可改删；托管行普通租户只读不可改（需 role/归属校验，非纯 tenant 谓词能表达 → handler 显式校验） |

### 2.8 datasources.py（5 处）— 数据源管理 【中】

`data_sources` 全局共享（docs/17 §2.1）但 `config` 含凭证 → T3 管理员托管。

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :124, :152 | `DataSource` 读 | d | 表本身 c 语义，但**整个 router 定调管理员专属**（T3）；普通租户只见 marketdata 能力不见凭证。`_to_response`(:101-118) 回包 `config` 明文(:110) 需脱敏或管理员专属 |
| :139-146 | `reset-to-seed` | d | 管理员专属（调 `reconcile_data_sources`，server.py:709） |
| :183, :199, :212 | 写/删/测试 | d | 管理员专属 |

### 2.9 settings.py（5 处）— 应用设置 + 头像 【中】

T20 app_settings 三分：jwt_secret/http_proxy/base_url=实例级；auth_*→users 表；notify_*/avatar=租户级。

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :79 | `AppSettings.all()` | d | 三分后按「实例级（管理员可见）/租户级（本租户）」分流组装；SETTING_KEYS(:50-60) 中 `http_proxy`/`panwatch_base_url`=实例级，`notify_*`=租户级，`stock_link_platform`=租户级 UI 偏好（归类自定，见 §8 上报） |
| :118, :140 | `ui_avatar` | d | 租户级；`data/avatars/`(:106-109) 目录按 tenant 隔离（MT-P2 数据层任务） |
| :183 | PUT `/{key}` | d | **新发现越权面：无 key 白名单**，任意登录用户可写任意 AppSettings key（含 jwt_secret/auth_password_hash）——必须加白名单 + 实例级 key 仅管理员（§8 上报项 2） |
| :197-198 | `apply_proxy_env` | d | 进程 env 代理**定调实例级**（docs/17 §4/T20），仅管理员可触发 |
| :216-219 | `http_proxy` 读 | d | 实例级，管理员专属 |

### 2.10 logs.py（7 处）【中】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :91 | `LogEntry` 查询 | a | T20 tenant 归因后自动过滤；infra 日志无 tenant → 普通租户只见 business 域自有日志，infra 域管理员专属 |
| :210-211 | `LogEntry.delete()` 全表清 | d | **无条件清全表**（docs/17 §3 已点名 :208-212）：改为 ① 普通租户仅清本 tenant business 日志；② 全清=管理员专属（role 实时查库 T5） |
| :221, :273, :275, :277, :278 | meta/health 统计 | a | 自动过滤；管理员视角另议 |

### 2.11 history.py（3 处）— 分析历史 【小】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :82, :155, :197 | `AnalysisHistory` | a | 私有，自动过滤 |

### 2.12 context.py（0 处 db.query，5 个入口走 core.context_store）【中】

docs/20 C7 点名 `context_store` 不在调用链上——本文件是 T16「后台/间接链显式传参」的路由侧代表。

| 入口 | 类 | 说明 |
|---|---|---|
| :85 `get_recent_stock_context_snapshots` | d | stock_context_snapshots **强私有**（R2/C2 改判：payload 含持仓+playbook 摘要且回读注入 prompt）——core 函数必须收 tenant_id |
| :109 `get_latest_news_topic_snapshot` | d | news_topic_snapshots 同判私有（C2） |
| :133 `list_recent_agent_context_runs` | d | agent_context_runs 私有（docs/17 §2.1） |
| :161 `list_agent_prediction_outcomes` | d | **属主待定**：agent 预测后验，输入是租户建议 → 建议判私有，§8 上报项 3 |
| :196, :209 | evaluate/cleanup | d | 显式 tenant 参数 |

### 2.13 news.py（2 处）【小】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :54 | `Stock.all()` | a | 租户 watchlist 决定新闻匹配范围；T8 共享去重在采集层，此查询是本租户自选 |
| :141 | `DataSource` | c | 全局共享 |

### 2.14 suggestions.py（0 处，3 入口走 core.suggestion_pool）【小】

| 入口 | 类 | 说明 |
|---|---|---|
| :30, :76, :94 | d | `StockSuggestion` 私有（docs/17 §2.1；T19 交互面归租户）——core 函数收 tenant_id；`:94` cleanup 按 tenant 限定 |

### 2.15 templates.py（8 处）— 配置包导出/导入 【大】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :71 | `AppSettings` | d | T20 三分：导出集 _SETTINGS_KEYS(:55-61) 含 http_proxy（实例级）与 notify_*（租户级）混在一起，需拆分；**导出=管理员专属**（T20 打包项「配置包导出管理员专属」） |
| :75 | `AgentConfig` | c | 导出读模板 |
| :99, :103, :228 | `Stock`/`StockAgent` | a | 导出本租户自选与绑定 |
| :161 | `AppSettings.key==k` upsert | d | 导入写——三分后按 key 归类分流 |
| :170 | `AgentConfig.name==X` upsert | d | **T4 冲突点**：导入直接 upsert 全局模板行（:170-207），多租户下=篡改系统模板；必须改为写 tenant override 表（整体替换语义） |
| :212-215 | `Stock.symbol==X & market==Y .first()` | d | **T15 解析点**（C6 清单 templates.py:213）；导入作用域=本租户 |
| :265 | `reload_scheduler` | d | 进程级穿透点（docs/17 §3）；T17 单 job 遍历租户后 reload 语义=重建全局 job（管理员操作结果），保持实例级 |

### 2.16 feedback.py（3 处）【小】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :25-28 | `StockSuggestion.id==X` | a | T19 交互面租户私有 |
| :50-61, :79-91 | `SuggestionFeedback` 聚合 | a | 反馈统计按租户 |

### 2.17 recommendations.py（1 处 db.query + 全量 core 入口）【中】

| 行号/入口 | 类 | 说明 |
|---|---|---|
| :55 `StrategySignalRun` | c | T19 市场级信号链全局；`:52` 自建 `SessionLocal()` —— 机制点须挂全局工厂（§1 前提 2） |
| :157 `list_entry_candidates` | c | T19 市场级；holding=held/unheld 参数(:164) 的持仓判定在 core 层需要 tenant → 入口显式传 tenant_id |
| :188 `save_entry_candidate_feedback` | d | T19 交互面租户私有 |
| :235 `list_strategy_signals` | c+d | 信号市场级；holding 判定租户化传参 |
| :272 `list_portfolio_risk_snapshots` | d | **portfolio_risk_snapshots 私有**（docs/17 §2.1「语义本身就错」：UQ(snapshot_date,market) 两租户撞约束，v121 重建）——core 函数收 tenant_id |
| 其余（catalog/regimes/factors/stats/weights/refresh） | c | T19 市场级全局；`:349` weights rebalance 写全局权重 → 管理员专属（同 factors.py 注） |

### 2.18 price_alerts.py（11 处 + 2 引擎入口）【中】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :112 | `StockPlaybook.id==X` | a | `_validate_playbook` 归属校验在自动过滤后天然成立 |
| :149-153 | `PriceAlertRule join Stock` | a | 隐式 join(:150)，机制点对两端表各自注入谓词 |
| :159, :274 | `Stock` | a | |
| :195, :233, :244, :295 | `PriceAlertRule.id==X` | a | |
| :264-268, :297-301 | `PriceAlertHit` | a | |
| :272 | `PriceAlertRule.all()` 构造 rule_map | a | 自动过滤后 map 仅本租户，`:278` 取不到别租户 rule 返回默认名，行为安全 |
| :319-321 | `ENGINE.scan_once(only_rule_id=...)` | d | 进程级单例穿透（docs/17 §4）：dry_run 按租户上下文执行 |
| :328-333 | `price_alert_scheduler` | d | 同上（docs/17 §3 进程级引用点 :328） |

### 2.19 chat.py（19 处 + 自建 session）— LLM 对话 【大】

**M8 头号泄漏面**：prompt 上下文拼装注入自选股(:115)、全部持仓(:238-274)、建议/分析(:200,:217)，mock LLM prompt 内容级断言的落点。

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :115 | `Stock` watchlist → prompt | a+d | 自动过滤 + prompt 面（M8 断言点） |
| :168, :171, :174, :177 | `AIModel`/`AIService` 解析 | d | `_get_ai_client`：is_default 全局回退(:171) 必须改 T13 租户解析（本租户默认模型 → 托管配额 → 自建服务）；该函数被 accounts.py:961、insights.py:15、dashboard.py:17 复用，**一处改四文件受益** |
| :200, :217, :332 | `StockSuggestion`/`AnalysisHistory` → prompt | a+d | prompt 面 |
| :238, :242 | `Position`/`Stock` 持仓 → prompt | a+d | prompt 面（:238 全表 .all() 注入 system message :515-517） |
| :254 | `PaperTradingPosition` → prompt | a+d | T9 每租户账户后按 tenant |
| :352-356 | `Position join Stock` symbol 解析 | d | **T15 解析点**（C6 清单 chat.py:354） |
| :397, :416, :420, :447, :450 | `ChatConversation`/`ChatMessage` | a | 私有 |
| :462 | `db = SessionLocal()` 自建 | d | **机制点绕过风险**（§1 前提 2）：`send_message` 不走 `get_db`，do_orm_execute 必须挂全局工厂 + contextvar 已在请求链 set；验收用例必测此 handler |
| :464, :501 | `ChatConversation`/`ChatMessage` | a | |

### 2.20 dashboard.py（12 处）【小】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :121, :311, :464 | `AnalysisHistory` | a | |
| :230-233 | `Position join Stock` | a | |
| :251 | `func.count(Stock.id)` | a | |
| :255-259 | `Account.available_funds` 聚合 | a | |
| :262, :267, :319 | `MarketScanSnapshot` | c | T19 市场级 |
| :296 | `NewsTopicSnapshot` | a | **C2 改判私有**——首页「热门主题」按租户隔离 |
| :314 | `EntryCandidate` | c | T19 市场级 |
| :324-331 | `LogEntry` 错误计数 | a | 租户归因后=本租户错误数；infra 错误管理员视角另议 |
| :161-204, :432 | `list_strategy_signals`/`_get_ai_client` | c+d | 信号市场级（holding 判定租户化传参）；curate AI 客户端 T13 租户化 |

### 2.21 paper_trading.py（17 处）— 模拟盘 【中】

T9 每租户一账户（前置补 account_id + 回填）。

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :104, :109, :175, :199, :207, :238, :243, :369, :380, :404 | `PaperTradingPosition`/`PaperTradingTrade` | a | T18 子行 tenant 从 account 派生；v120 直接给两表加 tenant_id 列后自动过滤即可 |
| :354, :398, :433, :460 | `PaperTradingAccount.first()` | d | **单例 .first() 4 处**（docs/17 §3 已点名 :354 等 4 处）：改 `filter(tenant_id==current).first()`（自动过滤后 .first() 语义自然正确，但 :354-363 缺账户自动建仓逻辑要按租户建） |
| :519, :546 | `AppSettings`（pt_notify_*） | d | 租户级 KV（T20 notify_* 归租户） |
| :524 | `NotifyChannel.enabled` | d | 双轨可见性（本租户 ∪ 托管，脱敏） |
| :444, :452, :491 | `ENGINE` reset/close/scan | d | 进程级单例穿透：按租户上下文执行，T17 扫描 job 单 job 遍历租户 |

### 2.22 factors.py（0 处 db.query，2 入口）【小】

| 入口 | 类 | 说明 |
|---|---|---|
| :29 `get_all_factor_weights` | c | T19 市场级：因子权重全局自动标定 |
| :38 `set_factor_weight`（pin/覆盖） | d | 写全局权重 → **管理员专属**（docs/17 §2.1 混合语义按 T19 市场级裁决；若未来要每租户标定需另行立项） |

### 2.23 health.py（0 处）【小】

| 入口 | 类 | 说明 |
|---|---|---|
| :10 `run_selfcheck` | d | 自检触达数据源/AI/通知凭证：`notify_send=True`(:12) 管理员专属；AI 自检项按双轨谓词只列本租户可见服务 |

### 2.24 playbooks.py（6 处 + 2 bulk update）【小】

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :63 | `Stock.id==X` | a | |
| :80, :101, :130 | `StockPlaybook` | a | T7/R3：playbook 价位解析进事件门租户态 |
| :109-112, :134-138 | `StockPlaybook.update({"is_active": False})` | b | 排他 update **已按 stock_id 限定**（父子派生安全，非 providers/channels 式全表型）；但用 `synchronize_session="fetch"`，是机制点 bulk 分支的**首选验收用例** |

### 2.25 auth.py（5 处）— 身份体系 【大】（详见 §5）

| 行号 | 目标 | 类 | 说明 |
|---|---|---|---|
| :51 | `AppSettings` jwt_secret | d | T20：jwt_secret=实例级，迁出 AppSettings 租户 KV |
| :109, :115 | `auth_username` | d | T20：auth_* → users 表 |
| :126, :132 | `auth_password_hash` | d | 同上 |

### 2.26 market.py（0 处，公开路由）【免改】

`app.py:61` 无 protected，公共市场指数，无需改造。

---

## 3. 路由外的 API 面特殊项

| 位置 | 项 | 类 | 处置 |
|---|---|---|---|
| `app.py:203-227` | `GET /reports/{filename}` 无鉴权 + 文件名可枚举 | d | **签名 URL 方案**（docs/17 §3 已定方向）：`?exp=&sig=` HMAC（key 派生自实例级 jwt_secret），路由校验签名+exp；签名内编码 tenant_id 与 filename，下载时校验文件属主目录（MT-P2 reports 目录按 tenant 隔离）；前端站内下载改走 Authorization 头。MT-P4 实施，通知外链（panwatch_base_url，`settings.py:57`）同步切换签名 URL |
| `server.py:302-352` | `seed_agents` 按 name 全库 upsert | — | **不在路由层**，列此备查：T4 后 seed 只写模板表，禁止触碰 tenant override；对账不删租户行（docs/20 E7） |
| `agents.py:79-89` | `_spawn_async_run` 后台线程 | d | 后台链显式传 tenant（contextvar 不跨线程传播——线程内首行 `with_tenant(tenant_id)`） |

---

## 4. handler 身份接收统一模式（签名改造约定）

1. **JWT → User**：`get_current_user` 返回真实 `User` ORM 对象（不再返字面量 `"user"`，`auth.py:186`），role 实时查库（T5），并在依赖内 `TenantContext.set(tenant_id)`；改为 **yield 依赖**，请求结束 `reset()`（防线程池/任务复用串 tenant）。
2. **默认约定（a 类 handler）**：**不改签名**。机制点过滤，handler 无感——这是 T16「N 个人工点降为 1 个机制点」的核心收益，本清单 a 类全部适用。
3. **显式身份约定（b/d 类 handler）**：签名追加 `user: User = Depends(get_current_user)`（置于 `db` 参数之后），适用场景：
   - 缓存 key 拼租户（accounts:864/941、agents:877、insights:317、discovery:244/283/350）
   - 排他 is_default 写、托管∪私有双轨谓词、脱敏分支（providers/channels/datasources）
   - 管理员专属门（logs DELETE、datasources 写、settings 实例级、factors 写、templates 导出）
   - 调度/引擎穿透（agents:383、price_alerts:319/328、paper_trading ENGINE、templates:265）
   - LLM 客户端解析（chat:536、accounts:998、insights:253/341、dashboard:432）
4. **后台线程约定**：`threading.Thread` 起的任务（agents:473、stocks:457-478、recommendations:123）在 runner 首行显式 `with_tenant(user.tenant_id)`；禁止依赖 contextvar 隐式跨线程。
5. **default-deny**：无 TenantContext 且命中私有表 → 抛错（不静默放行）；市场级/模板表白名单（c 类清单：`market_scan_snapshots`/`market_regime_snapshots`/`entry_candidates`/`strategy_signal_*`/`factor_*`/`news_cache`/`data_sources`/`agent_configs` 模板）显式注册豁免。
6. **双轨谓词注册**：`ai_services`/`notify_channels` 注册 per-model 谓词 `tenant_id == current OR (is_managed AND enabled)`；写操作（UPDATE/DELETE）谓词恒为 `tenant_id == current`（托管行只允许管理员身份通过 role 校验修改）。

---

## 5. auth.py API 面改造需求清单（MT-P1 输入）

| # | 端点/函数 | 现状（证据） | 改造需求 |
|---|---|---|---|
| A1 | `GET /auth/me` | 返回 `{"user": "user"|"guest"}` 字面量（`auth.py:252-255`） | 返回 `{id, username, role, tenant_id, ai_quota_mode}`；前端身份显示的收口点 |
| A2 | 注册供给 | 无注册端点；`/setup` 首次设密（:198-215） | **邀请制**（T12）：新增管理员专属 `POST /auth/users`（邀请创建）、`GET /auth/users`、`PATCH /auth/users/{id}`（角色/配额模式/停用）、`DELETE`；MT-P1 上线即关任何自助注册（M1：P1→P2 禁多用户） |
| A3 | `/auth/setup` | 未设密码即可设（:201-202） | 仅 `users` 表为空时允许（初始管理员 bootstrap）；env `AUTH_USERNAME/AUTH_PASSWORD`(:25-26,:141-157) 映射初始管理员（T20/M10） |
| A4 | 未设密码全站放行 | `auth.py:166-169` return None | **废除**（docs/17 §1.1-3 硬要求）；conftest 加 `authenticated_client` fixture（M6：3 个 TestClient 文件会 401） |
| A5 | `/auth/login` | 裸 SHA-256 无盐（:78-80, :229） | bcrypt；旧 SHA-256 哈希验证通过即**透明重哈希**写回 users（M10） |
| A6 | `/auth/change-password` | **不校验旧密码**（:236-249）——顺带发现的安全债 | 加旧密码校验；写 bcrypt |
| A7 | token 结构 | `sub="user"` 硬编码（:90）、verify 返 bool（:96-104）、30 天无吊销（:22） | payload=`{sub:user_id, tenant_id, tv}`；verify 返回 payload 并查库（role 实时 T5）；**旧 token 一次性踢出**（T20：无 user_id claim 即 401，不设兼容期） |
| A8 | `get_current_user` 返回 | 字面量 `"user"`（:186） | 返回 User 对象 + set TenantContext（§4-1） |
| A9 | jwt_secret 存储 | AppSettings KV（:29-31, :51-57） | 迁实例级配置（T20 三分） |
| A10 | 单租户回退 | — | feature flag（T20/M14）：flag 开时恒返回默认租户 User(tenant_id=1)，行为等价单用户，验收=611 基线零修改全绿 |

---

## 6. 每文件改造工作量分级汇总

| 分级 | 文件 | 依据 |
|---|---|---|
| **小** | quotes, klines, health, market(免改), factors, history, news, suggestions, feedback, dashboard, context, playbooks | 0 查询或全 a 类/入口参数化 |
| **中** | insights（2 解析点+2 缓存）, logs（DELETE 管理员化+归因）, settings（三分+白名单+env 代理）, channels（双轨+2 排他）, providers（双轨+脱敏+3 排他）, datasources（管理员化+脱敏）, discovery（缓存）, recommendations（holding 传参+风险快照）, price_alerts（引擎穿透）, paper_trading（T9+4 处 .first()+notify KV）, accounts（29 处 a+缓存+AI 客户端） | 有 d 类专项但无结构性重写 |
| **大** | stocks（T15 双解析点+4 组级联 bulk delete+trigger 穿透）, agents（21 处+缓存+trigger 链+进度 trace 归属）, chat（prompt 泄漏面+自建 session+AI 解析中枢）, templates（T4 语义重定义：导入写 override、导出管理员专属）, auth（身份体系全量重建） | 结构性改造 |

---

## 7. 评审项呼应（docs/20 中与本范围相关项）

| 项 | 呼应 |
|---|---|
| C2 | news_topic_snapshots/stock_context_snapshots 改判私有 → §2.12/:296 全部 a/d 过滤 |
| C6 | 12 处解析点中属路由层 4 处（stocks:222,386；insights:177,322；templates:212；chat:352-354 区段）全部标 d；剩余 8 处在运行时层清单 |
| C7 | §4 统一模式 = T16 机制点落地的 handler 侧契约；§1 前提 2 覆盖 log_handler/context_store 类「不在调用链」问题的路由侧代表（chat:462） |
| M6 | A4：authenticated_client fixture 需求登记 |
| M8 | §2.19 chat + §2.4 ai-review + §2.3 insights + §2.20 curate = prompt 内容级断言的全部路由侧注入点（4 处） |
| M10 | §5 A5/A7 |
| M11 | 缓存穷举：accounts:780、agents:26、insights:27 三处已登记；**新发现 discovery._cache** → §8 上报 |
| M13 | §2.9 settings 三分落位 |

## 8. 需上报 Orchestrator 裁决 / 与 docs-17/20 的差异

1. **discovery.py 缓存泄漏（新发现，补 M11 清单）**：`_cache`(:20) 的 `boards`(:283)/`board_stocks`(:350) key 无租户，但结果含 WATCHLIST 合成板块（:318, :366 用本租户 watchlist 参与构建 :177/:196, :220）→ 跨租户串。处置同其他缓存（key 加租户）；docs/17 §3 与 docs/20 M11 均未含此项。
2. **settings.py PUT /{key} 无白名单（新发现越权面）**：:183 任意登录用户可写任意 AppSettings key（含 jwt_secret/auth_password_hash），单用户下已成立，多租户下=改实例密钥。docs/17/20 未点名；处置：白名单 + 实例级 key 管理员专属。
3. **agent_prediction_outcomes 属主待定**：T19 将 outcomes 链判市场级，但该表是**租户 agent 建议**的后验（suggestion 属租户交互面），建议判租户私有；若判市场级，则反馈统计跨租户混算。需裁决。
4. **providers.py 排他 is_default 实为 3 处**：docs/17 §3 列 :138,:231，漏 :158（update_model）。事实补充，不影响决策。
5. **chat.py:462 自建 SessionLocal 的机制点约束**：T16 若把过滤挂在 `get_db` 依赖上将被绕过；要求机制点挂全局 Session 工厂 + contextvar 由 get_current_user 设置（§4-1），或重构该 handler 改走 get_db。此为 MT-P0.5 机制点设计硬约束，需登记进机制点验收。
6. **auth.py change-password 无旧密码校验**（:236-249）：超出 docs/17 §3 密码债清单的新发现，已并入 §5-A6。
7. **stock_link_platform 归类**：T20 三分未明确该 key，本稿归为租户级 UI 偏好；如判实例级请回填。
