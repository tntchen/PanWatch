# MT-P0 查询审计清单 · 运行时调度与 Agent 链

> 日期：2026-07-22 ｜ 阶段：MT-P0（设计稿，无代码改动）
> 对应决策点：T3 / T4 / T7 / T9 / T13 / T15 / T16 / T17 / T18 / T19 / T20 / T21
> 呼应评审项（docs/20）：C1 / C2 / C6 / C7 / C8 / M2 / M5 / M12 / M13
> 范围：`server.py` 调度与装配链、`src/core/scheduler.py`、`src/core/context_builder.py`、`src/agents/`（base/intraday_monitor/morning_brief/daily_report）、`src/core/notify_dedupe.py`、`src/core/intraday_event_gate.py`、`src/core/price_alert_engine.py`、`src/core/paper_trading_engine.py`、`src/core/context_scheduler.py`、`src/core/context_store.py`、`src/web/log_handler.py`、`src/core/log_context.py`
> 所有行号均经 2026-07-22 实读代码核对。

---

## 0. 证据基线与简报行号修正

| 简报给定 | 实读核对结果 | 说明 |
|---|---|---|
| `server.py` resolve_ai_model:1045 | 函数定义在 `server.py:1022`；:1045 是函数体内 `AIModel.is_default` 查询行 | 本文统一引用 :1022-1069 |
| `server.py` resolve_notify_channels:1106 | 函数定义在 `server.py:1072`；:1106 是 `is_default` 过滤行 | 本文统一引用 :1072-1116 |
| `server.py` load_portfolio_for_agent:881 | 函数定义在 `server.py:866`；:881 是 `Account.enabled==True` 全量查询行 | 本文统一引用 :866-940 |
| `src/core/log_handler.py` | 实际路径为 `src/web/log_handler.py`（`src/core/` 下只有 `log_context.py`） | 两文件均纳入审计 |
| `src/core/scheduler.py` job id:51-58 | 确认：`scheduler.py:51-58`，`id=agent.name`(:55) + `replace_existing=True`(:57) | — |
| `context_builder.py` :438 / :487 / :539 / :556 | 确认：:438 为 `_load_playbook_summary` 内 `Stock.symbol==X` 解析行；:487 持仓约束、:539 playbook 摘要、:556 constraints 写入快照 payload | — |
| `intraday_event_gate.py` :206-227 / :309-331 | 确认：:206-227 `_load_playbook_levels`（:216 symbol 解析）；:309-331 `pb_fired` 冷却（:322 冷却键） | — |
| `paper_trading_engine.py` :240-241 | 确认：`_get_or_create_account` 全库 `.first()` | — |
| `price_alert_engine.py` :617 | 确认：模块级 `ENGINE = PriceAlertEngine()` 单例 | — |

---

## 1. 身份穿透链逐点设计（T16 / T17 / C7）

### 1.1 现状调用链（证据化）

```
APScheduler job(id=agent.name, args=[agent.name])            scheduler.py:51-58
  └─ AgentScheduler._run_agent(agent_name)                   scheduler.py:64
       └─ context = self.context_builder(agent_name)         scheduler.py:86（类型签名 :26/:28）
            └─ server.build_context(agent_name, stock_agent_id=None)   server.py:1183-1204
                 ├─ load_watchlist_for_agent(agent_name)     server.py:1186 → def :746-774
                 │     └─ StockAgent 全表 + Stock.id IN(...) 无 tenant     :750-758
                 ├─ load_portfolio_for_agent(agent_name)     server.py:1187 → def :866-940
                 │     └─ Account.enabled 全量(:881) + Position(:886-893) 无 tenant
                 ├─ resolve_ai_model(agent_name, sa_id)      server.py:1190 → def :1022-1069
                 │     └─ is_default 全局唯一回退(:1043-1047)
                 ├─ resolve_notify_channels(agent_name,sa_id) server.py:1192 → def :1072-1116
                 │     └─ is_default 全局(:1102-1110)
                 ├─ _build_notifier(channels)                server.py:1119-1159
                 │     └─ notify_* 读 app_settings 全局 KV(:1123-1128)
                 └─ _build_ai_client(model, service, proxy)  server.py:1162-1180
                       └─ 无 model 时回退 env 凭证(:1173-1180)
```

旁路（不在 build_context 链上，C7 点名）：

| 旁路点 | 位置 | 现状 |
|---|---|---|
| 通知去重自建 Session | `src/core/notify_dedupe.py:54` | 无 tenant 概念 |
| 快照读写自建 Session | `src/core/context_store.py:29,75,104,155,175,205,232,267,295,325,356` | 无 tenant |
| 日志落库 | `src/web/log_handler.py:74-87,120` | LogEntry 无 tenant 字段（模型 `models.py:236-256`） |
| 事件门状态文件 | `src/core/intraday_event_gate.py:26-27` | DB 外落盘，迁移管不到 |
| 记录运行 | `src/core/agent_runs.py:10-58` | agent_runs 无 tenant 列 |

