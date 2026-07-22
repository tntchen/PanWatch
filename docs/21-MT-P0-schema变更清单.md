# MT-P0 · Schema 变更清单（数据层设计稿）

> 日期：2026-07-22 ｜ 阶段：MT-P0（设计稿，不含任何代码改动）
> 对应决策点：T2/T3/T4/T6/T8/T9/T12/T13/T14/T15/T18/T19/T20/T21（主），T1/T5/T7/T17（涉）
> 上游依据：`docs/17-多租户改造调研与计划.md` v1.1（R1–R8 修订节优先）、`docs/20-多租户方案评审-架构研发测试运维.md`（C1–C8、M1–M15）
> 审计口径：本文所有行号均于 2026-07-22 对照真实代码/真实库核对；生产库 `data/panwatch.db` 实测 44 张业务表 + `schema_migrations`，最新已应用迁移 v119。

---

## 0. 决策映射与评审呼应总表

| 决策/评审项 | 本文落点 |
|---|---|
| T2 共享库+tenant_id 列 | §1 逐表分类、§3 v120 加列 |
| T18 tenant_id NOT NULL DEFAULT 1 | §3 加列 DDL 统一模板 |
| T6/R4/M3 迁移拆 v120/121/122，幂等可重入 | §3/§4/§5 三步设计 |
| M5/R1-4 仅 5–7 处 UQ 重建 | §4 重建清单（含与 R6 的冲突裁决，见 §4.0） |
| T15 stocks 每租户复制 + (tenant,symbol,market) UQ | §1 stocks 行、§4.6 |
| T9 模拟盘每租户（前置补 account_id） | §3.3、§4.7 |
| T19 信号生成链市场级全局 | §1 B 组 |
| R2 stock_context_snapshots/news_topic_snapshots 改判私有 | §1 A 组第 13/14 行 |
| T8 新闻共享去重 + tenant_news_pushed | §7.3 新表 DDL |
| T4 agent override 表 | §7.4 新表 DDL |
| T20 app_settings 三分 / log tenant 归因 / 回退 flag | §7.1/§7.2/§7.1 |
| T12 邀请制 / T13 配额共享布尔位 / T5 两级 role | §2 tenants/users DDL |
| T21 notify_channels 私有+可引用托管 | §1 notify_channels 行、§3.4 |
| T7/R3 事件门状态分层（DB 外） | §6.1 |
| R5/C3 备份修复前置 | §8 |
| C5/M7 迁移验收锚点 | §5.3 对账 SQL |
| T14/M9 备份脱敏 | §6.4、§8 |

---

## 1. 逐表分类终表（45 表）

分类口径：
- **A 强私有**：v120 加 `tenant_id INTEGER NOT NULL DEFAULT 1`；查询层按租户过滤（T16 机制）。
- **B 市场级全局共享**：不加 tenant 列；任何租户可读，写入由市场级调度链负责。
- **C 实例级**：不加 tenant 列；仅管理员/系统进程可写。
- **D 新增表**：v120 新建。

证据列给出模型定义行号（`src/web/models.py`，简写 m.py）或迁移定义行号（`src/web/migrations.py`，简写 mig.py）。

