# 09 · TradingAgents 多 Agent 决策集成

> 范围：`src/agents/tradingagents/`（13 个文件）+ `tests/test_tradingagents_*.py`（8 个）
> 代码注释里引用的设计文档 `.docs/tradingagents/02-technical-design.md` **在当前仓库中不存在**（已确认），本文即事实来源。

## 模块职责一句话

把开源多 Agent 投资决策框架 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)（4 分析师 → 看多看空辩论 → 研究主管 → 交易员 → 风控辩论 → PM 共 9 个节点）以 **软依赖 + monkeypatch** 的方式适配进 PanWatch：数据走 PanWatch Provider（A 股/港股专用），LLM 走 PanWatch AI Service，产出映射为 `AnalysisResult` 并落库/推送/驱动模拟盘。

## 关键文件清单

| 文件 | 作用 |
|---|---|
| `agent.py` (565 行) | 核心。`TradingAgentsAgent(BaseAgent)`：重写 `analyze()`，串联可用性检测 → 同日缓存 → 预算检查 → LLM 配置 → 线程池跑同步 TA 图（硬超时）→ 结果映射 → 三重落库（AnalysisHistory / StockSuggestion / StrategySignalRun）。 |
| `toolkit_adapter.py` (835 行) | 数据注入枢纽。monkeypatch 上游 `route_to_vendor` 和 `load_ohlcv`，让 A 股(6 位数字)/港股(5 位数字)请求返回 PanWatch 已拉好的 quote/K线/新闻/资金流/财报；美股透传 yfinance。用 `ContextVar` 保证并发数据隔离，用引用计数+锁保证嵌套 patch 安全。 |
| `result_mapper.py` | `final_state` → `AnalysisResult`。5 档评级（buy/overweight/hold/underweight/sell）→ 3 档 action；评级解析优先级：**PM 正文显式标签 > 上游 decision > 文本模糊扫描**；置信度正则识别中/英/全角冒号，抓不到按评级推导。 |
| `llm_adapter.py` | `build_ta_llm_config`：继承上游 `DEFAULT_CONFIG`，覆盖 `llm_provider="openrouter"`、`backend_url=ai_client.base_url`、deep/quick 双模型；`inject_api_key_env` 把 key 写入 `OPENROUTER_API_KEY`/`OPENAI_API_KEY`/`DEEPSEEK_API_KEY` 环境变量。 |
| `cost_tracker.py` | `check_budget`（SQL 聚合本月 `AnalysisHistory.raw_data.cost_usd`）、`estimate_cost`（按分析师数/辩论轮次估 token 成本，返回 2~5 倍区间）、`get_today_cache_key`。 |
| `progress.py` | `PanWatchProgressHandler`（LangChain `BaseCallbackHandler` 子类）：把 LLM/节点事件经 `log_context` 写成 `event=ta_progress` 日志；`aggregate_progress` 把日志聚合为 9 阶段进度（`STAGES_ORDER`）。 |
| `portfolio_context.py` | 渲染「标的元信息 + 用户持仓」文本，通过上游官方扩展通道 `past_context`（PM 节点会读）注入 —— 用 `patch_propagator` 猴补 `create_initial_state`。关键防呆：显式告诉 LLM `601127 = 赛力斯`，防止从 A 股 ticker 瞎编公司名。 |
| `financial_data.py` | akshare `stock_financial_abstract` 拉 A 股真实财报（最近 6 期），4 个 `render_*` 函数渲染成基本面/利润表/资产负债表/现金流文本，注入对应工具。仅支持 6 位数字 A 股，失败返回 `None` 降级到 quote 轻量基本面。 |
| `auto_trigger.py` | 盘中急涨/急跌联动：`should_auto_trigger`（阈值默认 5%，冷却 24h，预算检查，读 `AgentConfig.raw_config.auto_trigger`）+ `fire_and_forget_trigger`（异步调 `server.trigger_agent_for_stock`）。默认关闭。 |
| `paper_trading_bridge.py` | TA 的 BUY/ADD 决策 → 写 `StrategySignalRun`（entry ±2%、止损 -5%、目标 +10%），模拟盘引擎周期性扫描该表自动开仓。SELL 不开仓；同日同标的 upsert（`source_candidate_id=0` 作为 TA 专用 sentinel）。 |
| `history_comparison.py` | 历史决策回测：近 N 天 `AnalysisHistory` 记录 + KlineCollector K线，算 1d/5d/20d 收益与命中率（buy 后涨 / sell 后跌 / hold 横盘 ±2% 以内算命中）。 |
| `backfill.py` | 启动时跑一次，把最近 7 天 TA 历史回填到 `stock_suggestions`（依赖 `save_suggestion` 内部去重，幂等）。 |
| `langchain_compat.py` | 猴补 `create_tool_call` 与 `AIMessage.__init__`：把小模型（如硅基流动 Qwen）返回的 `tool_calls.args` JSON 字符串自动转 dict。幂等。 |