### 1.2 目标签名与逐点改造表

核心签名变更（MT-P0.5 机制点，C7「前置重构，不可打补丁」）：

| # | 点位 | 现状位置 | 改造方式 | 依据 |
|---|---|---|---|---|
| P1 | `AgentContext` 载体 | `src/agents/base.py:119-133` | 新增字段 `tenant_id: int = 1`（T18 默认租户），所有链路的 tenant 最终落在此载体 | T16/T18 |
| P2 | `context_builder` 类型签名 | `scheduler.py:26,28-30` | `Callable[[str], AgentContext]` → `Callable[[int, str], AgentContext]`（首参 tenant_id） | C7 |
| P3 | `build_context` 签名 | `server.py:1183` | `build_context(tenant_id, agent_name, stock_agent_id=None)`；函数体 4 个 load/resolve 全部显式下传 tenant_id | C7 |
| P4 | `load_watchlist_for_agent` | `server.py:746-774` | 加参 `tenant_id`；`StockAgent` 查询(:750-752)加 `tenant_id==` 过滤；`Stock.id IN` (:758) 由 stock_agents 父行派生，天然租户内（T18 子行仅父派生不变量） | T15/T18 |
| P5 | `load_portfolio_for_agent` | `server.py:866-940` | 加参 `tenant_id`；`Account.enabled`(:881) 加 tenant 过滤；`Position`(:886-893) 经 account 派生 | T18 |
| P6 | `load_portfolio_for_stock` | `server.py:943-999` | 加参 `tenant_id`；解析 stock 后**断言** `stock.tenant_id == tenant_id`（防御性，违例即空组合+告警日志） | T16 |
| P7 | `resolve_ai_model` | `server.py:1022-1069` | 加参 `tenant_id`，解析链重写见 §6.1 | T4/T13 |
| P8 | `resolve_notify_channels` | `server.py:1072-1116` | 加参 `tenant_id`，解析链重写见 §6.2 | T4/T21 |
| P9 | `_build_notifier` | `server.py:1119-1159` | 加参 `tenant_id`；`notify_*` 配置(:1123-1128)改读**租户级** app_settings（T20 三分） | T20 |
| P10 | `_build_ai_client` env 回退 | `server.py:1173-1180` | env 凭证回退**仅当 tenant 为管理员租户**（T20：env 凭证→初始管理员）；普通租户解析失败返回 (None,None)，记「未配置」 | T20，待裁决 §8-F6 |
| P11 | `record_agent_run` | `src/core/agent_runs.py:10-58` | 加参 `tenant_id`；agent_runs 表 v120 加列 DEFAULT 1；扇出循环内每租户一行运行记录 | T20 |
| P12 | 快照读写（C2 泄漏点） | `context_store.py:20-91`（stock）、`:94-164`（news topic） | 全部函数加 `tenant_id` 参数并写入/过滤；v121 重建 UQ：`stock_context_snapshots` UQ 加 tenant_id（现 `models.py:348-355`）、`news_topic_snapshots` UQ 加 tenant_id（现 `models.py:378-381`） | C2/M5 |
| P13 | 快照 payload 组装 | `context_builder.py:487`（持仓约束）、`:539`（playbook 摘要）、`:556`（写入 payload）、`:564-572`（落库）、`:577-589`（news topic 落库） | 不改结构；因 P4/P5/P12 已租户化，payload 天然只含本租户持仓/方案——**MT-P5 prompt 内容级断言的验证点**（M8） | C2 |
| P14 | `_build_snapshot_memory` 回读 | `context_builder.py:365-400` → `context_store.py:67-91` | 回读链路随 P12 加 tenant 过滤（记忆不串租户） | C2 |
| P15 | `ContextBuilder._load_playbook_summary` | `context_builder.py:428-452`（解析行 :438） | 加参 `tenant_id`；`Stock.symbol==X`(:436-440) 加 tenant 过滤（12 解析点之一，T15） | C6/T15 |
| P16 | `morning_brief._load_playbook_summary` | `src/agents/morning_brief.py:49-77`（解析行 :64） | 同上，加 tenant 过滤（12 解析点之一） | C6/T15 |
| P17 | `daily_report._build_one_playbook_section` | `src/agents/daily_report.py:259-303`（解析行 :274）；`_load_trigger_status` :305-315 | 同上，加 tenant 过滤；告警规则/Hit 经 stock→tenant 派生 | C6/T15 |
| P18 | 事件门 `check_and_update` | `intraday_event_gate.py:251-340`；调用点 `intraday_monitor.py:1040-1048` | 加参 `tenant_id`（默认 1 保 611 基线）；内部分层见 §4 | T7/C1 |
| P19 | 通知去重 | `base.py:269-328`（scope 构造 :275）；`notify_dedupe.py:17-25,32-91` | scope 字符串编入 tenant，见 §3；函数 `check_and_mark_notify` 不加参，tenant 完全由 scope 承载 | C8/T21 |
| P20 | intraday 个股节流 | `intraday_monitor.py:943-1008` | `NotifyThrottle.stock_symbol` 字段值改为 `{tenant_id}:{symbol}` 前缀编码，见 §3.2 | C8 |
| P21 | 日志 tenant 归因 | `log_context.py:11-17`；`src/web/log_handler.py:74-87`；`models.py:236-256` | log_context 新增 `tenant_id` ContextVar；record factory(:96-106) 注入；DBLogHandler.emit(:74-87) 写列；LogEntry v120 加列 DEFAULT 1；**系统级日志 tenant_id=0** | T20/M12 |
| P22 | `TenantContext` contextvar 后台链设置点 | （MT-P0.5 新机制，web 层之外） | 扇出循环内 `with tenant_scope(tenant_id):` 同时设置 TenantContext + log tenant；此后 `do_orm_execute` 自动过滤对旁路自建 Session（P19/P12 的 SessionLocal）同样生效——这是「漏一环不静默用错数据」的机制兜底 | C7 |