| # | 表 | 分类 | 依据与说明 | 证据 |
|---|---|---|---|---|
| 1 | ai_services | **A** | T3 默认管理员托管（行落 tenant 1）；T13 例外：未共享配额租户自建，行归该租户，api_key 明文仅本租户可见 | m.py:20-33；docs/17:205,215 |
| 2 | ai_models | A | FK 链派生（service_id→ai_services），is_default 排他 update 需加 tenant 条件（§3 风险 9） | m.py:36-50 |
| 3 | notify_channels | A | T21 租户私有（config 含 bot_token 明文 m.py:59）+ `is_shared` 列支持管理员托管渠道被引用（§3.4） | m.py:53-62；docs/17:223 |
| 4 | accounts | A | 交易账户，含资金 | m.py:65-79 |
| 5 | stocks | A | T15 每租户复制；v121 新增 `(tenant_id,symbol,market)` UQ；12 处解析点改造属代码层（查询审计清单），本文只落 schema | m.py:82-102；docs/20:34-36 |
| 6 | positions | A | FK 链派生（account_id/stock_id 均已租户化）；UQ(account_id,stock_id) 天然租户内唯一，**不重建** | m.py:105-137 |
| 7 | position_trades | A | FK 链派生（position_id） | m.py:140-167 |
| 8 | stock_agents | A | FK 链派生（stock_id）；UQ(stock_id,agent_name) 不重建 | m.py:170-190 |
| 9 | agent_configs | **C** | T4：seed 模板全局（name UNIQUE 保留），租户差异走新表 agent_config_overrides（#46） | m.py:193-215；docs/17:206 |
| 10 | agent_runs | A | 每次执行归属租户（调度单 job 遍历租户时按租户写行，T17） | m.py:218-233 |
| 11 | log_entries | A | T20 日志 tenant 归因；高写入量，加 tenant 索引 | m.py:236-256；docs/17:222 |
| 12 | app_settings | **C** | T20 三分后只留实例级键（jwt_secret/http_proxy/panwatch_base_url/single_tenant_mode），映射见 §7.1 | m.py:259-265 |
| 13 | data_sources | **C** | T3 数据源 token 管理员托管；全实例共享一份 | m.py:268-284；docs/17:205 |
| 14 | news_cache | **B** | T8 共享去重；UQ(source,external_id) 不动 | m.py:287-303；docs/17:210 |
| 15 | notify_throttle | A（仅加列，**不重建 UQ**） | R6-MT-P3 裁决：tenant 编进 scope 字符串 `__notify__:{tenant}:{hash}`，UQ(agent_name,stock_symbol) 保留即天然隔离；tenant_id 列仅用于归因/清理。与 M5 的冲突见 §4.0 | m.py:306-318；docs/17:190 |
| 16 | analysis_history | A | M5：UQ(agent_name,stock_symbol,analysis_date) 跨租户互覆，**v121 重建** | m.py:321-340；docs/20:54 |
| 17 | stock_context_snapshots | A | **R2 改判私有**：payload 含持仓约束+playbook 摘要且回读注入 prompt，共享=泄漏（docs/20 C2）；**v121 重建** | m.py:343-370；docs/17:164；docs/20:17-20 |
| 18 | news_topic_snapshots | A | **R2 改判私有**（输入是租户 watchlist 新闻）；UQ(snapshot_date,window_days) 两租户同天必撞，**v121 重建**（M5 "5–7 处"弹性项，本文取第 6 处） | m.py:373-394；docs/17:164 |
| 19 | agent_context_runs | A | 上下文摘要含租户持仓/自选股，回读注入 prompt（同 C2 理据） | m.py:397-412 |
| 20 | agent_prediction_outcomes | A | 记录的是**租户级 Agent**（intraday_monitor 等）建议的后验，非 T19 市场级信号链；注意与 #28 strategy_outcomes 区分 | m.py:415-444 |
| 21 | stock_suggestions | A | 各租户 Agent 产出，prompt_context/ai_response 含租户上下文 | m.py:447-493 |
| 22 | entry_candidates | A + 市场级哨兵 | T19 按 candidate_source 拆语义：`watchlist` 源行租户私有；`market_scan`/`mixed` 源行市场级，统一写 **tenant_id=0（市场级哨兵，非真实租户）**。UQ 重建为含 tenant_id（§4.5）。do_orm_execute 对该表需 `tenant_id IN (:t, 0)` 特例——**跨文档依赖，见 §9 上报项 U-1** | m.py:496-539；docs/17:221 |
| 23 | market_scan_snapshots | **B** | T19 市场池候选快照，全局一份 | m.py:542-567 |
| 24 | entry_candidate_feedback | A | T19 租户交互面 | m.py:570-588 |
| 25 | entry_candidate_outcomes | A | FK 链派生（candidate_id→entry_candidates，含哨兵行）；UQ(candidate_id,horizon_days) 不重建 | m.py:591-622 |
| 26 | strategy_catalog | **B** | 策略目录全局（seed 于 mig.py:793-884） | m.py:625-645 |
| 27 | strategy_signal_runs | A + 市场级哨兵 | **docs/26-J2 裁决改判**：信号行拷贝候选行的 signal/reason/evidence（strategy_engine.py:1364-1366），watchlist 源行含租户 AI 文本；TA 信号去重键跨租户互覆（paper_trading_bridge.py:60-72）。v120 加 tenant_id——market_scan/mixed 源=0 哨兵、watchlist 源=候选 tenant | m.py:648-704 |
| 28 | strategy_outcomes | A（父派生） | signal_run_id→strategy_signal_runs，tenant_id 由父派生（T18 不变量），v120 加列 | m.py:707-741 |
| 29 | strategy_weights | **B** | T19/M2 全局自动标定（v1.0 §2.1 的"混合语义"拍板为全局） | m.py:744-767 |
| 30 | strategy_weight_history | B | 同上（审计历史） | m.py:770-789 |
| 31 | factor_weights | B | 同上 | m.py:792-815 |
| 32 | factor_weight_history | B | 同上 | m.py:818-837 |
| 33 | market_regime_snapshots | B | 红利清单保留项 | m.py:840-867；docs/17:165 |
| 34 | strategy_factor_snapshots | B | T19 factor_snapshots 市场级 | m.py:870-898 |
| 35 | portfolio_risk_snapshots | **B**（docs/26-J1 裁决改判） | Orchestrator 核码实证：内容=信号池聚合（total/active/held_signals 均指 StrategySignalRun 行，strategy_engine.py:1010-1059,2048-2054），**非用户持仓组合**；docs/17:46 的"语义本身就错"条目及本文 v1.0 判断有误。UQ(snapshot_date,market) 保持正确，**免于 v121 重建** | m.py:901-927 |
| 36 | suggestion_feedback | A | 租户交互面（FK→stock_suggestions） | m.py:930-943 |
| 37 | price_alert_rules | A | 告警规则含租户 notify_channel_ids | m.py:946-977 |
| 38 | price_alert_hits | A | FK 链派生（rule_id）；UQ(rule_id,trigger_bucket) 不重建 | m.py:980-1008 |
| 39 | stock_playbooks | A | 方案档案含价位/持仓策略，注入 prompt 与事件门（C1） | m.py:1011-1038 |
| 40 | paper_trading_account | A | T9 每租户一账户；注释明写"单例"（m.py:1042），v120 加 tenant_id + 唯一索引 `uq_paper_account_tenant(tenant_id)`（CREATE UNIQUE INDEX 即可，无需重建表） | m.py:1041-1059 |
| 41 | paper_trading_positions | A | T9 前置：v120 补可空 `account_id`+回填，v121 重建为 NOT NULL+FK（§3.3/§4.7） | m.py:1062-1089 |
| 42 | paper_trading_trades | A | 同上 | m.py:1092-1117 |
| 43 | chat_conversations | A | 会话含租户上下文（initial_context） | m.py:1120-1136 |
| 44 | chat_messages | A | FK 链派生（conversation_id；注意 m.py:1148 该列**无 FK 声明**，仅逻辑关联，v122 不变量校验按逻辑父处理） | m.py:1139-1151 |
| 45 | schema_migrations | **C** | 迁移账本，实例级 | mig.py:85-99 |
| 46 | **tenants** | **D 新表** | §2.1 | — |
| 47 | **users** | **D 新表** | §2.2 | — |
| 48 | **tenant_settings** | **D 新表** | T20 租户级 KV（notify_*/ui_avatar），§7.1 | — |
| 49 | **tenant_news_pushed** | **D 新表** | T8，§7.3 | — |
| 50 | **agent_config_overrides** | **D 新表** | T4，§7.4 | — |

统计：A 强私有 **30 张**（v120 加列）；B 市场级共享 **11 张**；C 实例级 **4 张**；D 新增 **5 张**。与 docs/17:43 "约 30 张" 的估算一致。

---

