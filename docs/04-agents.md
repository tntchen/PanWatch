# 04 · 业务 Agent 与提示词

> 范围：`src/agents/base.py`、`chart_analyst.py`、`daily_report.py`、`intraday_monitor.py`、`news_digest.py`、`premarket_outlook.py` + `prompts/*.txt`。
> 不含 `src/agents/tradingagents/`（另有文档覆盖）。

## 一句话总结

本模块是 PanWatch 的"AI 分析引擎"：5 个业务 Agent（盘前分析 / 盘中监测 / 收盘复盘 / 新闻速递 / 技术分析）统一继承 `BaseAgent`，按 `collect → build_prompt → analyze → should_notify → notify` 模板流程运行，把行情/技术/资金/新闻/持仓组装成 Prompt 交给 LLM，再把结果解析、落库（历史、建议池、预测追踪）并按需推送。

## 关键文件清单

| 文件 | 行数 | 作用 |
|---|---|---|
| `base.py` | 343 | `BaseAgent` 抽象基类 + 数据类（`PositionInfo`/`AccountInfo`/`PortfolioInfo`/`AgentContext`/`AnalysisResult`）+ 标准 `run()` 流程（含通知抑制、静默时段、全局去重） |
| `premarket_outlook.py` | 927 | 盘前展望：昨日复盘 + 隔夜美股 + SignalPack + 分层新闻上下文，全程 trace_id 日志 |
| `intraday_monitor.py` | 1058 | 盘中监测：单股模式、交易时段门禁、ATR 自适应异动阈值、AI 判断是否提醒、DB 节流 |
| `daily_report.py` | 787 | 收盘复盘：大盘指数（仅 A 股）+ 全量 SignalPack + 上下文质量分，落库最全 |
| `news_digest.py` | 579 | 新闻速递：按时间窗抓新闻、NewsCache 表跨进程去重、无新闻自动回退更长窗口 |
| `chart_analyst.py` | 277 | 技术分析：Playwright 截 K 线图 → 多模态（Vision）AI 分析，唯一重写 `analyze` 传图片的 Agent |
| `prompts/*.txt` | 29–122 行 | 5 个 Agent 各自的 system prompt，与代码一一对应 |

## 核心机制

### 1. 统一执行模板（base.py）

```
run(context) = collect(context) → analyze(context, data) → [notify 决策链]
```

- 子类必须实现 `collect()` 和 `build_prompt()`；`analyze()` 默认调 `context.ai_client.chat(system, user)` 并拼标题（`【显示名】股票1、股票2 等N只`）和 `\n\n---\nAI: <model_label>` 尾注。
- 通知决策链（顺序固定，每层都会在 `raw_data` 里记录跳过原因）：
  1. `context.suppress_notify` → `suppressed`
  2. `should_notify(result)`（子类可重写）→ 无需通知
  3. `NotifyPolicy.is_quiet_now()` → `quiet_hours`
  4. 全局去重 `check_and_mark_notify`（key = agent 名 + 标题 + 内容 hash）→ `deduped`；**先查后标**，仅发送成功后才 `mark=True`
  5. `notifier.notify_with_result()` 发送，失败记 `notify_error`
- 去重 TTL 默认值硬编码在 `_notify_dedupe_ttl_minutes()`：daily_report/premarket 12h、chart_analyst 6h、news_digest 60min、intraday 30min（intraday 另有每股节流）。可被 `NotifyPolicy.dedupe_ttl_minutes()` 覆盖。

### 2. 数据源组装（三个共用构建器）

所有 Agent 的 `collect()` 都围绕三个核心组件（在 `src/core/`，本范围外）：

- `SignalPackBuilder.build_for_symbols()` → 每股一个 pack：`quote / technical / capital_flow / news / events / position`。
- `ContextBuilder.build_symbol_contexts()` → 每股分层上下文：news（realtime/extended/history 三层 + history_topic）、kline_history、constraints（仓位约束）、memory（历史质量记忆）、data_quality（质量分）。各 Agent 时间窗参数不同（如盘前 realtime 12h / 盘中 6h / 日报 24h）。
- `save_*` 落库系列（见下节）。