### 1.3 手动触发链（与调度链共用改造）

| 入口 | 位置 | 改造 |
|---|---|---|
| `trigger_agent` | `server.py:1322-1407` | 加参 `tenant_id`（web 层自 JWT/TenantContext 传入）；内部 `load_watchlist_for_agent`(:1337)、`resolve_ai_model`(:1344)、`resolve_notify_channels`(:1345)、`build_context`(:1348) 全部下传 |
| `trigger_agent_for_stock` | `server.py:1410-1533` | 加参 `tenant_id`；`load_portfolio_for_stock`(:1442)、`resolve_ai_model`(:1444)、`resolve_notify_channels`(:1445) 下传；stock 入参需先经租户作用域解析（web 层职责） |
| `AgentScheduler.trigger_now` | `scheduler.py:174-176` | 签名加 `tenant_id: int \| None`：指定租户=单租户执行；None=全租户扇出（保留运维「全量立即跑」能力） |
| API 调用点 | `src/web/api/agents.py:369`（docs/17 §3 已登记） | 属 web 路由审计文档范围，**两份文档必须对齐 `trigger_agent(tenant_id, ...)` 签名**（§8-F5） |

---

## 2. 调度 job 命名规则与扇出结构（T17 / C8）

### 2.1 硬约束复述

T17 / C8 / R6：**单 job 遍历租户，严禁 per-tenant 注册 N 个 job**。per-tenant 注册会把调度器与上游行情接口打爆（`market_http.py` 无全局限流器，docs/20 C8、R7-15）。

### 2.2 现有 job 清单与改造判定

| job id（现状） | 注册位置 | 多租户改造判定 |
|---|---|---|
| `agent.name`（每 agent 一个，:55，`replace_existing=True` :57） | `scheduler.py:51-58` | **id 不变、数量不变**；job 体内部改扇出（§2.3）。绝不允许出现 `{agent}:{tenant}` 后缀 id |
| `price_alert_scan` | `price_alert_scheduler.py:49-58` | **不变**。引擎单遍扫全部启用规则（`price_alert_engine.py:515`），规则经 stock 派生 tenant，通知按行级 tenant 路由（§6.3）——天然单 job 合规 |
| `paper_trading_scan` / `_premarket` / `_summary` | `paper_trading_scheduler.py:74-105` | **不变**。引擎内改为遍历全部租户账户（T9），单 job 单遍 |
| `context_maintenance_evaluate` / `_cleanup` / `_refresh_opportunities_*` / `_bootstrap_evaluate` | `context_scheduler.py:238-281` | **不变、无扇出**。策略信号生成链为市场级全局（T19/M2），不引入 tenant 维度 |

结论：**全部现有 job id 原样保留，多租户不新增任何 job**。job 数量与租户数解耦——这是 T17 的可审计检查点（验收：`scheduler.get_jobs()` 数量不随租户数变化）。

### 2.3 Agent 扇出结构（`_run_agent` 改造设计，scheduler.py:64-172）

伪代码：