## 2. tenants / users 完整 DDL（v120 自包含建表，不依赖 create_all——M3）

> 建表用 `CREATE TABLE IF NOT EXISTS`，幂等；默认租户显式 `id=1`（T18）。`CREATE TABLE` 内允许直接声明 FK + NOT NULL（SQLite 的 ADD COLUMN 限制不适用于建表）。

### 2.1 tenants（T12 邀请制、T1 ≤5 人容量、T20 回退语义的挂载点）

```sql
CREATE TABLE IF NOT EXISTS tenants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,                          -- 显示名，默认租户为 '默认租户'
  status TEXT NOT NULL DEFAULT 'active',       -- active / disabled（禁用即全租户只读拒绝）
  max_users INTEGER NOT NULL DEFAULT 5,        -- T1：N≤5 硬上限写进 schema 层默认值
  invite_code TEXT DEFAULT '',                 -- T12：管理员生成/旋转的邀请码，空=未开放
  invite_expires_at DATETIME,                  -- 邀请码过期时间，NULL=不过期
  registration_enabled INTEGER NOT NULL DEFAULT 0,  -- T12/M1：MT-P1 上线即 0（关注册），P1→P2 间保持 0
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 幂等回填默认租户（T18：默认租户=1）
INSERT INTO tenants (id, name, status, max_users, registration_enabled)
SELECT 1, '默认租户', 'active', 5, 0
WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE id = 1);
```

### 2.2 users（T5 两级 role、T13 配额共享布尔位、T20 env 凭证→初始管理员、M10 旧哈希透明重哈希）

```sql
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  username TEXT NOT NULL UNIQUE,               -- 全局唯一（登录不选租户，用户名定租户）
  password_hash TEXT NOT NULL,
  password_algo TEXT NOT NULL DEFAULT 'bcrypt',-- 'bcrypt' / 'sha256_legacy'（M10：legacy 首次登录透明重哈希为 bcrypt）
  role TEXT NOT NULL DEFAULT 'user',           -- 'admin' / 'user'（T5 两级，运行时实时查库，不进 JWT）
  quota_shared_with_admin INTEGER NOT NULL DEFAULT 0,  -- T13：1=与管理员共享 AI 配额；0=不分配配额须自建模型
  is_active INTEGER NOT NULL DEFAULT 1,
  invited_by INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- T12 邀请人（管理员）
  last_login_at DATETIME,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_users_tenant ON users(tenant_id);
```

初始管理员回填（v120 内，幂等；T20/M10：env 凭证优先，其次旧 app_settings 凭证）：

```sql
-- 伪代码（runner 内 Python 执行，因需读 env 与 app_settings 两处来源）：
-- 1. username = env AUTH_USERNAME 或 app_settings['auth_username']（证据：src/web/api/auth.py:29）
-- 2. password_hash/algo：
--    - env AUTH_PASSWORD 存在 → bcrypt(env 值)，algo='bcrypt'
--    - 否则 app_settings['auth_password_hash']（auth.py:30）→ 原样搬入，algo='sha256_legacy'
-- 3. INSERT INTO users (tenant_id, username, password_hash, password_algo, role, quota_shared_with_admin)
--    SELECT 1, :u, :h, :a, 'admin', 1 WHERE NOT EXISTS (SELECT 1 FROM users WHERE tenant_id=1 AND role='admin');
-- 4. 搬迁成功后 DELETE FROM app_settings WHERE key IN ('auth_username','auth_password_hash')（§7.1）
```

设计说明：
- `role` 不放 JWT、实时查库（T5，docs/17:207），升降级即时生效，无需踢 token。
- `quota_shared_with_admin` 只对 AI 配额计量生效（cost_tracker 按 tenant 归因，§7.2）；值为 0 时该用户的 ai_services 行走 T3 例外（租户私有）。
- 用户名全局 UNIQUE 而非 (tenant_id,username) 复合：登录流程需要先定位租户，且 N≤5 场景无用户名稀缺问题。

---

## 3. v120：建表 + 加列 + 回填（幂等可重入）

### 3.1 加列统一模板（30 张 A 类表）

SQLite 限制（docs/20 M3，docs/17:178）：`ADD COLUMN` 不能同时 `NOT NULL DEFAULT` + `REFERENCES`。因此 **v120 一律加无 FK 的裸列**，FK 只在 §4 的 `__new` 重建表内声明；未重建的表 tenant_id 永远无 DB 级 FK，一致性由 v122 不变量校验 + T16 机制保证：

```sql
ALTER TABLE <table> ADD COLUMN tenant_id INTEGER NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS ix_<table>_tenant ON <table>(tenant_id);
```

幂等实现：复用 `mig.py:51-59 _has_column` / `mig.py:62-67 _add_column_if_missing` / `mig.py:69-82 _create_index_if_missing` 现有 helper 模式，已存在即跳过，断点重跑安全。

适用表清单（32 张）：ai_services, ai_models, notify_channels, accounts, stocks, positions, position_trades, stock_agents, agent_runs, log_entries, notify_throttle, analysis_history, stock_context_snapshots, news_topic_snapshots, agent_context_runs, agent_prediction_outcomes, stock_suggestions, entry_candidates, entry_candidate_feedback, entry_candidate_outcomes, suggestion_feedback, price_alert_rules, price_alert_hits, stock_playbooks, paper_trading_account, paper_trading_positions, paper_trading_trades, chat_conversations, chat_messages, strategy_signal_runs（含 tenant_id=0 市场级哨兵行）, strategy_outcomes（父派生）。（docs/26-J1/J2：portfolio_risk_snapshots 移出、strategy_signal_runs/strategy_outcomes 移入）

### 3.2 回填策略与"免逐行回填"判定规则

**判定规则**：v120 执行时刻全库为单用户系统，一切存量行逻辑上均属默认租户 1；`DEFAULT 1` 在加列瞬间即完成全部存量行回填，**无需任何逐行 UPDATE**。逐行 UPDATE 仅当某表存在需要非 1 的存量行时才需要——本期不存在此类表。

