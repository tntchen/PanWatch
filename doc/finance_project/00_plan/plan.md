# 东芯股份抄底分析报告 — 执行计划

**任务**：从半导体行业 + 上下游产业链/相关公司角度，生成东芯股份（688110.SH）详细分析报告 + 抄底实施方案
**数据源优先级**：iFinD 插件（行情/财务/公告）+ 天眼查插件（工商/股东/对外投资）+ 网络搜索（行业动态）
**当前日期**：2026-07-17

## Stage 1 — 研究阶段（deep-research-swarm，多子代理并行）
- 先读 `/app/.agents/plugins/ifind/skills/ifind/SKILL.md` 与 `/app/.agents/plugins/tianyancha/skills/tianyancha/SKILL.md`，掌握插件调用方式
- 并行研究代理：
  1. **公司基本面**：东芯最新行情、财务三表、主营构成、业绩预告/公告（iFinD）
  2. **行业研究**：存储芯片（NOR Flash / SLC NAND / DRAM）行业周期、价格趋势、供需格局、国产替代进展（iFinD + 搜索）
  3. **产业链图谱**：上游（晶圆代工/封测/设备材料）、下游（通信/汽车/工控/消费电子）、可比公司（兆易创新、普冉股份、恒烁股份、复旦微电等）横向对比（iFinD + 搜索）
  4. **资金与技术面**：近月股价走势、主力资金流向、龙虎榜、股东户数、解禁/减持（iFinD + 搜索）
  5. **公司资质核查**：工商信息、股权结构、对外投资、风险信息（天眼查）
- 输出：各方向研究简报（带来源标注）

## Stage 2 — 写作阶段（report-writing）
- 读 `/app/.agents/skills/report-writing/SKILL.md`，按其流程：大纲 → 分章写作 → 组装
- 报告结构：公司概况 → 行业周期判断 → 产业链与竞争格局 → 财务与估值 → 资金/技术面 → 风险清单 → 抄底实施方案（分批建仓、止损、催化剂日历）
- 输出：`.agent.final.md`

## Stage 3 — 格式化阶段（docx）
- 读 `/app/.agents/skills/docx/SKILL.md`，将最终 md 转为 .docx
- 输出：md + docx 双格式交付