```python
async def _run_agent(self, agent_name: str):
    agent = self.agents.get(agent_name)          # 不变 :70-73
    tenants = list_active_tenants()              # ORDER BY id，确定性顺序
    for tenant in tenants:                       # 串行，不 asyncio.gather
        if not agent_enabled_for_tenant(agent_name, tenant.id):   # T4 override enabled
            continue
        trace_id = f"sch-{agent_name}-t{tenant.id}-{ts}"     # trace 含 tenant（现状 :76）
        try:
            with tenant_scope(tenant.id), log_context(..., tenant_id=tenant.id):
                context = self.context_builder(tenant.id, agent_name)   # P2/P3
                ...  # single/batch 分支逻辑不变（:88-161），逐租户执行
                record_agent_run(..., tenant_id=tenant.id)              # P11
        except Exception as e:
            # 单租户失败不阻断后续租户（现状 :162-172 的兜底逻辑下沉到租户粒度）
            record_agent_run(..., status="failed", tenant_id=tenant.id)
            continue
```

设计要点：

1. **串行不并行**：N≤5（T1），串行成本可忽略；并行会加剧 SQLite 写竞争（docs/17 §4 末条、风险 #10）。
2. **租户粒度容错**：现状整个 job 一个 try(:77-172)；改造后 try 下沉到租户粒度，单租户异常不吞掉其余租户（现状缺陷的顺带修复）。
3. **execution_mode=single 分支**（:89-134）：`context.watchlist` 已是本租户自选（P4），循环逻辑零改动。
4. **enabled 判定**：租户 override `enabled=False` → 扇出时跳过（T4）。**schedule 不可被租户 override**——见 §8-F2 待裁决项（T4 与 T17 的潜在冲突）。

### 2.4 错峰 jitter 保留清单（原样保留，禁止删除）

| 位置 | 参数 | 注释原意 |
|---|---|---|
| `price_alert_scheduler.py:53` | `jitter=20` | 避免与模拟盘 60s 同刻并发写 SQLite |
| `paper_trading_scheduler.py:78` | `jitter=20` | 同上 |
| `context_scheduler.py:242` | `jitter=120` | 避免与 60s 扫描同刻写库 |
| `context_scheduler.py:253` | `jitter=120` | 清理 job |
| `context_scheduler.py:266` | `jitter=120` | 机会刷新 job |

扇出循环内可选增加**租户间微错峰**（如 `await asyncio.sleep(tenant_index * 1.0)`，仅 intraday 等 single 高频 agent）：租户串行执行天然已错峰，此项为可选优化而非必须；N≤5 下建议不引入，保持简单。

---

## 3. 通知去重 / 节流改造（T21 / C8 / R6-MT-P3）

### 3.1 现状键结构（证据）

| 机制 | 位置 | 键 |
|---|---|---|
| 全局通知去重 | `base.py:271-281`（scope 构造 :275）、`notify_dedupe.py:17-25` | `dedupe_key = sha1(agent_name\|title\|content[:1200])`；`scope = f"__notify__:{dedupe_key}"`，存 `notify_throttle.stock_symbol` |
| intraday 个股节流 | `intraday_monitor.py:943-1008` | `(agent_name, stock_symbol=symbol)` 直查 NotifyThrottle |
| 表约束 | `models.py:306-312` | `UQ(agent_name, stock_symbol)` = `uq_agent_stock_throttle` |

互吞场景（docs/17 §1.2-1）：两租户自选股相同 → 内容 hash 相同 → 后发的被当重复吞掉。

### 3.2 目标设计：tenant 编进 scope 字符串，不重建 UQ（C8 原文）

| 机制 | 新 scope / 字段值 | 改造点 |
|---|---|---|
| 全局去重 | `scope = f"__notify__:{tenant_id}:{dedupe_key}"` | `base.py:275` 一处；tenant 取自 `context.tenant_id`（P1） |
| intraday 个股节流 | `stock_symbol` 字段值 = `f"{tenant_id}:{symbol}"` | `intraday_monitor.py:954,983` 两处查询/写入的键构造 |
| `notify_throttle` 表 | v120 加 `tenant_id NOT NULL DEFAULT 1`（T18，仅归因/审计用）；**UQ 保持 `(agent_name, stock_symbol)` 不变，不进 v121 重建清单** | 唯一性由前缀字符串保证 |
| 去重 TTL / 静默时段 | `notify_dedupe_ttl_overrides`、`notify_quiet_hours` 等随 T20 变**租户级**配置（P9），不同租户可有不同 TTL | `base.py:199-229` 的 `policy.dedupe_ttl_minutes` 链路不变，数据源换租户级 |

为什么不重建 UQ（执行 C8 而非 M5，见 §8-F1）：SQLite 改 UQ 只能重建表（docs/17 §2.2-3），而字符串前缀方案零 schema 风险、旧数据可原地回填、且 scope 语义自解释。

