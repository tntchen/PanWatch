# 19 · Tushare 数据源回归计划

> 状态：待所有者确认决策点（D1–D4），确认后才动代码。
> 日期：2026-07-22

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

## 决策点（待所有者逐条确认）

- **D1 · 默认启用状态**：建议 `enabled=False`（需 token 才能用，默认启用会产生无意义失败调用）。是否同意？
- **D2 · 依赖安装方式**：
  - (a) 软依赖（推荐，沿用旧设计）：不写进 requirements.txt，Docker 镜像不装；用户在文档/UI 提示下自行 `pip install tushare`。镜像体积不增。
  - (b) 必装：写进 requirements.txt + Dockerfile，开箱可用但镜像变大（tushare 依赖 pandas 等）。
- **D3 · 回归范围**：建议仅恢复 K 线（与旧行为一致）。Tushare 还有基本面/资金流/龙虎榜等接口，本次不扩展。是否同意？
- **D4 · 同步上游**：本改动是 fork 私有，还是整理成 PR 提回上游 TNT-Likely/PanWatch？（影响分支与提交粒度安排）

## 变更清单模板（完成后填写审计）

- [ ] 新增/修改文件列表
- [ ] 测试结果
- [ ] 数据源页实测截图/日志
