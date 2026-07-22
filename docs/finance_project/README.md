# finance_project — 东芯股份抄底分析项目归档

> 任务日期：2026-07-17 ｜ 标的：东芯股份（688110.SH）｜ 基准价：2026-07-16 收盘 136.50 元
> 本目录为「个股深度研究 + 抄底方案」任务的完整归档，将作为固定化 work 项目的模板。

## 目录结构

```
finance_project/
├── README.md                     ← 本文件（项目导航）
├── 00_plan/
│   └── plan.md                   ← 任务执行计划（阶段划分、数据源策略、技能加载方案）
├── 01_research/                  ← 研究阶段产物（8 维度 + 验证 + 洞察）
│   ├── dongxin_dim01.md          ← 公司基本面与财务（iFinD）
│   ├── dongxin_dim02.md          ← 股价走势、资金流向、筹码结构
│   ├── dongxin_dim03.md          ← 存储行业周期与价格趋势
│   ├── dongxin_dim04.md          ← 产业链上游与供应链
│   ├── dongxin_dim05.md          ← 下游需求与应用（含砺算/Wi-Fi 7/车规）
│   ├── dongxin_dim06.md          ← 可比公司横向对比（iFinD）
│   ├── dongxin_dim07.md          ← 催化剂、公告与风险（iFinD 公告 129 条）
│   ├── dongxin_dim08.md          ← 工商治理核查（天眼查）+ 机构观点
│   ├── dongxin_cross_verification.md ← 交叉验证（置信分级 + 冲突裁决）
│   ├── dongxin_insight.md        ← 6 条跨维度洞察（方案逻辑骨架）
│   └── data/                     ← iFinD 原始 CSV 数据（行情/财务/预测）
├── 02_writing/                   ← 写作阶段产物
│   ├── dongxin_report.agent.outline.md ← 报告执行大纲（4 级标题契约）
│   ├── dongxin_report_sec00.md   ← 执行摘要
│   ├── dongxin_report_sec01~08.md ← 第 1~8 章
│   ├── dongxin_report_sec00~08_refs.md ← 各章引用映射表
│   └── *.png                     ← 3 张图表（SLATE 配色）
├── 03_deliverable/               ← 最终交付
│   ├── dongxin_report.agent.final.md  ← 终稿 Markdown（6.2 万字、135 条参考文献）
│   └── 东芯股份抄底分析报告.docx      ← 终稿 Word（47 页、16 表、3 图）
├── 04_review/
│   └── 审校记录.md               ← 审校修复 4 处 + 全项核对记录
└── 05_retrospective/
    └── 复盘总结.md               ← 做得好/可改进/可拓展 + 下次启动模板
```

## 复用方式

1. 读 `05_retrospective/复盘总结.md` 第五节「启动模板」，替换 ticker 与持仓参数即可发起同类任务
2. `01_research/` 的 8 个维度划分即研究 checklist；`02_writing/` 的大纲即写作契约样板
3. 核心结论速览：先看 `03_deliverable/` 终稿的 Executive Summary，再看第 8 章抄底方案

## 数据有效期声明

本报告基于 2026-07-16 收盘及之前的公开数据。存储板块波动剧烈（单日 ±5% 以上常见），方案中的价位触发器与催化日历需结合最新行情校准后再执行。报告为研究分析框架，不构成投资建议。
