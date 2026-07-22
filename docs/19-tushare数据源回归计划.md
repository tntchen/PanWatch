# 19 · Tushare 数据源回归计划

> 状态：✅ 已完成（2026-07-22，D1–D4 按推荐口径、D4 fork 私有、D5 同意）。改动未提交。
> 日期：2026-07-22 ｜ 2026-07-22 晚追加「多租户影响评估」（MT-P5 闭环后）

## 背景

上游 TNT-Likely/PanWatch 在 commit `5b5ac16`（PR #88，数据层抽独立包 marketdata）中删除了旧
`src/core/providers/kline/tushare.py`，新包 `packages/marketdata` 未注册 tushare vendor。
非本 fork 二开删除。

旧实现行为（作为回归基线）：

- 仅 K 线（日线）、仅 A 股（supports_markets = {"CN"}）
- 软依赖：未安装 `tushare` 包或未配 token 时初始化即 disabled，fetch 返回明确错误
- token 来源：`DataSource.config["token"]` → 环境变量 `TUSHARE_TOKEN` 兜底
- 种子位：kline / tushare，priority = 10（腾讯 0、东财 5 之后）

## 现状残留（加回时需一并处理）

| 位置 | 现状 | 处理 |
|---|---|---|
| `server.py:409` 注释 | 提到 Tushare(10) 优先级空位 | 种子补回后注释自然成立 |
| `frontend/src/pages/DataSources.tsx:83-84` | tushare token 配置表单仍在（孤儿 UI） | 无需改，vendor 注册后即生效 |
| `tests/test_datasource_reconcile.py:29-93` | 断言 `kline/tushare` 为孤儿应被删 | 需反转：改为断言补回后不再是孤儿 |
| `tests/test_datasource_test_path.py:92-98` | 用 tushare 举例"包内无 vendor 返回明确 error" | 需换一个不存在的 provider 举例 |
| `requirements*.txt` / Docker | 无 tushare 依赖 | 见 D2 |

## 实施步骤（确认后执行）

1. **新增 vendor**：`packages/marketdata/src/marketdata/vendors/tushare.py`
   - 实现 `TushareKlineVendor(KlineVendor)`，`name = "tushare"`，`supports_markets = {"CN"}`
   - 接口对齐现有 vendor：`fetch(symbols: list[Symbol], config: dict) -> list[Bar]`
   - `tushare` 包在 fetch 内惰性 import（对齐 registry.py 注释"可选三方依赖惰性 import"原则，参照 yfinance vendor 写法）
   - token 取 `config["token"]` → `os.environ["TUSHARE_TOKEN"]`；缺失/未装包时返回 `[]` 并记明确日志（新架构下 vendor 返回空由 Engine 落到下一优先级源，错误经 MetricsSink 透出）
   - `pro.daily(ts_code, start_date, end_date)` 取数，date 格式转 `YYYY-MM-DD`，字段映射到 `Bar(date/open/close/high/low/volume/turnover)`
2. **注册**：`packages/marketdata/src/marketdata/registry.py` 的 `VENDOR_CLASSES_BY_TYPE["kline"]` 增加 `"tushare": TushareKlineVendor`
3. **种子**：`server.py` `DATA_SOURCE_SEEDS` 补回 "Tushare K线"（type=kline, provider=tushare, priority=10, enabled 见 D1, test_symbols=["600519"]）
   - 启动对账 `reconcile_data_sources` 的 legal 集合 = 包内 vendor ∪ seed provider，两处都补上后即不再是孤儿
4. **依赖**：见 D2
5. **测试**：
   - 反转 `test_datasource_reconcile.py` 中 kline/tushare 孤儿断言
   - `test_datasource_test_path.py` 的"无 vendor"用例改用虚构 provider 名
   - `packages/marketdata/tests/` 新增 tushare vendor 单测（mock pro_api，覆盖：未装包/无 token/正常返回/空数据）
6. **验证**：`pytest` 全绿 + 数据源页手动测试（配 token 后测试 600519 返回 K线；不配 token 返回明确错误）

## 多租户影响评估（2026-07-22 追加，MT-P1~P5 完成后复核）

### 兼容无冲突项（原方案照旧有效）

1. **种子位/对账**：`data_sources` 是实例级共享表（无 tenant_id 列，docs/27 行31「实例级资源」），
   补回 "Tushare K线"（priority=10）无需租户维度；`reconcile_data_sources` 自 MT-P4 起为
   require_admin，种子「只动模板行」契约不受影响。
