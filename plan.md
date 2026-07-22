# PanWatch 项目读懂计划

目标：通读 fork 的 PanWatch 开源项目，产出面向二次开发的结论性文档，存放在 `docs/` 目录。

## 阶段 1 — 并行阅读（10 个 explore/coder 子代理，各自写 docs/XX.md）

| # | 范围 | 产出 |
|---|------|------|
| 1 | server.py、src/web 基础设施（app/database/models/migrations）、config | docs/01-架构总览.md |
| 2 | src/web/api 行情数据类路由 | docs/02-api-行情数据.md |
| 3 | src/web/api 代理/系统/设置类路由 | docs/03-api-代理与系统.md |
| 4 | src/agents（5 个 agent）+ prompts/ | docs/04-agents.md |
| 5 | src/collectors + marketdata_client + 数据源路由 | docs/05-数据采集.md |
| 6 | src/core 通知与调度模块 | docs/06-通知与调度.md |
| 7 | src/core 策略/因子/回测/信号 | docs/07-策略与回测.md |
| 8 | src/core 模拟盘/组合/其余工具 | docs/08-核心工具与模拟盘.md |
| 9 | src/agents/tradingagents 集成 | docs/09-tradingagents集成.md |
| 10 | frontend/ 前端整体 | docs/10-前端.md |
| 11 | Dockerfile/build/Makefile/scripts/tests/贡献文档 | docs/11-部署运维.md |

## 阶段 2 — 汇总

Orchestrator 汇总各子代理返回的摘要，写 `docs/README.md` 索引，并向用户汇报项目全貌。