逐表核验（三类例外，均已在模板外处理）：

| 表 | 例外处理 | 依据 |
|---|---|---|
| paper_trading_positions / paper_trading_trades | 额外补 `account_id`（见 §3.3），**唯一需要逐行 UPDATE 的回填** | T9 前置，docs/17:211 |
| entry_candidates | 存量行全部 `tenant_id=1`（DEFAULT 即可）；`tenant_id=0` 哨兵只由改造后的运行时代码写入，v120 不回填 | T19 |
| tenants / users | §2 的 INSERT...WHERE NOT EXISTS 幂等回填 | T18/T20 |

### 3.3 paper_trading 补 account_id（T9 前置）

v120（裸列 + 回填，幂等）：

```sql
ALTER TABLE paper_trading_positions ADD COLUMN account_id INTEGER;  -- 可空、无 FK（SQLite 限制）
ALTER TABLE paper_trading_trades    ADD COLUMN account_id INTEGER;

UPDATE paper_trading_positions
SET account_id = (SELECT MIN(id) FROM paper_trading_account)
WHERE account_id IS NULL;

UPDATE paper_trading_trades
SET account_id = (SELECT MIN(id) FROM paper_trading_account)
WHERE account_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_account_tenant ON paper_trading_account(tenant_id);
```

依据：持仓/流水两表当前连 account FK 都没有（docs/17:43；模型定义 m.py:1062-1117 确无 account_id 列），模拟盘引擎 `.first()` 全库单账户（docs/17:21 引 `paper_trading_engine.py:240-241`）。回填后 v121 重建为 NOT NULL + FK（§4.7）。

### 3.4 notify_channels 增补列（T21 托管渠道引用）

```sql
ALTER TABLE notify_channels ADD COLUMN is_shared INTEGER NOT NULL DEFAULT 0;
-- 语义：1 = 管理员托管渠道，其他租户可引用（只见 id/名称，不见 config 密钥）；0 = 租户私有
```

新租户默认零渠道（docs/17:223）为运行时行为，schema 层无默认值需求。

### 3.5 v120 runner 骨架（顺序即依赖序，全部幂等）

```
1. CREATE TABLE IF NOT EXISTS tenants / users / tenant_settings / tenant_news_pushed / agent_config_overrides（§2/§7 DDL，自包含）
2. 回填默认租户 id=1 + 初始管理员（§2）
3. 30 张 A 类表逐表 _add_column_if_missing(tenant_id) + tenant 索引（§3.1）
4. paper_trading 补 account_id + UPDATE 回填 + 账户唯一索引（§3.3）
5. notify_channels.is_shared（§3.4）
6. app_settings 三分迁移（§7.1：auth_*→users 删键；notify_*/ui_avatar 复制进 tenant_settings；写 single_tenant_mode）
7. log_entries/analysis_history 等无需额外动作的归因列已含于步骤 3
```

---

## 4. v121：约束重建清单（`__new` 重建，幂等可重入 + 清半成品）

### 4.0 与 docs/20-M5 的冲突裁决（上报备查）

M5（docs/20:54）与 R1-4（docs/17:157）把 notify_throttle 列入重建候选；但 R6-MT-P3（docs/17:190）与 C8（docs/20:44）明确"NotifyThrottle **不重建** UQ，tenant 编进 scope 字符串（`__notify__:{tenant}:{hash}`）"。**按 v1.1 修订节优先原则（docs/17:6），裁决：notify_throttle 仅加列不重建**。理由：scope 字符串已含 tenant 后，原 UQ(agent_name,stock_symbol) 键天然带租户维度，重建是纯成本。同时把 R2 改判带来的 news_topic_snapshots 补入重建清单（其 UQ(snapshot_date,window_days) 跨租户必撞，m.py:378-382）——最终重建 **8 张表**，仍落在 M5"5–7 处+新增"的量级内。

### 4.1 重建总表

| # | 表 | 现约束（证据） | 新约束 | 理由 |
|---|---|---|---|---|
| 4.2 | analysis_history | UQ(agent_name,stock_symbol,analysis_date) m.py:326-328 | UQ(tenant_id,agent_name,stock_symbol,analysis_date) | 同 agent+symbol+date 跨租户互覆（M5） |
| 4.3 | stock_context_snapshots | UQ(symbol,market,snapshot_date,context_type) m.py:348-354 | 加 tenant_id 前缀 | R2 私有后跨租户撞约束 |
| 4.4 | news_topic_snapshots | UQ(snapshot_date,window_days) m.py:378-382 | 加 tenant_id 前缀 | 同上（R2） |
| 4.5 | entry_candidates | UQ(stock_symbol,stock_market,snapshot_date) m.py:501-505 | UQ(tenant_id,stock_symbol,stock_market,snapshot_date) | T19：watchlist 行按租户、市场级行 tenant_id=0 仍全局唯一 |
| 4.6 | stocks | 无 UQ（m.py:82-102） | **新增** UQ(tenant_id,symbol,market) | T15/C6：12 处解析点改造后的正确性锚 |
| 4.7 | paper_trading_positions | 无 | account_id 改 NOT NULL+FK REFERENCES paper_trading_account(id) ON DELETE CASCADE；新增 ix(account_id,status) | T9 |
| 4.8 | paper_trading_trades | 无 | 同上；新增 ix(account_id,closed_at) | T9 |

不重建（FK 链传导/已天然租户内唯一，与 M5 一致）：positions.uq_account_stock、stock_agents.uq_stock_agent、stock_playbooks.uq_stock_playbook_version、price_alert_hits.uq_price_alert_rule_bucket、entry_candidate_outcomes.uq_entry_outcome_candidate_horizon、suggestion_feedback（无 UQ）、ai_models（无 UQ）、notify_throttle（§4.0）。

### 4.2–4.8 重建 SQL 模板

统一工序（沿用 `database.py:507-541` stocks__new 先例，每表独立事务，幂等）：