2. **vendor/引擎链**：marketdata vendors 无状态，K 线属 T7 市场级共享观测数据；调度租户扇出后
   各租户读同一份市场观测态——与多租户架构零冲突。
3. **env `TUSHARE_TOKEN` 兜底**：T20「env 凭证→初始管理员」，数据源为实例级资源，env 兜底
   天然管理员托管，与 F6（AI env 回退限管理员租户）语义一致、互不干扰。
4. **前端孤儿 UI**：DataSources.tsx 的 tushare token 表单在编辑弹窗内，MT-P4 已把该弹窗门控为
   管理员专属——普通用户看不到表单，恰好符合 T3 托管语义，无需额外改动。
5. **测试改动点**（reconcile 断言反转等）与多租户无交集；824 基线不受冲击。

### 新发现的影响（原方案未评估，必须处理）

6. **凭证可见性缺口（T3 相关，实质新增）**：`GET /api/datasources` 的 `_to_response` 对任何
   登录用户返回 `config` 明文。MT-P4 已掩码 `ai_services.api_key` 与 `notify_channels.config`，
   但 `data_sources.config` 漏掩（docs/27 行31「普通用户只读可见状态」未裁决"只读"是否含凭证
   字段）。tushare 回归将第一个踩中：token 配进去后普通租户可经 list/detail 接口直接读到。
   → 新增决策点 D5。
7. **test 端点未门控（轻度）**：`POST /{id}/test` 任何登录用户可触发真实取数（消耗 token
   配额/上游限流额度）。建议维持现状（只读连通性，单租户等价），记录在案。

## 决策点（待所有者逐条确认）

- **D1 · 默认启用状态**：建议 `enabled=False`（需 token 才能用，默认启用会产生无意义失败调用）。是否同意？
- **D2 · 依赖安装方式**：
  - (a) 软依赖（推荐，沿用旧设计）：不写进 requirements.txt，Docker 镜像不装；用户在文档/UI 提示下自行 `pip install tushare`。镜像体积不增。
  - (b) 必装：写进 requirements.txt + Dockerfile，开箱可用但镜像变大（tushare 依赖 pandas 等）。
- **D3 · 回归范围**：建议仅恢复 K 线（与旧行为一致）。Tushare 还有基本面/资金流/龙虎榜等接口，本次不扩展。是否同意？
- **D4 · 同步上游**：本改动是 fork 私有，还是整理成 PR 提回上游 TNT-Likely/PanWatch？（影响分支与提交粒度安排）
- **D5 · data_sources config 凭证掩码（多租户新增）**：建议随本次回归一并修——`GET /api/datasources`
  list/detail 响应对非 admin 用户将 `config` 掩为 `{}`（前端普通用户本就不渲染 config；admin 与
  单租户直通行为不变），并补一条跨租户用例进 tests/。是否同意？

## 变更清单（2026-07-22 完成）

- 新增 `packages/marketdata/src/marketdata/vendors/tushare.py`（TushareKlineVendor：惰性 import、
  token=config["token"]→env TUSHARE_TOKEN、pro.daily 取数、ts_code 复用 _cn_exchange、
  vol=手原样透传、amount×1000→turnover、降级返回 [] 由 Engine 落下一优先级源）
- 注册 `packages/marketdata/src/marketdata/registry.py`（VENDOR_CLASSES_BY_TYPE["kline"]["tushare"]）
- 种子 `server.py` DATA_SOURCE_SEEDS 补回 "Tushare K线"（priority=10、enabled=False、
  test_symbols=["600519"]）——已落真实库验证
- D5 掩码 `src/web/api/datasources.py`：_mt_ctx 范式，多租户非 admin 的 list/detail config 掩为 {}；
  单租户/admin 明文不变；前端核对无需改动（普通用户本就不渲染 config）
- 测试：`packages/marketdata/tests/test_tushare_kline.py` 新增 12 例；`test_registry.py` 补 vendor；
  `test_datasource_reconcile.py` 孤儿断言反转为非孤儿；`test_datasource_test_path.py` 换虚构
  provider；`tests/test_datasource_config_mask.py` 新增 4 例（D5）
- 测试结果：主套件 **828 passed** + marketdata 包 **215 passed**，零失败
- 实测：服务重启后种子已落库（Tushare K线, priority=10, enabled=False）；
  **真实取数未验证**——venv 未装 tushare（D2 软依赖），需 `pip install tushare` + 配置 token
  后在数据源页手动测 600519