### 3.3 旧行处理（v120 幂等回填，禁改 v101-119）

旧行全部属于默认租户 1。v120 内执行幂等 UPDATE（可重入，已带前缀的行不匹配 WHERE）：

```sql
-- 全局去重行：'__notify__:{hash}' → '__notify__:1:{hash}'（'__notify__:' 为 11 字符）
UPDATE notify_throttle
SET stock_symbol = '__notify__:1:' || substr(stock_symbol, 12)
WHERE stock_symbol LIKE '__notify__:%'
  AND stock_symbol NOT LIKE '__notify__:1:%';

-- 个股节流行：'{symbol}' → '1:{symbol}'（symbol 为纯数字/字母，无冒号）
UPDATE notify_throttle
SET stock_symbol = '1:' || stock_symbol
WHERE stock_symbol NOT LIKE '__notify__:%'
  AND stock_symbol NOT LIKE '%:%';
```

- 不回填也可正确运行（旧行自然过期，TTL 上限 12h，`base.py:205-221`），代价是默认租户可能收到一次重复推送；回填 SQL 成本极低，**采纳回填**。
- fail-open 语义保留：`notify_dedupe.py:86-89` 异常时放行（宁可重发不吞）——多租户下此语义**更安全**，不改。

---

## 4. 事件门状态分层详细设计（T7 / C1 / R3）

### 4.1 现状文件结构（`data/state/intraday_monitor_state.json`，`intraday_event_gate.py:26-27`）

```json
{ "<symbol>": {
    "last_price": 112.57,          // 穿越判定基线 :307,:332
    "tech_sig": {...},             // 技术态变更检测 :294,:338
    "pb_fired": {"上穿:防线@100": "iso-ts"},  // 冷却 :309-331，键 :322
    "last_seen_at": "...", "change_pct": ..., "volume_ratio": ...  // :335-337
} }
```

问题（C1）：多租户交替写 → last_price 基线错乱；`pb_fired` 冷却键 `方向:位名@价位`(:322) 不含 tenant → 互吞（1800s，`PLAYBOOK_CROSS_COOLDOWN_SEC` :96）；`_load_playbook_levels`(:206-227) 按 symbol 无过滤解析 Stock(:216) → 取到别租户档案价位。

### 4.2 键控分层表（R3 落地）

| 状态键 | 分层判定 | 新位置 | 理由 |
|---|---|---|---|
| `last_price` | **市场级单份** | `market.{symbol}.last_price` | 行情观测，租户复制反而错误（C1 原文） |
| `tech_sig` | **市场级单份** | `market.{symbol}.tech_sig` | 由市场 K 线推导，全租户同值 |
| `last_seen_at` / `change_pct` / `volume_ratio` | **市场级单份** | `market.{symbol}.*` | 观测记录 |
| `pb_fired` 冷却键 | **(tenant, symbol) 键控** | `tenants.{tenant_id}.{symbol}.pb_fired` | 冷却键 `方向:位名@价位` 语义不变，但归属租户命名空间，互吞消除 |
| playbook 价位解析（`_load_playbook_levels`） | **(tenant, symbol) 键控** | 不落盘；调用时 `tenant_id` 过滤 Stock(:216) → 本租户激活 playbook | T15 12 解析点之一 |

### 4.3 v2 文件结构

```json
{
  "version": 2,
  "market": {
    "<symbol>": { "last_price": ..., "tech_sig": {...},
                  "last_seen_at": "...", "change_pct": ..., "volume_ratio": ... }
  },
  "tenants": {
    "<tenant_id>": {
      "<symbol>": { "pb_fired": { "上穿:防线@100": "iso-ts" } }
    }
  }
}
```

- `check_and_update` 加参 `tenant_id: int = 1`（T18 默认值保 611 基线零修改）；调用点 `intraday_monitor.py:1040` 传 `context.tenant_id`。
- 读写仍走 `read_json` / `write_json_atomic`（`json_store.py:13-28`），tmp+replace 原子性保留。
- 穿越判定逻辑 :307-331 不变，仅 `prev_price` 改读 `market` 节、`fired` 改读写 `tenants.{t}` 节；「首次观测无价态基线不判定」(:314-315) 语义保留。
- 租户/股票删除时的惰性清理：stale tenant/symbol 节不影响正确性，随文件自然增长有界（N≤5 × 自选股数），**不做主动 GC**（设计简化，登记为已知项）。

### 4.4 旧文件一次性迁移脚本（v120 配套，文件迁移不走 DB 迁移链——docs/17 §2.3 教训）

脚本：`scripts/migrate_event_gate_state_v2.py`（v120 执行器调用，或启动时惰性自愈，二选一——**建议 v120 显式调用**，可审计）：