```
-- 每张表 X：
-- 0. DROP TABLE IF EXISTS X__new;            -- 清半成品（M3 断点重跑要求）
-- 1. PRAGMA foreign_keys=OFF;                 -- 必须在事务外/连接级设置（事务内是 no-op，docs/17:178）
-- 2. CREATE TABLE X__new (...新 DDL 全列+新 UQ+FK...);
-- 3. INSERT INTO X__new (cols...) SELECT cols... FROM X;   -- 列清单显式枚举，不用 SELECT *
-- 4. 对账：SELECT (SELECT COUNT(*) FROM X) = (SELECT COUNT(*) FROM X__new)，不等即 ROLLBACK 报错
-- 5. DROP TABLE X; ALTER TABLE X__new RENAME TO X;
-- 6. 重建原索引（CREATE INDEX IF NOT EXISTS ...）
-- 7. PRAGMA foreign_keys=ON;
```

示例一：analysis_history（4.2）

```sql
DROP TABLE IF EXISTS analysis_history__new;
CREATE TABLE analysis_history__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  agent_name VARCHAR NOT NULL,
  stock_symbol VARCHAR NOT NULL,
  analysis_date VARCHAR NOT NULL,
  title VARCHAR DEFAULT '',
  content VARCHAR NOT NULL,
  raw_data JSON DEFAULT '{}',
  agent_kind_snapshot VARCHAR DEFAULT 'workflow',
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_agent_stock_date UNIQUE (tenant_id, agent_name, stock_symbol, analysis_date)
);
INSERT INTO analysis_history__new (
  id, tenant_id, agent_name, stock_symbol, analysis_date, title, content,
  raw_data, agent_kind_snapshot, created_at, updated_at
)
SELECT id, COALESCE(tenant_id, 1), agent_name, stock_symbol, analysis_date, title, content,
       raw_data, agent_kind_snapshot, created_at, updated_at
FROM analysis_history;
-- 行数对账 → DROP analysis_history; ALTER TABLE analysis_history__new RENAME TO analysis_history;
CREATE INDEX IF NOT EXISTS ix_analysis_history_kind_date ON analysis_history(agent_kind_snapshot, analysis_date);
CREATE INDEX IF NOT EXISTS ix_analysis_history_agent_updated ON analysis_history(agent_name, updated_at);
CREATE INDEX IF NOT EXISTS ix_analysis_history_tenant ON analysis_history(tenant_id);
```

示例二：stocks（4.6，注意保留 m.py:89-92 的废弃可空列以保持列序一致，避免 create_all 与迁移库 schema 漂移——M7）

```sql
DROP TABLE IF EXISTS stocks__new;
CREATE TABLE stocks__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  symbol VARCHAR NOT NULL,
  name VARCHAR NOT NULL,
  market VARCHAR NOT NULL,
  cost_price FLOAT, quantity INTEGER, invested_amount FLOAT,   -- 已废弃列原样保留
  sort_order INTEGER DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_stocks_tenant_symbol_market UNIQUE (tenant_id, symbol, market)
);
INSERT INTO stocks__new (id, tenant_id, symbol, name, market, cost_price, quantity, invested_amount, sort_order, created_at, updated_at)
SELECT id, COALESCE(tenant_id,1), symbol, name, market, cost_price, quantity, invested_amount, sort_order, created_at, updated_at FROM stocks;
-- 对账 → DROP stocks; ALTER TABLE stocks__new RENAME TO stocks;
```

示例三：paper_trading_positions（4.7）

```sql
DROP TABLE IF EXISTS paper_trading_positions__new;
CREATE TABLE paper_trading_positions__new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL DEFAULT 1,
  account_id INTEGER NOT NULL REFERENCES paper_trading_account(id) ON DELETE CASCADE,
  stock_symbol VARCHAR NOT NULL,
  stock_market VARCHAR NOT NULL DEFAULT 'CN',
  stock_name VARCHAR DEFAULT '',
  quantity INTEGER NOT NULL DEFAULT 100,
  entry_price FLOAT NOT NULL,
  stop_loss FLOAT, target_price FLOAT, current_price FLOAT, highest_price FLOAT,
  unrealized_pnl FLOAT NOT NULL DEFAULT 0.0,
  status VARCHAR NOT NULL DEFAULT 'open',
  signal_run_id INTEGER, signal_snapshot_date VARCHAR DEFAULT '',
  signal_action VARCHAR DEFAULT '', strategy_code VARCHAR DEFAULT '',
  opened_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  closed_at DATETIME,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO paper_trading_positions__new (
  id, tenant_id, account_id, stock_symbol, stock_market, stock_name, quantity, entry_price,
  stop_loss, target_price, current_price, highest_price, unrealized_pnl, status,
  signal_run_id, signal_snapshot_date, signal_action, strategy_code, opened_at, closed_at, updated_at
)
SELECT id, COALESCE(tenant_id,1), account_id, stock_symbol, stock_market, stock_name, quantity, entry_price,
       stop_loss, target_price, current_price, highest_price, unrealized_pnl, status,
       signal_run_id, signal_snapshot_date, signal_action, strategy_code, opened_at, closed_at, updated_at
FROM paper_trading_positions;
-- 对账 → DROP → RENAME
CREATE INDEX IF NOT EXISTS ix_paper_pos_status ON paper_trading_positions(status);
CREATE INDEX IF NOT EXISTS ix_paper_pos_symbol_market ON paper_trading_positions(stock_symbol, stock_market);
CREATE INDEX IF NOT EXISTS ix_paper_pos_account_status ON paper_trading_positions(account_id, status);
```

其余 4 张（stock_context_snapshots / news_topic_snapshots / entry_candidates / paper_trading_trades）同构：新 UQ 加 `tenant_id` 前缀、paper_trading_trades 同 4.7 模式，列清单按 m.py 对应模型逐列枚举，索引按原 `__table_args__` 重建并补 `ix_<table>_tenant`。（docs/26-J1：portfolio_risk_snapshots 已移出重建清单，v121 共重建 7 张）