数据完全取不到时，日报会 `raise RuntimeError` 中止；其他 Agent 多为降级（空列表 / 警告日志）继续。

### 3. 状态存储位置

| 存储 | 写入者 | 内容 |
|---|---|---|
| `analysis_history`（经 `save_analysis`） | 日报/盘前/新闻速递 | 全局分析（`stock_symbol="*"`），raw_data 内含压缩后的 context_payload、suggestions、prompt_context（截断 12000/2000 字） |
| 建议池（`save_suggestion`） | 4 个文本 Agent | 每股建议（action/signal/reason + prompt_context + ai_response），有效期不同：盘中 6h、盘前/新闻 12h、日报 16h |
| `agent_prediction_outcome`（`save_agent_prediction_outcome`） | 日报/盘前/盘中 | 每条建议按 horizon=1/5 天各存一条，用于事后准确率追踪；confidence = 数据质量分/100 |
| `agent_context_run`（`save_agent_context_run`） | 日报/盘前/盘中 | 上下文快照 + 质量分 |
| `NotifyThrottle` 表（SQLAlchemy） | 盘中监测 | 每股 `last_notify_at` + `notify_count`（跨天重置），节流比较用 UTC naive |
| `NewsCache` 表 | 新闻速递 | (source, external_id) 去重，内容截断 2000 字 |
| K 线截图文件 | chart_analyst | `ScreenshotCollector` 落盘，每次运行清理 24h 前旧图 |

### 4. AI 输出的结构化解析