1. 读旧文件；若已含 `"version": 2` → 跳过（**幂等可重入**，对齐 R4）。
2. 备份原文件为 `intraday_monitor_state.json.v1.bak`（保留现场，对齐 R5 备份意识）。
3. 转换：旧 `rec` 的行情字段（last_price/tech_sig/last_seen_at/change_pct/volume_ratio）→ `market.{symbol}`；旧 `pb_fired` → `tenants."1".{symbol}.pb_fired`（T18 默认租户=1）。
4. `write_json_atomic` 写回。
5. 防御：新代码读取时若无 `version` 字段，按 v1 结构容错解析并触发惰性迁移（fail-soft，对齐该模块 :225-227 的一贯容错风格）。

### 4.5 已知边界语义（明示，不视为缺陷）

- 单 job 串行扇出下，后执行租户的穿越判定使用前序租户刚写入的共享 `last_price` 基线——行情是连续观测流，语义正确（T7「行情观测态单份」的直接推论）；租户 B 首次运行时若基线已存在且价格正穿越其 playbook 价位，会立即报告一次——事件真实发生，属合理行为。
- 租户 A 修改 playbook 价位后，其旧冷却键（含旧价位）自然过期（1800s），新价位键独立——无需失效广播。

---

## 5. 种子逻辑：系统模板 vs 租户行判定规则（T4 / T19）

### 5.1 判定规则表

| 种子函数 | 位置 | 现状行为 | 多租户判定规则 |
|---|---|---|---|
| `seed_agents` | `server.py:302-352`，spec 源 `src/core/agent_catalog.py:44-60,60+` | 按 `name` 全库 upsert（`agent_configs.name` 全局 unique，`models.py:197`）；不删孤儿（docs/20 E7 已勘误） | **`agent_configs` 整体改判为「系统模板表」**：一行是模板 ⟺ `name ∈ AGENT_SEED_SPECS`。seed 只 upsert 模板行，**永不触碰**新增的 `agent_config_tenant_overrides` 表（T4：override 表，非整行复制）。租户个性化 100% 落在 override 表，模板表无租户行→不存在「误删租户自定义」问题 |
| `seed_data_sources` | `server.py:655-698` | 按 (name, provider) 只增不删 upsert | `data_sources` 维持**实例级共享**（T3 凭证管理员托管）；无租户行概念，逻辑不变 |
| `reconcile_data_sources` | `server.py:709-737` | 删孤儿：provider ∉ legal(type)（:723-729）；保留用户自定义行 | 不变。推论：用户自定义数据源自动成为管理员托管共享资源（T3 一致）；** orphan 判定集合不变，租户不参与** |
| `seed_sample_stocks` | `server.py:279-299` | 全表 count==0 时插 5 只示例 | 仅首次启动（空库）触发，插入行 tenant_id=1（T18）；**新租户不补示例股，自选股从零开始**（与 T21「新租户默认零渠道」同哲学） |
| `seed_strategies` | `server.py:740-743` | 策略目录 upsert | 策略目录市场级全局（T19/M2），不变 |

### 5.2 模板行 vs 租户行冲突防护

- T4 已选 override 表方案，从结构上消除了「seed 对账误删租户行」的风险（docs/17 §4 种子逻辑条目的设计应答）。
- override 表语义=**整体替换**（docs/20 §4 架构师条目）：一个 (tenant_id, agent_name) 一行，字段含 `enabled / ai_model_id / notify_channel_ids / config`；**不含 schedule**（§8-F2）。
- 生命周期防护：`agent_configs` 模板行被 seed 标记 `lifecycle_status=deprecated`（:332）时，override 行仍有效——解析链（§6）在 template 层读取 `enabled/schedule` 时以模板行现状为准。

---

## 6. resolve_ai_model / resolve_notify_channels 租户作用域解析（T4 / T13 / T21）

### 6.1 `resolve_ai_model(tenant_id, agent_name, stock_agent_id)`（现状 `server.py:1022-1069`）

新解析顺序（T4 优先级链 `stock_agent > tenant_override > template > 系统默认` + T13 共享配额判定）：