## 核心机制 / 数据流

```
collect():  marketdata 包并发拉 quote/kline(120d)/capital_flow/events(30d)
            + A 股 akshare 财报 + KlineCollector 预算技术指标
                ↓ (dict 存入 panwatch_data_context 的 ContextVar)
analyze():  同日缓存命中? → 预算 reject/warn/continue → build_ta_llm_config
                ↓
_run_tradingagents_sync (asyncio.to_thread + wait_for 硬超时):
    apply_compat_patches → inject_api_key_env
    with patch_route_to_vendor(), panwatch_data_context(...):
        TradingAgentsGraph(..., callbacks=[progress_handler])
        _inject_graph_callbacks (补 LangGraph 节点级回调)
        patch_propagator (注入持仓上下文到 past_context)
        graph.propagate(symbol, date)  # 3-5 分钟
                ↓ {decision, final_state, cost_usd}
map_state_to_result → AnalysisResult
                ↓
save_analysis (AnalysisHistory, 同标同日复跑覆盖)
save_suggestion (StockSuggestion, expires 24h)
maybe_emit_paper_trading_signal (StrategySignalRun, 可选)
```

**状态存储**：

- `analysis_history` 表（JSON `raw_data`）：完整结果、suggestion、cost_usd、debate_history、analyst_reports、toolkit_diagnostic、price_at_analysis。既是缓存源也是预算统计源。
- `stock_suggestions` 表：建议池，前端持仓页/关注列表徽章。
- `strategy_signal_runs` 表：模拟盘信号（仅 `emit_paper_trading_signal=true`）。
- `log_entries` 表：`event=ta_progress`（阶段进度）+ `event=ta_toolkit`（数据注入 hit/miss/passthrough 诊断），按 `trace_id` 归属。
- `agent_configs.config`：TA 全部运行参数（见下「配置项」）。

## 对外接口

**注册点**：`server.py` `AGENT_REGISTRY["tradingagents"]`；seed 配置在 `src/core/agent_catalog.py:130-154`（`enabled=False`，`execution_mode="single"`，需手动触发）。启动时 lifespan 跑 `backfill_tradingagents_suggestions(days=7)`。

**配置项**（`AgentConfig.config`，server.py:1369-1375 实例化时整体展开为构造参数）：`analyst_types`、`debate_rounds`、`monthly_budget_usd`(10.0)、`over_budget_action`(reject/warn/continue)、`cache_ttl_hours`(12)、`output_language`、`deep_model`/`quick_model`、`timeout_minutes`、`emit_paper_trading_signal`(False)、`auto_trigger`（嵌套 dict：`enabled/change_pct_threshold/cooldown_hours`）。

**API 路由**（`src/web/api/agents.py`）：