> ⚠ 双轨同步义务（docs/17 §2.2-2）：上述 7 张表的**新 DDL 必须同步回写 `models.py`**（含 entry_candidates 等），否则 create_all 新建库与迁移库 schema 漂移。models.py 改动属 MT-P2 施工范围，本文只定目标 DDL。

---

## 5. v122：对账（独立连接，失配即 fail）

事务内 `PRAGMA foreign_keys` 是 no-op（docs/17:178；docs/20 M3），故 v122 在 v121 提交后**另开独立连接**执行，任一检查非空即抛错使迁移失败（`mig.py:1693-1706` 失败路径会记 `schema_migrations.error` 并 raise，阻断启动）。

### 5.1 外键一致性

```sql
PRAGMA foreign_key_check;   -- 必须返回 0 行
```

### 5.2 子行 tenant 仅父派生不变量（T18 架构不变量，docs/20:32）

父表均为 v120 后 tenant_id=1，所有子行亦应=1；下列每条必须返回 0：

```sql
-- positions.tenant 必须等于其 account.tenant
SELECT COUNT(*) FROM positions p JOIN accounts a ON p.account_id=a.id
WHERE p.tenant_id <> a.tenant_id;
-- position_trades ← positions
SELECT COUNT(*) FROM position_trades t JOIN positions p ON t.position_id=p.id
WHERE t.tenant_id <> p.tenant_id;
-- positions/stock_agents/price_alert_rules/price_alert_hits/stock_playbooks ← stocks
SELECT COUNT(*) FROM positions p JOIN stocks s ON p.stock_id=s.id WHERE p.tenant_id <> s.tenant_id;
SELECT COUNT(*) FROM stock_agents x JOIN stocks s ON x.stock_id=s.id WHERE x.tenant_id <> s.tenant_id;
SELECT COUNT(*) FROM price_alert_rules r JOIN stocks s ON r.stock_id=s.id WHERE r.tenant_id <> s.tenant_id;
SELECT COUNT(*) FROM price_alert_hits h JOIN price_alert_rules r ON h.rule_id=r.id WHERE h.tenant_id <> r.tenant_id;
SELECT COUNT(*) FROM stock_playbooks b JOIN stocks s ON b.stock_id=s.id WHERE b.tenant_id <> s.tenant_id;
-- ai_models ← ai_services
SELECT COUNT(*) FROM ai_models m JOIN ai_services s ON m.service_id=s.id WHERE m.tenant_id <> s.tenant_id;
-- entry_candidate_outcomes / suggestion_feedback 逻辑父派生
SELECT COUNT(*) FROM entry_candidate_outcomes o JOIN entry_candidates c ON o.candidate_id=c.id
WHERE o.tenant_id <> c.tenant_id;
SELECT COUNT(*) FROM suggestion_feedback f JOIN stock_suggestions s ON f.suggestion_id=s.id
WHERE f.tenant_id <> s.tenant_id;
-- paper_trading 子表 ← paper_trading_account
SELECT COUNT(*) FROM paper_trading_positions p JOIN paper_trading_account a ON p.account_id=a.id
WHERE p.tenant_id <> a.tenant_id;
SELECT COUNT(*) FROM paper_trading_trades t JOIN paper_trading_account a ON t.account_id=a.id
WHERE t.tenant_id <> a.tenant_id;
-- chat_messages ← chat_conversations（逻辑父，m.py:1148 无 FK 声明）
SELECT COUNT(*) FROM chat_messages m JOIN chat_conversations c ON m.conversation_id=c.id
WHERE m.tenant_id <> c.tenant_id;
-- users.tenant_id 必须指向存在租户
SELECT COUNT(*) FROM users u LEFT JOIN tenants t ON u.tenant_id=t.id WHERE t.id IS NULL;
```

### 5.3 一致性锚点核对（C5/M7，docs/20:56）

```sql
-- 锚点 1：默认账户存在且有持仓 2150 股 @ 112.572
SELECT COUNT(*) FROM positions WHERE quantity = 2150 AND ABS(cost_price - 112.572) < 0.001;  -- 期望 ≥1
-- 锚点 2：该持仓关联流水恰 5 笔
SELECT COUNT(*) FROM position_trades t
JOIN positions p ON t.position_id = p.id
WHERE p.quantity = 2150 AND ABS(p.cost_price - 112.572) < 0.001;                             -- 期望 = 5
-- 锚点 3：playbook id=1 仍处于激活态
SELECT is_active FROM stock_playbooks WHERE id = 1;                                          -- 期望 = 1
-- 锚点 4：全部 A 类表除 entry_candidates 外，tenant_id=1 覆盖率 100%
-- （对 30 张 A 类表逐表执行：）SELECT COUNT(*) FROM <table> WHERE tenant_id <> 1;            -- 期望 = 0
-- entry_candidates 允许 tenant_id ∈ {0,1}
-- 锚点 5：users 恰 1 名 admin 且 tenant_id=1；tenants id=1 存在
SELECT COUNT(*) FROM users WHERE role='admin' AND tenant_id=1;                               -- 期望 = 1
```

### 5.4 行数总账

```sql
-- 对每张 v121 重建表：迁移前快照行数（v121 步骤 4 已即时对账）；
-- v122 再核 A 类表总行数与 v120 前基线（测试制品①的 v119 基线库副本，docs/20:56）一致。
```

---

## 6. DB 外落盘状态隔离设计（迁移脚本管不到的部分，docs/17 §2.3）

### 6.1 事件门状态文件 `data/state/intraday_monitor_state.json`（T7/R3/C1 分层）

现状：单文件按 symbol 键控，`last_price`/`tech_sig`/`pb_fired` 混存同一 record（`src/core/intraday_event_gate.py:251-342`；状态路径 :22-27；冷却键构造 :322-330）。

目标格式（分层，写入由 MT-P3 代码实现，本文定 schema 契约）：

```json
{
  "schema_version": 2,
  "market": {
    "600519": {
      "last_price": 1423.5,
      "change_pct": 1.2,
      "volume_ratio": 1.8,
      "tech_sig": {"trend": "...", "macd_status": "..."},
      "last_seen_at": "2026-07-22T07:00:00+00:00"
    }
  },
  "tenants": {
    "1": {
      "600519": {
        "pb_fired": {"上穿:止盈位@1500": "2026-07-22T06:30:00+00:00"}
      }
    }
  }
}
```

