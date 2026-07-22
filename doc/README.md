# PanWatch 项目解读文档索引

本目录是 AI 通读 PanWatch 源码后产出的**结论性文档**，面向二次开发。
生成日期：2026-07-21（基于 fork 当前 main 分支快照）。

## 项目一句话

**盯盘侠 PanWatch**：自托管 AI 盯盘助手 —— FastAPI 单进程后端（API + 调度器 + 静态托管）+ React/Vite 前端，覆盖 A 股/港股/美股的实时监控、持仓管理、AI 分析（5 个业务 Agent + TradingAgents 9-Agent 深度决策）、模拟盘、9 渠道推送，SQLite 持久化，单 Docker 容器部署。

## 文档清单

| 文档 | 内容 |
|------|------|
| [01-架构总览.md](01-架构总览.md) | 启动流程、Agent 双层注册（AGENT_REGISTRY + DB seed）、三级配置解析、SQLite 约 30 张表、迁移三层机制 |
| [02-api-行情数据.md](02-api-行情数据.md) | quotes/klines/market/stocks/discovery/news 等行情类路由、K线缓存与聚合、dashboard 聚合 |
| [03-api-代理与系统.md](03-api-代理与系统.md) | agents/chat/channels/settings/auth 等管理平面路由、单用户 JWT、调度预览与手动触发 |
| [04-agents.md](04-agents.md) | BaseAgent 模板（collect→build_prompt→analyze→notify）、5 个业务 Agent、通知五层决策链、结构化输出解析 |
| [05-数据采集.md](05-数据采集.md) | packages/marketdata 本地包是真正实现、collectors 只是 shim、数据源主备链与注册/健康检查 |
| [06-通知与调度.md](06-通知与调度.md) | 9 渠道通知、四道通知闸、四套调度器、cron 时区/星期归一化陷阱 |
| [07-策略与回测.md](07-策略与回测.md) | 候选池→信号→后验→权重自校准闭环、六因子打分、组合约束、纯 Python 回测内核（未接 API） |
| [08-核心工具与模拟盘.md](08-核心工具与模拟盘.md) | 模拟盘引擎（分市场子池、五种平仓规则）、入场候选双源合并、ContextBuilder、组合诊断 |
| [09-tradingagents集成.md](09-tradingagents集成.md) | TradingAgents 软依赖 + monkeypatch 适配、5 档评级、预算护栏、数据通道与并发隔离 |
| [10-前端.md](10-前端.md) | pnpm workspace 三层（api/base-ui/biz-ui）、9 大页面、无状态库靠 CustomEvent、分享卡/PWA |
| [11-部署运维.md](11-部署运维.md) | 单容器两阶段构建、版本发布流（tag→CI→Docker Hub）、测试体系、发版检查脚本 |

## 二次开发计划（进行中）

| 文档 | 内容 |
|------|------|
| [12-融合计划-东芯方案与持仓交易.md](12-融合计划-东芯方案与持仓交易.md) | 唯一事实来源：东芯方案跟踪 + 持仓交易化的分期执行计划（v1.3，含决策表与回滚方案） |
| [13-方案评审-架构研发运维.md](13-方案评审-架构研发运维.md) | 三角评审报告：3 项高危误判（龙虎榜重复建设等）+ 10 项中危修订，已回填 12 号文档 |
| [14-测试与验收方案.md](14-测试与验收方案.md) | QA 评审：分 Phase 测试清单、go/no-go 门禁、测试数据策略、5 项原缺失验收项，已回填 12 号文档 |
| [15-跟踪数据能力核查.md](15-跟踪数据能力核查.md) | Kimi 跟踪简报数据点 × marketdata 覆盖核查：核心输入现成可用，4 小缺口入 P3e，2 中缺口（席位明细/web 检索）待决策 |

## 二次开发最常碰的注册点（跨文档共识）

1. **新增 Agent**：`src/agents/` 建类继承 BaseAgent → `server.py` 的 `AGENT_REGISTRY` + `AGENT_SEED_SPECS` 两处注册 → `prompts/` 加提示词。
2. **新增数据源**：改 `packages/marketdata` 包（vendors + registry）→ `server.py` 的 `DATA_SOURCE_SEEDS` 加 seed（不进 seed 会被 reconcile 当孤儿删掉）。
3. **新增通知渠道**：`notifier.py` 的 `CHANNEL_TYPES` 白名单。
4. **新增 API**：`src/web/api/` 建路由 → `src/web/app.py` 注册（注意固定路由先于通配路由）；前端必须走 `@panwatch/api` 包。

## 全局性注意事项

- 配置优先级：UI 设置 > .env > 代码默认（代理配置例外）。
- DB 时间一律 UTC-naive，调度用 `Settings().app_timezone`。
- SQLite 写锁敏感：遵循 no_autoflush、分批 commit 的既有模式。
- 红涨绿跌配色（前端）、`llm_provider` 必须写 `"openrouter"`（TradingAgents）等具体坑见各分文档。
- 仓库原有的 `docs/` 目录只有截图和打赏图；本 `doc/` 目录才是解读文档。
