# 02 · 行情与数据类 API 路由

> 阅读范围：`src/web/api/` 下的 quotes / klines / market / stocks / discovery / news / insights / context / dashboard，
> 以及股票列表缓存模块 `src/web/stock_list.py`（注意：它不在 `api/` 目录下）。
> 面向二次开发工程师，重结论轻罗列。未读到的细节会明确标注。

## 一句话总结

这组路由是前端"看盘"数据面：自选股 CRUD 与触发、实时行情、K线、市场指数、热门发现、新闻、AI 轻量解读（加仓评估/公告解读/今日策展）、上下文快照与预测后验、首页聚合看板。除 `/api/market/indices` 外全部需要登录。

## 关键文件清单

| 文件 | 路由前缀 | 作用 |
|---|---|---|
| `api/quotes.py` | `/api/quotes` | 单只/批量实时行情，走 `md_quote_rows`（marketdata 包） |
| `api/klines.py` | `/api/klines` | 日K + 周/月本地聚合 + K线摘要（`KlineCollector`） |
| `api/market.py` | `/api/market` | 主要市场指数（上证/深成/创业板/恒指/纳指/道指），**公共无需登录** |
| `api/stocks.py` | `/api/stocks` | 自选股 CRUD/排序/搜索/刷新列表/批量行情/市场交易状态/绑定 Agent/手动触发 Agent |
| `api/discovery.py` | `/api/discovery` | 热门股票、热门板块（东财实时源 + DB 快照兜底 + 港美股合成板块） |
| `api/news.py` | `/api/news` | 自选股相关新闻/公告聚合（按 DB 数据源配置），及已配置新闻源列表 |
| `api/insights.py` | `/api/insights` | 聚合卡片（行情+K线摘要+最新建议）、AI 加仓评估、AI 公告利好利空解读 |
| `api/context.py` | `/api/context` | 上下文快照/新闻话题/Agent 上下文运行/预测后验查询与评估、数据清理 |
| `api/dashboard.py` | `/api/dashboard` | 首页 overview 大聚合、AI"今日必读"策展、盘前/盘后简报 |
| `src/web/stock_list.py` | （被 stocks.py 调用） | 全市场股票列表缓存（JSON 文件）+ 模糊搜索 |

## 注册与横切机制（src/web/app.py）

- 所有路由在 `src/web/app.py` 统一 `include_router`；除 `auth`、`market` 外都挂 `dependencies=[Depends(get_current_user)]`。
- 全局中间件：`ResponseWrapperMiddleware`（统一响应包壳）+ CORS 全开；`redirect_slashes=False`（避免重定向丢 Authorization 头）。
- 行情统一入口是 `src/core/marketdata_client.py`：`get_market_data()` 进程级单例，`DbConfigProvider` 每次现查 DB `DataSource` 表（type/enabled/priority/supports_batch）映射成 vendor 配置；`md_quote_rows()` 是**同步函数**，async 路由用 `asyncio.to_thread` 调用。
- 新闻统一入口是 `NewsCollector.from_database()`（按 `DataSource` 表 type="news" 构建）。

## 各模块要点

### quotes.py（105 行）
- `GET /{symbol}?market=CN`、`POST /batch`。市场参数只接受 CN/HK/US（`MarketCode` 枚举），非法返回 400。
- 批量接口按市场分组后逐市场调 `md_quote_rows`；单只查不到返回 404，批量查不到返回全 null 字段（不报错）。

### klines.py（177 行）
- `GET /{symbol}`、`POST /batch`、`GET /{symbol}/summary`、`POST /summary/batch`。
- **全部是同步 `def` 端点**（FastAPI 会放线程池跑，不是 async）。
- 周线/月线不是从源取的，而是本地 `_aggregate_klines` 把日K按 ISO 周/月聚合（open=首日、close=末日、high/low=极值、volume=求和）。
- 数据来自 `KlineCollector`（`src/collectors/kline_collector.py`，走 marketdata 包，tencent → stooq(US)/eastmoney(CN/HK) 按 DataSource 优先级换源）。采集器自带进程内缓存：正缓存（按市场状态 TTL）+ 同标的并发合并锁（`_FETCH_LOCKS`，只联网一次）+ 失败负缓存（`_FAIL_UNTIL` 冷却窗口，防止源故障时刷屏）。

### market.py（73 行）
- `GET /api/market/indices`，公共接口。指数列表**硬编码**在 `MARKET_INDICES`（6 个）。
- 用腾讯 symbol 取数（`get_market_data().index_quotes`），再用 `response_symbol` 匹配回来——美股指数腾讯返回带点前缀（`.IXIC`/`.DJI`），这是刻意做的映射，改指数列表时注意三段 symbol 都要对。
- 取数异常时返回 `[]`（不是 5xx），单个指数缺失时字段为 null。