- **结构化 JSON 块**：日报/盘前/新闻的 prompt 末尾强制要求追加 `<!--PANWATCH_JSON-->{...}<!--/PANWATCH_JSON-->`，经 `try_extract_tagged_json` / `strip_tagged_json` 解析（在 `src/core/signals/structured_output.py`）。解析出的 `suggestions` 优先于文本正则解析。
- **文本兜底解析**：`_parse_suggestions()` 用 5 级正则匹配（「」/【】→ 括号 → 行首 → 包含 → 股票名），每个 Agent 有自己的 ACTION_MAP（中文标签 → 标准 action），且对港股去前导 0（00700↔700）、A 股加 SH/SZ 前缀做了兼容。
- **盘中监测特殊**：prompt 要求**只输出一个 JSON 对象**（`try_parse_action_json`），解析失败还有 `_try_parse_loose_json` 宽松兜底（容忍 ```json fenced block）；若模型输出 JSON，则用 `_format_human_readable_content()` 转成人话再推送，避免渠道收到原始 JSON。`should_alert` 最终只在 action ∈ {buy, add, reduce, sell} 时为真。

### 5. 盘中监测的门禁与联动

- **交易时段门禁**：按**该股票所属市场**（MARKETS 定义）判断，非交易时段直接返回 `skip_reason`（`bypass_market_hours` 可跳过，供手动分析）。
- **ATR 自适应异动阈值**：`max(固定阈值 3%, 1.5×ATR%)`，由 `src/core/intraday_event_gate.py` 计算；固定阈值只是下限，高波动股不易误报。
- **事件门禁**：`event_only=True` 时调 `check_and_update`，但仅作为上下文信号（`data["event_gate"]`），**不阻断** AI 分析——降噪靠 `should_alert` + 节流。
- **TA 联动**：`analyze()` 末尾尝试 `tradingagents.auto_trigger.try_auto_trigger(stock)`，急涨急跌时异步触发深度分析（默认关闭，异常只记日志不影响主流程）。
- **单股模式**：`run_single()` 通过临时替换 `context.config.watchlist` 再 finally 恢复的方式实现——注意这是**直接改共享 config 对象**，并发下不安全。

### 6. chart_analyst 的特殊性

- 唯一依赖 `ScreenshotCollector`（Playwright）和多模态模型的 Agent；`period` 构造参数支持 daily/weekly/monthly。
- 重写 `analyze()` 把图片路径传给 `ai_client.chat(..., images=image_paths)`；无截图时返回固定文案且 `should_notify` 要求 content > 50 字。
- SignalPack 只取 quote + technical（不含资金流），失败仅警告。

## 对外接口与注册点

- **注册**：`server.py` 中 `AGENT_REGISTRY: dict[str, type]`（server.py:1107），key 即 `Agent.name`；`seed_agents()` 把内置配置写入 DB（`AgentConfig.schedule` 控制调度，无 schedule 则跳过）。
- **触发**：调度器按 DB 配置 cron 执行；手动触发走 `run()`（全量）或 `run_single()`（单股，仅 chart_analyst / intraday_monitor 实现）。
- **Prompt 加载**：每个 Agent 用 `Path(__file__).parent.parent.parent / "prompts" / "<name>.txt"` 读 system prompt——**移动文件位置或改 prompts 目录结构会直接报错**。
- **配置项**（构造参数，来自 DB AgentConfig）：intraday 的 throttle_minutes / price_alert_threshold / volume_alert_ratio / stop_loss_warning(-5%) / take_profit_warning(10%) / bypass_*；news_digest 的 since_hours(12) / fallback_since_hours(24)；chart_analyst 的 period。
- 输出消费方：通知渠道（notifier）、Web UI（analysis_history / 建议池 API）、tradingagents 联动。

## 二次开发扩展点与注意事项

1. **新增 Agent**：继承 `BaseAgent`，实现 `collect` + `build_prompt`，设 `name/display_name/description`；在 `server.py` 的 `AGENT_REGISTRY` 和 `AGENT_SEED_SPECS` 注册；在 `prompts/` 加同名 `.txt`；如需通知 TTL 特殊值，改 `base.py` 的 `_notify_dedupe_ttl_minutes()`。
2. **Prompt 与解析器强耦合**：改 `prompts/*.txt` 里的建议类型中文词（如"考虑加仓"）必须同步改对应 Agent 的 `*_ACTION_MAP`，否则文本兜底解析静默失效（JSON 路径不受影响）。JSON 块标签 `<!--PANWATCH_JSON-->` 一字不能差。
3. **建议池/预测落库在 `analyze()` 内**：不是基类行为。如果重写 `analyze()`（如 chart_analyst），历史、建议池、prediction_outcome 都不会自动落库——这是 chart_analyst 无建议池记录的原因。
4. **`run_single` 改共享 watchlist**：chart_analyst 和 intraday_monitor 都用"临时替换 + finally 恢复"模式，多股票并发触发同一 Agent 实例会互相污染 watchlist；改造时应改为传参而非改 config。
5. **去重语义**：`base.py` 去重 key 含标题，标题又含 watchlist 前 5 只股名——watchlist 变动会让 key 变化导致去重失效；且只有发送成功才 mark，发送失败会在 TTL 内反复重试发送。
6. **时区**：`NotifyThrottle` 用 UTC naive datetime 比较；`analysis_date` 用本地日期字符串，跨时区部署时注意 horizon 评估口径。
7. **大盘指数仅 A 股**：`_fetch_index_for_market` 对非 CN 市场返回空 list（与旧口径一致）；港股/美股自选股不会带出本地大盘指数（隔夜美股指数由盘前 Agent 单独取 usDJI/usIXIC/usINX）。
8. **日志噪音**：intraday_monitor 会把完整 prompt 和 AI 原始响应打进 INFO 日志（"=== Prompt for ..."），生产环境注意日志体积；premarket 有完整 trace_id 链路日志可参考。
9. **NewsCache 去重副作用**：`_dedupe_with_db` 在去重的同时把新新闻写入 NewsCache——即使本次 AI 分析失败，这些新闻下次也不会再出现，调试"为什么新闻速递空跑"时先查这张表。
10. **资金流向仅 A 股**：各 Agent 对 `capital_flow` 都做了市场判断/容错，港美股 pack 里 flow 可能为空 dict 或带 error，扩展时不要假设必有。

## 未确认事项

- `NotifyPolicy`（`src/core/notify_policy.py`）与 `notify_dedupe` 的具体实现（DB 还是内存）未在本范围内深读，仅按调用签名确认其语义。
- `AGENT_SEED_SPECS` 中各 Agent 默认 schedule / 参数未逐一核对（在 server.py 顶部，属另一模块范围）。
- `structured_output.py` 中 `TAG_START` 的确切字符串按 prompt 推断为 `<!--PANWATCH_JSON-->`，未读源文件确认。