| 步骤 | 现状行 | 多租户改造 |
|---|---|---|
| 1. stock_agent 覆盖 | :1031-1035 | `stock_agents.ai_model_id`（stock_agents 经 stock 派生 tenant，P4 已过滤）；**校验所引 model 对本租户可见**（见下「可见集」），不可见则视为未配置并告警 |
| 2. tenant_override | （新表） | `agent_config_tenant_overrides.ai_model_id`，整体替换语义 |
| 3. template | :1037-1041 | `agent_configs.ai_model_id`（模板表全局读，无 tenant 过滤——模板是共享资源） |
| 4. 租户默认 | :1043-1047 | `AIModel.is_default==True` 限定在**租户可见集**内（现行为全局唯一 is_default，多租户下必须按可见集过滤；ai_models 经 service 派生可见性） |
| 5. 兜底第一个 | :1049-1053 | 可见集内第一个 |
| 6. env 回退 | `_build_ai_client` :1173-1180 | **仅管理员租户**（T20 env 凭证→初始管理员）；普通租户可见集为空 → 返回 (None,None) → 该租户本次运行记「未配置 AI」并跳过 LLM 调用（fail-soft） |

**租户可见集（T13 落地）**：
- 租户「与管理员共享配额」（邀请时选定，T13 用户确认口径）→ 可见 = 管理员托管 ai_services 下的全部模型 + 本租户自建服务的模型；密钥仅在服务端装配 AIClient 时使用（:1162-1172），API 序列化层对托管行脱敏（T3）。
- 租户「不共享配额」→ 可见 = **仅本租户私有 ai_services**（T3 例外路径，密钥仅本租户可见）；无自建服务则可见集为空。
- cost_tracker 按 tenant 归因计量（T13），计费/配额判定不在本函数内，由 AIClient 调用链另行记账。

### 6.2 `resolve_notify_channels(tenant_id, agent_name, stock_agent_id)`（现状 `server.py:1072-1116`）

| 步骤 | 现状行 | 多租户改造 |
|---|---|---|
| 1. stock_agent 覆盖 | :1080-1084 | 同 6.1 步骤 1；渠道 id 逐个校验可见性 |
| 2. tenant_override | （新表） | `notify_channel_ids` 整体替换 |
| 3. template | :1086-1090 | 模板 notify_channel_ids 指向管理员渠道→**即 T21「引用管理员托管渠道」的默认路径**，允许；普通租户模板引用视为托管引用 |
| 4. id 列表查询 | :1093-1101 | 过滤条件加可见性：`tenant_id==本租户` **OR** `托管共享`（notify_channels 新增 `is_shared/managed` 语义，管理员行可被引用）；`enabled==True` 不变 |
| 5. is_default 回退 | :1102-1110 | is_default 限定可见集（T21：行租户私有 + 可引用托管） |
| 6. 零渠道 | — | 新租户默认零渠道 → 返回空列表 → `notify_result.skipped` 路径（现有 `base.py:302-313` 跳过逻辑直接复用，不视为错误） |

**密钥不可见边界**：托管渠道的 `config`（含 bot_token 明文，`models.py:59`）仅在服务端 `_build_notifier`(:1157-1158) 装配使用；API 层序列化托管渠道时 config 打码（web 层审计文档职责，本文登记依赖）。

### 6.3 旁路引擎的渠道解析点（同一套规则，不得各写一套）

| 点位 | 位置 | 改造 |
|---|---|---|
| 价格提醒渠道解析 | `price_alert_engine.py:398-410` | 规则→stock→tenant 派生（T18）；`rule.notify_channel_ids`(:399-405) 与 is_default 回退(:406-410) 均套用 §6.2 可见性过滤 |
| 价格提醒 NotifyPolicy | `price_alert_engine.py:412-450`（AppSettings 读 :422-424） | `notify_*` 改读规则所属租户的配置（T20） |
| 模拟盘通知渠道 | `paper_trading_notifier.py:67-100` | `pt_notify_channel_ids`(:75-89) 按账户 tenant 解析；is_default 回退(:85-89) 同上 |
| 模拟盘 pt_notify_* 配置 | `paper_trading_notifier.py:34-48` | 归租户级（T20 只明写 `notify_*`，`pt_notify_*` 归属见 §8-F3 待裁决） |
| 模拟盘账户单例假设 | `paper_trading_engine.py:240-241`（`.first()`）；notifier :318/:358 | T9 每租户一账户：`_get_or_create_account(db, tenant_id)`；`_scan_sync`(:619-651) 改遍历全部启用账户；notifier 的 `.first()` 改按 tenant（前置补 account_id+回填属 schema 文档范围） |

### 6.4 app_settings 三分对本范围读取点的影响（T20 / M13）

| 读取点 | 键 | 三分后归属 | 改造 |
|---|---|---|---|
| `server.py:1002-1009` `_get_proxy` | `http_proxy` | 实例级 | 不变 |
| `server.py:1123-1128` `_build_notifier` | `notify_quiet_hours/retry_attempts/retry_backoff_seconds/dedupe_ttl_overrides` | 租户级 | 加 tenant 维度读取（P9） |
| `price_alert_engine.py:422-433` | 同上 notify_* | 租户级 | 按规则 tenant 读取 |
| `paper_trading_notifier.py:34-48` | `pt_notify_*` | 租户级（建议） | 按账户 tenant 读取 |