- **市场观测态**（last_price/tech_sig/change_pct/volume_ratio/last_seen_at）：单份共享，键=symbol（R3：按租户复制反而错误，docs/20:14）。
- **租户态**（pb_fired 冷却记录）：键=`(tenant_id, symbol)`；冷却键本身仍为 `方向:位名@价位`（:322），因存放路径已含 tenant，两租户不再互吞 1800s 冷却（:96）。
- `_load_playbook_levels`（:206-227）必须按 tenant 过滤 stocks/playbooks——属 MT-P3 代码改动，本文仅在 §5.2 提供 stocks↔playbooks tenant 一致性的 DB 侧保证。
- 一次性格式迁移：启动时读旧格式（无 `schema_version`），把 last_price/tech_sig 等观测字段并入 `market` 层、`pb_fired` 落入 `tenants["1"]`，原子写回（复用 `json_store.write_json_atomic`，:17）。幂等：`schema_version=2` 即跳过。

### 6.2 头像目录 `data/avatars/`

现状：全实例单头像，DB `app_settings.ui_avatar` 存文件名、图片落 `data/avatars/avatar.*`（`src/web/api/settings.py:103,114,136`）。

目标：`data/tenants/{tenant_id}/avatars/avatar.*`；DB 键迁入 `tenant_settings`（§7.1）。迁移动作：若 `data/avatars/` 存在旧文件，整体移入 `data/tenants/1/avatars/`（幂等：目标已存在则跳过）。MT-P2 代码按 tenant 读写新路径。

### 6.3 报告目录 `data/reports/`

现状：全实例一目录，下载路由无鉴权、文件名可枚举（docs/17:67 引 `app.py:203-227`）。

目标：`data/tenants/{tenant_id}/reports/`；存量文件移入 tenant 1 子目录（幂等同上）。下载鉴权+签名 URL 属 API 层（查询审计清单范围），目录隔离是本设计给出的前置条件。

### 6.4 备份文件脱敏 `panwatch.db.bak.*`（T14/M9）

现状：`database.py:114-131` copy2 生成 `panwatch.db.bak.{ts}`，含 ai_services.api_key、notify_channels.config bot_token 明文（m.py:28,59）；实测生产目录已存在 2 份（`panwatch.db.bak.20260722_005130` / `_013922`）。

设计（配合 §8 新备份函数）：
- **原地热备**（同卷，供回滚）：保留完整内容——回滚必须能恢复密钥。
- **出卷冷备**（Hyper Backup/异机）：生成**脱敏副本**——复制后对副本执行 `UPDATE ai_services SET api_key=''; UPDATE notify_channels SET config='{}'; UPDATE users SET password_hash='REDACTED';`，脱敏副本仅供灾难恢复重建结构，密钥由管理员重录。
- 轮转 ≤3 份（含热备+脱敏各一序列），超出删最旧（R5，docs/17:183）。

---

## 7. 新表与配置迁移

### 7.1 app_settings 三分迁移映射表（T20/M13）

现状键证据：`auth.py:29-31`（auth_username/auth_password_hash/jwt_secret）、`settings.py:51-57`（http_proxy/notify_*/panwatch_base_url）、`settings.py:103`（ui_avatar）、`notifier.py:21` 与 `analysis_link.py:16`（消费方）。

| 键 | 现位置 | 目标位置 | v120 动作 |
|---|---|---|---|
| jwt_secret | app_settings | **实例级**，留 app_settings | 不动 |
| http_proxy | app_settings | 实例级（进程 env，天然无法按租户，docs/17:79） | 不动 |
| panwatch_base_url | app_settings | 实例级（签名 URL 的 base，全局一份） | 不动 |
| single_tenant_mode | （新增） | 实例级 app_settings，默认 `'1'`（T20 单租户回退 flag；M14 验收=611 全绿） | INSERT IF NOT EXISTS |
| auth_username / auth_password_hash | app_settings | **users 表**（§2.2 回填，algo='sha256_legacy'） | 搬迁后 DELETE 两键 |
| ui_avatar | app_settings | **tenant_settings**（tenant_id=1） | 复制不删（见下） |
| notify_quiet_hours / notify_retry_attempts / notify_retry_backoff_seconds / notify_dedupe_ttl_overrides | app_settings | **tenant_settings**（tenant_id=1） | 复制不删 |

> 复制不删的原因：v120 落在 MT-P2，而读路径切换（settings.py/notifier.py 改造）同阶段施工；为允许分步上线，v120 只双写式复制，**旧键删除推迟到读路径切换完成后的后续迁移**（编号 ≥v123，施工期定）。

tenant_settings DDL：

```sql
CREATE TABLE IF NOT EXISTS tenant_settings (
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT DEFAULT '',
  description TEXT DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, key)
);
-- 回填（幂等）：
INSERT OR IGNORE INTO tenant_settings (tenant_id, key, value, description)
SELECT 1, key, value, description FROM app_settings
WHERE key IN ('ui_avatar','notify_quiet_hours','notify_retry_attempts',
              'notify_retry_backoff_seconds','notify_dedupe_ttl_overrides');
```

### 7.2 cost_tracker / log_entries tenant 归因（T13/T20/M12）

- `cost_tracker` 不是表，是 `src/agents/tradingagents/cost_tracker.py:1-60` 的模块：对 `AnalysisHistory.raw_data["cost_usd"]` 做 SQL 聚合。**schema 层归因 = analysis_history.tenant_id（§3.1 已含）**；MT-P3 给 `check_budget` 查询加 tenant 过滤即可，不加新列（raw_data JSON 提取维持现状，改动最小）。配额语义：`users.quota_shared_with_admin=1` 的租户计量归入 tenant 1 预算；=0 的租户无配额、须走自建 ai_services（T13）。
- `log_entries` 加 `tenant_id`（§3.1 已含）+ `ix_log_entries_tenant` 索引；归因写入由 log_handler 运行时填（T16 contextvar 读取），schema 层无额外动作。`DELETE /api/logs` 全表清空端点（docs/17:63）属查询审计范围。