### stocks.py（505 行，本范围最重）
- 自选股 CRUD：`GET ""` 列表（按 sort_order）、`POST ""` 新增（重复 symbol+market 报 400，sort_order 取 max+1）、`PUT /reorder` 排序、`PUT/{DELETE} /{id}`。
- `DELETE` 有保护：存在持仓（`Position`）直接 400；因 SQLite 可能不启用 FK 级联，手动清理 `PriceAlertHit`/`PriceAlertRule`/`StockAgent`，避免孤儿记录。
- `GET /quotes`：自选股批量行情（按市场分组调 `md_quote_rows`，某市场失败仅记日志不影响其他）。
- `GET /markets/status`：用 `MARKETS` 定义判断 交易中/盘前/已收盘/午间休市/周末（不做节假日判断）。
- `GET /search` → `stock_list.search_stocks`；`POST /refresh-list` → 强制刷新列表缓存。
- `PUT /{id}/agents`：重建股票-Agent 绑定；只允许 `AGENT_KIND_WORKFLOW` 类 Agent（内部能力类报 400），并校验 `AgentConfig` 存在。
- `POST /{stock_id}/agents/{agent_name}/trigger`：手动触发，**最复杂的一个端点**：
  - 支持无绑定模式（`stock_id<=0` + `allow_unbound=true` + symbol/market，不落库，用 `SimpleNamespace` 伪装股票，且强制 `suppress_notify`）。
  - tradingagents 幂等去重：通过 `find_active_tradingagents_trace`（从 agents.py 导入）查同 symbol 在跑任务，有则复用 trace_id；`force_refresh=true` 跳过。
  - 预生成 trace_id（`man-{agent}-{symbol}-{ms}`），tradingagents 会先写一条 `ta_progress` 日志，保证前端轮询第一拍就能看到 running。
  - 默认后台 `threading.Thread` + `asyncio.run` 执行（立即返回 `queued:true`）；`wait=true` 同步等待。
  - **函数体内 `from server import trigger_agent_for_stock`** —— 对入口模块的反向依赖，重构 server.py 时易爆。

### src/web/stock_list.py（451 行）
- 缓存文件：`data/stock_list_cache.json`，TTL 7 天；`get_stock_list()` 缓存优先，miss 才联网。
- 列表源：东财 `clist/get` 分页并发拉取（A股/港股/美股/北交所四组参数，`fs` 市场过滤串各不相同），A股失败降级 akshare（15s 超时）。
- 搜索 `search_stocks`：**优先**东财 suggest 实时搜索（5s 超时），结果不足/失败时用本地缓存模糊匹配补全（代码前缀 > 名称包含 > 代码包含）。
- symbol 归一化：去 SH/SZ/BJ/HK/US 前缀、`00700.HK` 截断为 `00700`、港股 `zfill(5)`。美股按 `TypeUS` 过滤掉 ETF 等非股票类型。

### discovery.py（404 行）
- `GET /stocks`（mode=turnover|gainers）、`GET /boards`（CN 用真实行业板块）、`GET /boards/{code}/stocks`。
- 数据源 `EastMoneyDiscoveryCollector`；代理解析顺序：UI 配置的全局代理（`get_global_proxy()`）→ 环境变量 `Settings().http_proxy`。
- **进程内简单 TTL 缓存**（模块级 dict：stocks 45s、boards/board_stocks 60s），多 worker 部署时不共享。
- 兜底链：实时源失败 → `MarketScanSnapshot` 表最新快照（按 score_seed 排序）；两者都没有 → 503。
- 港美股没有真实板块数据，用 `_build_synthetic_boards` 从热股池合成 4 个主题桶（涨幅领先/成交额领先/波动活跃/自选关联），code 形如 `US_GAINERS`；`boards/{code}/stocks` 识别这种前缀走合成逻辑。

### news.py（155 行）
- `GET /api/news`：参数 symbols/names/hours(默认168=7天)/limit/filter_related/source。
- **names 优先于 symbols**（注释明说名称匹配比代码稳定），名称来自 DB `Stock` 表映射。
- 相关性过滤：东财公告（source=="eastmoney"）天然相关；否则看 item.symbols 命中或标题/内容含代码/名称关键词。
- `GET /sources`：返回 `DataSource` 表 type="news" 的启用配置。
- 来源显示名硬编码 `SOURCE_LABELS`（xueqiu/eastmoney_news/eastmoney）。

### insights.py（360 行）
- `POST /batch`：一次返回 quote + kline_summary（模块级 60s 简易缓存 `_KLINE_CACHE`，懒初始化）+ 最新建议（`suggestion_pool.get_latest_suggestions`，存 DB）。
- `POST /add-position-eval`：服务端先算摊薄成本（`(cur_q*cur_c + add_q*add_p)/new_q`），再拼实时行情+基本面（PE/换手/市值/振幅）+技术面+消息面上下文给 AI，要求输出"结论: 适合/谨慎/不适合"。`_parse_verdict` 取回复前 120 字符匹配，**`_VERDICTS` 顺序必须是"不适合"在"适合"前**（子串包含关系）。
- `POST /announcement-eval`：近 7 天公告（优先东财公告源）→ AI 按 `序号|利好/利空/中性|一句话` 格式逐条解读；模块级 `TTLCache` 6 小时（无数据短缓存 10 分钟）。
- 复用 `chat.py` 的内部函数 `_get_ai_client`/`_fetch_realtime_context`/`_fetch_technical_context`/`_build_stock_context`（下划线函数跨模块引用，改 chat.py 时注意）。