- `GET /tradingagents/running?stock_symbol=` — 后端权威源判断是否有在跑任务（`running/success/failed/none/stale`，5 分钟无日志视为 stale）
- `GET /tradingagents/latest?stock_symbol=` — 最近一次完整结果（含 raw_data，DeepAnalysisModal 用）
- `GET /tradingagents/analysis`、`GET /tradingagents/analysis/pdf` — 历史详情 / PDF 导出
- `GET /tradingagents/history-comparison?stock_symbol=&market=&days=` — 决策命中率对比
- `GET /tradingagents/budget` — 本月预算用量
- `GET /runs/{trace_id}/progress` — 9 阶段进度 + toolkit 诊断（前端轮询）

**联动入口**：`intraday_monitor.py:856` 每次分析后调 `try_auto_trigger(stock)`。

## 二次开发扩展点与注意事项

**扩展点**：

1. **新增分析师**：改 `VALID_ANALYSTS`（llm_adapter.py:20），同时看上游 `selected_analysts` 合法值；构造函数会校验非法值直接 raise。
2. **新增数据注入方法**：在 `_serve_from_panwatch` 按 method 名关键词加分支（indicator/stockstats/news/capital/fundamental/income/balance/cashflow），未识别 raise `NotImplementedError` → 自动放行上游 vendor，**失败返回空串而非抛异常**（单工具缺数据不拖垮整轮）。
3. **改评级档位**：只动 `RATING_LABEL_MAP` / `RATING_ACTION_MAP`（result_mapper.py），`DECISION_LABEL_MAP` 是其别名，下游 paper_trading_bridge 也引用。
4. **新增 TA 触发源**：复用 `fire_and_forget_trigger(stock, source_agent=...)`，自带 trace_id 与异步兜底。

**踩坑警示**：

- **`llm_provider` 不能写 `"openai"`**：TA 会强制启用 OpenAI Responses API（`/v1/responses`），硅基流动/智谱等兼容端点 404。必须用 `"openrouter"` + `backend_url` 覆盖。
- **monkeypatch 必须覆盖所有 import sites**：上游工具模块用 `from ... import route_to_vendor`，是 import-time binding，只 patch 源头模块无效。新增上游版本若改了工具模块路径，需同步 `_ROUTE_TO_VENDOR_IMPORT_SITES` / `_LOAD_OHLCV_IMPORT_SITES`。
- **并发数据隔离依赖 ContextVar**：`_PANWATCH_DATA` 曾是模块级 dict，导致两只股票并发分析互相污染（广汽混入赛力斯 K线）。改数据通道时不要退回模块级全局（`test_tradingagents_toolkit_isolation.py` 回归守护）。
- **评级解析以 PM 正文为权威**：上游 `propagate()` 返回的 decision 是二次提炼会失真（正文"卖出"返回"HOLD"）。全角冒号"："、中文评级词都必须覆盖（`test_tradingagents_5_tier_rating.py`）。
- **成本记账两条链**：`progress_handler._total_cost`（按 deepseek 单价实时估算）与 `_extract_cost_from_graph`（读上游字段，可能为 0），fallback 用 `estimate_cost` 均值。换模型后 `progress.py` 里硬编码单价 `_PRICE_PER_M_*` 会低估/高估。
- **env var 竞态**：`inject_api_key_env` 是进程级，多 AI service 并发跑不同 key 会互相覆盖（代码注释承认，P0 假设单 service）。
- **HK 回退策略不对称**：港股先试 yfinance（转 `0241.HK`）再 fallback PanWatch；A 股直接走 PanWatch 且 `load_ohlcv` 拉空时**故意不回退 Yahoo**，直接抛 `NoMarketDataError` 报真因。
- **缓存语义**：同日同标的复跑直接返回 AnalysisHistory 缓存（`from_cache=True`），`force_refresh=True` 才跳过；触发 API 侧另有幂等：已有在跑任务则返回现有 trace_id 不启新（`find_active_tradingagents_trace`）。
- **软依赖**：`tradingagents` 不在 PyPI，需 git clone + `pip install -e`；未安装时构造不炸，`analyze()` 抛 `TradingAgentsUnavailable` 带安装指引。升级上游版本时优先跑 `tests/test_tradingagents_*.py` 全套（尤其 propagate 签名 TypeError 兜底、route_to_vendor 存在性检测两条容错路径）。