### 7.3 tenant_news_pushed（T8：共享去重 + 记录租户已推，新租户不补发）

```sql
CREATE TABLE IF NOT EXISTS tenant_news_pushed (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  news_id INTEGER NOT NULL REFERENCES news_cache(id) ON DELETE CASCADE,
  channel_id INTEGER,                          -- 推送所用渠道（可空，便于排查）
  pushed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_tenant_news_pushed UNIQUE (tenant_id, news_id)
);
CREATE INDEX IF NOT EXISTS ix_tenant_news_pushed_news ON tenant_news_pushed(news_id);
```

语义：news_cache 全实例一份（#14）；某条新闻对某租户推送成功后写一行。**新租户注册时不回填历史 news_cache** → 天然"不补发"（docs/17:210）。v120 无存量回填（单租户期推送历史可不追溯，或可选回填当日行——取不回填，保守）。

### 7.4 agent_config_overrides（T4：override 表 + 整体替换 + 优先级链）

```sql
CREATE TABLE IF NOT EXISTS agent_config_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  agent_name VARCHAR NOT NULL,                 -- 跨表字符串键，对应 agent_configs.name（m.py:197）
  enabled INTEGER,                             -- 以下字段整体替换语义：行存在即覆盖 template 同名字段
  schedule VARCHAR,
  execution_mode VARCHAR,
  ai_model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL,
  notify_channel_ids JSON,
  config JSON,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT uq_agent_override_tenant_name UNIQUE (tenant_id, agent_name)
);
```

解析优先级链（运行时在 context_builder 实现，docs/20:80）：`stock_agent（行内配置） > tenant_override（本表） > template（agent_configs） > 系统默认`。override 语义=**整体替换**而非字段 merge：租户一旦建立 override 行，该行非空字段全集生效，避免 template seed 升级时半新半旧。agent_configs 本体保持实例级模板（#9），seed 升级不影响租户行。

---

## 8. R5 备份修复设计（v120 的前置任务，C3/M9）

现状缺陷：`database.py:114-131 _backup_db_before_migration` 用 `shutil.copy2` 只拷主文件；WAL 模式（`database.py:31`）下 -wal/-shm 未拷，备份非一致性快照且可能丢最近事务；无轮转、与主库同卷（docs/20:23）。

替代设计（MT-P2 施工，本设计定契约）：

```
def _backup_db_before_migration():      # 重写，函数签名/调用点不变（database.py:61）
    1. pre-flight：磁盘剩余 ≥ 2×DB 大小（docs/20:83），不足直接 fail
    2. 另开独立连接执行 VACUUM INTO '<DB_PATH>.bak.<ts>'
       —— 首选：单文件、事务一致、无需处理 -wal/-shm；VACUUM INTO 不能在事务内跑，须独立连接
       —— 备选（SQLite < 3.27 无 VACUUM INTO）：PRAGMA wal_checkpoint(TRUNCATE) 后 copy2
    3. 对备份文件执行 PRAGMA integrity_check，非 'ok' 即删备份并 fail（恢复侧同要求，docs/17:183）
    4. 轮转：保留最近 ≤3 份热备（panwatch.db.bak.*），超出删最旧
    5. 生成脱敏副本 .bak.<ts>.sanitized 供出卷冷备（§6.4）
    6. 迁移窗口 = 服务全停（init_db 在监听前完成，docs/20:83），备份失败则迁移不启动
```

与 T11 发布 SOP 的接口：破坏式迁移版本独立 tag + 人工 promote，SOP=暂停 Watchtower→出卷冷备→升级→验证→恢复（docs/20:28）；回滚 runbook 依赖本节的 integrity_check 通过的热备。

---

## 9. 需 Orchestrator 裁决/跨文档协同事项

| # | 事项 | 说明 |
|---|---|---|
| U-1 | entry_candidates 的 tenant_id=0 市场级哨兵 | T19"按 candidate_source 拆语义"的 schema 落地选择。do_orm_execute 自动过滤（T16）需对该表开 `tenant_id IN (:t, 0)` 特例——**身份穿透设计文档必须吸收本约定**，否则市场级行对所有租户不可见或租户行被错误合并 |
| U-2 | notify_throttle 不重建 UQ 的裁决 | M5/R1-4 列入重建候选 vs R6/C8 明确不重建；本文按修订节优先取后者（§4.0），如 Orchestrator 认为应统一口径，需在 docs/20 M5 行加勘误 |
| U-3 | chat_messages.conversation_id 无 FK 声明 | m.py:1148 仅 Integer 无 ForeignKey；v121 不重建该表（不加 FK，避免 orphan 消息阻断迁移），仅 v122 逻辑校验。若要求 DB 级 FK，需先做孤儿数据清理，提请确认 |
| U-4 | tenant_news_pushed 存量不回填 | 单租户期推送历史不追溯（§7.3）；如需"升级当天不重推"，可回填当日 news_cache 行，提请确认 |
| U-5 | entry_candidates 之外，agent_prediction_outcomes 归类 | T19 列举的市场级链为 market_scan/strategy_signal/outcomes/factor_snapshots；本文将 agent_prediction_outcomes（Agent 建议后验）判私有（#20），因其数据源是租户 Agent 执行。若评审原意包含该表，需改判并移出 v121 范围——提请确认 |
| U-6 | models.py 同步改写清单 | 本文 §4 目标 DDL 与 §1 分类需原样回写 models.py（MT-P2）；建议查询审计清单文档引用本文 #1–#50 编号作为逐表过滤标注的锚 |

---

*本文仅为 MT-P0 设计稿；任何代码、配置、测试文件均未改动。施工入口：MT-P0.5 机制点（T16）→ MT-P1 身份骨架（§2 users 先行可用）→ MT-P2 数据层（§3–§8 + R5 前置）。*