---

## 7. 审计点汇总 Checklist（MT-P3 施工逐项回销）

| # | 审计点 | 位置 | 验收方式 |
|---|---|---|---|
| A1 | 扇出单 job，无 per-tenant job | `scheduler.py:51-58` | `get_jobs()` 数量不随租户变 |
| A2 | 租户粒度容错+串行 | `scheduler.py:64-172` | 租户 A 抛异常，B 仍执行 |
| A3 | context_builder/build_context 新签名 | `scheduler.py:26`;`server.py:1183` | 全调用点编译期可查 |
| A4 | 4 个 load/resolve 显式 tenant 传参 | `server.py:746,866,943,1022,1072` | 双租户对照测试 |
| A5 | TenantContext 覆盖旁路 Session | `notify_dedupe.py:54`;`context_store.py`;`log_handler.py` | C7 机制点单测 |
| A6 | 快照私有（C2） | `context_builder.py:487/539/556`;`context_store.py:20-164` | **prompt 内容级断言**（M8）：B 的 prompt 不含 A 持仓数字 |
| A7 | 去重 scope 含 tenant | `base.py:275`;`intraday_monitor.py:954,983` | 双租户同 watchlist 互吞回归（R6-MT-P3 门禁） |
| A8 | 事件门分层 | `intraday_event_gate.py:206-227,309-331` | 双租户同 symbol 不同 playbook 价位，冷却不互吞 |
| A9 | 事件门 v1→v2 迁移幂等 | §4.4 脚本 | 重跑两次文件一致；.bak 存在 |
| A10 | 种子不触租户行 | `server.py:302-352,655-737` | seed 后 override 表 diff 为空 |
| A11 | 解析链优先级 | §6.1/6.2 | stock_agent>override>template>默认 逐层单测 |
| A12 | 零渠道新租户不报错 | §6.2 步骤 6 | skipped 路径单测 |
| A13 | 日志 tenant 归因 | `log_context.py`;`log_handler.py:74-87` | 扇出后 log_entries 可按 tenant 过滤 |
| A14 | record_agent_run 含 tenant | `agent_runs.py:10-58` | 每租户独立运行记录 |

---

## 8. 与 docs/17 / docs/20 的冲突与待裁决项

| # | 事项 | 冲突双方 | 本文立场 | 建议 |
|---|---|---|---|---|
| F1 | notify_throttle 是否重建 UQ | M5（docs/20:54，列入 5-7 处重建清单）vs C8/R6（docs/20:44、docs/17 R6-MT-P3：不重建，tenant 编进 scope 字符串） | **执行 C8/R6**（修订节优先级高于评审正文，docs/17:150；且 R6 晚于 M5 形成） | 上报 Orchestrator：请确认 M5 重建清单剔除 notify_throttle，避免 MT-P2 施工时两份依据打架 |
| F2 | tenant override 是否含 `schedule` 字段 | T4（override 表+整体替换，未限定字段）vs T17（单 job 硬约束） | 若 override 可改 schedule，单 job 无法按不同 cron 服务不同租户，T17 被破坏 | **建议裁决：override 表字段仅限 `enabled/ai_model_id/notify_channel_ids/config`，schedule 恒取模板**；enabled=False 的租户在扇出时跳过 |
| F3 | `pt_notify_*` 配置键归属 | T20 只明写 `notify_*/avatar=租户级`，未含模拟盘 `pt_notify_*`（`paper_trading_notifier.py:26-31`） | 按 T9「模拟盘每租户」语义，应归租户级 | 上报确认 |
| F4 | 共享 last_price 基线的先后写语义 | T7「行情观测态单份」vs 严格「每租户独立首次观测」 | 单份共享语义正确（§4.5），但租户 B 首次运行可能立即报穿越 | 无需裁决，已在 §4.5 明示；如产品不接受再议 |
| F5 | `trigger_agent` 等手动入口的 tenant 传参 | 本文（运行时链）vs web 路由审计文档（`agents.py:369` 调用点） | 签名必须两份文档对齐：`trigger_agent(tenant_id, agent_name)` | 上报 Orchestrator 协调两份 MT-P0 文档的签名一致性 |
| F6 | env 凭证回退范围 | `_build_ai_client` :1173-1180 现状无条件回退 env | T20「env 凭证→初始管理员」implies 仅管理员租户可回退；普通租户可见集为空时**不**回退 env（否则等于全员共享管理员 key，违背 T13） | 上报确认该推论 |

---

*本文档为 MT-P0 设计稿，未修改任何代码；施工以 MT-P2/MT-P3 阶段门禁为准。*