### context.py（214 行）
- 纯查询 + 两个动作端点，数据全部来自 `src/core/context_store.py` / `prediction_outcome.py`（DB 表，本范围未深读其实现）。
- `GET /snapshots/{symbol}`、`GET /topics/latest`、`GET /runs`、`GET /predictions`；`POST /predictions/evaluate`（触发待评估预测的后验计算）；`POST /cleanup`（按保留天数清理四类数据，默认快照/话题/运行 180 天、后验 365 天）。
- 时间格式统一走 `_format_datetime`：naive datetime 视为 UTC，转 `Settings().app_timezone` 输出 ISO。

### dashboard.py（482 行）
- `GET /overview`：首页大聚合，**设计原则是不打外网、只读 DB**——策略信号（`strategy_engine.list_strategy_signals/get_strategy_stats`）、持仓/账户（`Position`/`Account`/`Stock`）、市场脉搏（`MarketScanSnapshot` 最新快照 top18）、热点话题（`NewsTopicSnapshot`）、最新 AI 报告（`AnalysisHistory` 中 premarket_outlook/daily_report/news_digest 各取最新一条）、24h 错误数（`LogEntry`）。
- 信号按 `market:symbol` 分组去重（`_group_signals`：active 优先 > action 优先级 buy>add>watch/hold > rank_score）；行动清单要求 buy/add 且有入场价区间；风险清单打 flags（组合约束/高风险/非活跃/信号转弱）。
- **汇率硬编码**：HK=0.92、US=7.25 折算投入成本，改汇率要改代码（明显的坑）。
- `POST /curate`：把前端收集的候选事件（最多 20 条）交 AI 按重要度排序（`序号|重要度|一句话`），AI 失败按原序+递减重要度兜底。
- `GET /brief?type=premarket|eod`：复用对应 Agent 的最新 `AnalysisHistory` 报告，没有时返回 `{"empty": true}`。

## 状态存储位置一览

- **SQLite（经 `src/web/database.py`）**：`Stock`、`StockAgent`、`AgentConfig`、`Position`、`Account`、`PriceAlertRule/Hit`、`DataSource`、`MarketScanSnapshot`、`NewsTopicSnapshot`、`AnalysisHistory`、`LogEntry`、建议池（suggestion_pool）、上下文快照/话题/运行/预测后验（context_store/prediction_outcome）。
- **JSON 文件**：`data/stock_list_cache.json`（全市场股票列表，7 天 TTL）。
- **进程内存**：discovery 的 `_cache`（45/60s）、insights 的 `_KLINE_CACHE`（60s）与 `_ANN_CACHE`（6h TTLCache）、KlineCollector 的正/负缓存与取数锁、marketdata 单例。**全部不跨进程共享**，多实例部署时各自独立。

## 二次开发扩展点与注意事项

1. **加新数据源**：不要改这些路由，改 `DataSource` 表配置 + marketdata 包的 vendor 实现即可（quotes/klines/news 都走这套优先级换源机制）。
2. **同步 vs 异步不一致**：quotes 是 async + `to_thread`，klines 是同步 `def`（线程池）。加新端点时照所在文件的风格走，混用容易引入事件循环阻塞。
3. **缓存键与 TTL 分散**：discovery/insights/kline 各有私有缓存，排查"数据不刷新"问题时要逐个想；没有统一失效入口。
4. **循环/反向依赖**：`stocks.py` 函数体内 `from server import trigger_agent_for_stock`；`insights.py` 引用 `chat.py` 的下划线私有函数；`dashboard.py` 局部 import `Account`。重构 server.py / chat.py / models.py 时这些是第一批炸点。
5. **硬编码清单**：`MARKET_INDICES`（指数）、`SOURCE_LABELS`（新闻来源名）、dashboard 汇率（0.92/7.25）、`_VERDICTS` 顺序、合成板块桶。改其中任何一个都没有配置入口。
6. **删除股票的级联是手动的**：新增与 Stock 关联的表时，记得同步补 `stocks.py` 的 delete 逻辑，否则产生孤儿记录。
7. **响应包壳**：所有响应经过 `ResponseWrapperMiddleware`，前端看到的不是路由原始返回值；调试时直接 curl 原始路由要意识到这一点（中间件实现未在本范围深读）。
8. **错误口径不统一**：market 指数失败返回 `[]`，quotes 单只失败 404，discovery 全链路失败 503，AI 类失败 502。前端各自有兜底，改状态码前检查前端调用点。
9. **AI 输出解析脆弱**：insights/dashboard 的 AI 结果都靠 `|` 分隔 + 前 N 字符子串匹配解析，换 prompt 或模型时容易静默降级为"未知/中性/兜底排序"。
10. **`/api/health` 注册了两次**（health.router 与 app.py 内联函数同路径，内联在后），属无害冗余，但清理时注意别只删一处造成行为变化。
