# 盯盯（PanWatch 二次开发版）

自托管 AI 盯盘助手 —— A 股 / 港股 / 美股实时监控、持仓台账、方案驱动的全天候跟踪分析。

本仓库是开源项目 [TNT-Likely/PanWatch](https://github.com/TNT-Likely/PanWatch) 的个人二次开发版本，在上游「实时行情 + AI 分析 + 多渠道推送」的基础上，围绕**持仓交易**与**个股操作方案的全天候跟踪**做了深度扩展。

## 本仓库与上游的区别

### 💰 持仓交易化

- 持仓页直接 **加仓 / 减仓**，完整交易流水台账
- Decimal 精确成本引擎：移动加权成本 + 已实现盈亏逐笔核算
- 多券商账户独立管理、汇总展示

### 📋 方案档案（Playbook）

- 把个股的完整操作方案结构化为档案：建仓批次、做 T 区间、防守纪律、冻结条件、关键日历
- 触发器扩展：换手率、主力净流入、连续 N 日收盘价，规则可关联档案，命中通知自带方案提示
- 个股详情页「方案」面板，支持版本管理与 JSON 导入

### ⏰ 全天候跟踪简报

| 时点 | 简报 | 内容 |
|------|------|------|
| 10:00 | 早盘简报 | 可执行 / 观望 / 冻结 三选一结论 |
| 14:45 | 尾盘简报 | 持有 / 执行右侧批 / 做T减出 / 减仓 / 观望 五选一 + 数量与价格区间 |
| 18:00 | 收盘复盘 | 触发器逐项状态、流水精确盈亏、当日操作 vs 方案纪律点评、次日预案与日历提醒 |
| 盘中 | 异动监测 | 方案价位穿越即事件（做T区/防线/批次触发区），按方案语境解读 |

- 简报输出带**契约校验**：缺结论词、缺数量或价格区间则不推送，防止 AI 输出空泛建议
- 所有分析自动注入持仓台账与近期流水，盈亏按流水精确口径计算

### 📄 研究报告归档

- 个股深度研究报告入库检索，Word 附件直接下载

### 🧭 规划中

- 多租户改造（调研已完成，见 [docs/17-多租户改造调研与计划.md](docs/17-多租户改造调研与计划.md)）

## 上游功能（完整保留）

<details>
<summary><b>智能 Agent 系统</b></summary>

| Agent | 触发时机 | 功能 |
|-------|---------|------|
| **盘前分析** | 每日开盘前 | 综合隔夜美股、新闻消息、技术形态，给出今日操作策略 |
| **盘中监测** | 交易时段实时 | 监控异动信号，RSI/KDJ/MACD 共振时推送提醒 |
| **盘后日报** | 每日收盘后 | 复盘当日走势，分析资金流向，规划次日操作 |
| **新闻速递** | 定时采集 | 抓取财经新闻，AI 筛选与持仓相关的重要信息 |

</details>

<details>
<summary><b>TradingAgents 多 Agent 深度决策</b></summary>

接入 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 多 Agent 投资决策框架，在持仓页点 🧠 图标即可触发：

- **4 类分析师**（技术 / 情绪 / 新闻 / 基本面） → **看多看空辩论** → **风控审查** → **PM 整合决策**
- 3-5 分钟输出完整推理链，结论同步推送到 Telegram / 微信 / 钉钉

</details>

<details>
<summary><b>专业技术分析</b></summary>

- **趋势指标**：MA 多空排列、MACD 金叉死叉、布林带突破
- **动量指标**：RSI 超买超卖、KDJ 钝化与背离
- **量价分析**：量比异动、缩量回调、放量突破
- **形态识别**：锤子线、吞没形态、十字星等 K 线形态
- **支撑压力**：自动计算多级支撑位和压力位

</details>

<details>
<summary><b>多市场 & 全渠道通知</b></summary>

- **覆盖市场**：A 股、港股、美股实时行情
- **通知渠道**：Telegram / 企业微信 / 钉钉 / 飞书 / Bark / 自定义 Webhook
- **价格提醒**：价格、涨跌幅、成交额、量比等条件组合（AND / OR），冷却时间、日触发上限、重复触发模式

</details>

## 快速开始

```bash
docker run -d \
  --name panwatch \
  -p 8000:8000 \
  -v panwatch_data:/app/data \
  chentnt/panwatch:latest
```

访问 `http://localhost:8000`，首次使用设置账号密码即可。

说明：镜像内已包含 Playwright 运行所需的系统依赖；Chromium 浏览器会在容器首次启动时自动下载并安装到挂载卷（默认 `/app/data/playwright`），首次启动可能需要几分钟且需要网络可达。不需要截图等浏览器能力时，可设置 `PLAYWRIGHT_SKIP_BROWSER_INSTALL=1` 跳过。

<details>
<summary>环境变量</summary>

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `AUTH_USERNAME` | 预设登录用户名 | 首次访问时设置 |
| `AUTH_PASSWORD` | 预设登录密码 | 首次访问时设置 |
| `JWT_SECRET` | JWT 签名密钥 | 自动生成 |
| `DATA_DIR` | 数据存储目录 | `./data` |
| `TZ` | 应用时区（影响 Agent 调度触发时间与时间展示） | `Asia/Shanghai` |
| `PLAYWRIGHT_SKIP_BROWSER_INSTALL` | 跳过首次 Chromium 安装（不需要截图时可用） | 未设置 |
| `LOG_LEVEL` | 控制台日志级别 | `INFO` |
| `HTTP_PROXY` / `HTTPS_PROXY` / `http_proxy` | 出站 HTTP 代理 | 未设置 |

</details>

<details>
<summary>本地开发</summary>

**环境要求**：Python 3.10+ / Node.js 18+ / pnpm

```bash
# 一键开发（推荐）
make dev-api          # 启动后端（自动 venv+依赖，监听 :8000）
make dev-web          # 启动前端（自动 pnpm install，监听 :5183）

# 或手动
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python server.py                              # 后端 :8000

cd frontend && pnpm install && pnpm dev       # 前端 :5183
```

前端 dev server 跑在 `http://localhost:5183`，并把 `/api` 代理到 `127.0.0.1:8000`。

</details>

<details>
<summary><b>技术栈</b></summary>

**后端**：FastAPI / SQLAlchemy / APScheduler / OpenAI SDK

**前端**：React 18 / TypeScript / Tailwind CSS / shadcn/ui

</details>

## 项目文档

`docs/` 目录下有完整的架构解读与二开记录：

- [docs/README.md](docs/README.md) — 项目解读文档索引（架构 / API / Agent / 采集 / 通知调度 / 前端 / 部署）
- [docs/12-融合计划-东芯方案与持仓交易.md](docs/12-融合计划-东芯方案与持仓交易.md) — 二开主计划（阶段门禁制，含全部执行记录）
- [docs/16-项目状态卡.md](docs/16-项目状态卡.md) — 当前项目状态快照
- [docs/17-多租户改造调研与计划.md](docs/17-多租户改造调研与计划.md) — 多租户改造调研（待决策）

## 致谢

本项目 fork 自 **[TNT-Likely/PanWatch](https://github.com/TNT-Likely/PanWatch)**。感谢原作者开源了这样一个架构清晰、功能完整的 AI 盯盘助手——行情采集、Agent 框架、通知体系、模拟盘等核心能力全部来自上游的扎实工作，本仓库的二次开发得以站在一个很高的起点上。如果这个项目对你有帮助，请给上游项目点一个 ⭐ Star。

## License

[MIT](LICENSE)（与上游一致）
