"""尾盘简报 Agent（P3d）—— 14:45 尾盘决策窗口，仅对有方案档案的股票执行。

契约（doc/12 §3 P3d / doc/14 §1 P3d）：
- 输出模板契约校验：正文必须含「持有/执行右侧批/做T减出/减仓/观望」
  五选一结论 + 数量（X股）+ 价格区间（如 110-115）；
  校验不过则不推送、落日志。
- prompt 渲染近期流水（P3a v1.5）：执行细节必须考虑当日已有操作
  （如当日已做T减出则提示接回区而非重复卖出）。
- 其余（传参式 run_single / 档案门控 / 校验框架）见基类 BasePlaybookBriefAgent。
"""

from __future__ import annotations

from pathlib import Path

from src.agents.morning_brief import BasePlaybookBriefAgent

# 尾盘简报决策五选一（词表与现有 *_ACTION_MAP 的 action 取值同步）
TAIL_ACTION_MAP = {
    "持有": {"action": "hold", "label": "持有"},
    "执行右侧批": {"action": "add", "label": "执行右侧批"},
    "做T减出": {"action": "sell", "label": "做T减出"},
    "减仓": {"action": "reduce", "label": "减仓"},
    "观望": {"action": "watch", "label": "观望"},
}

PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "tail_brief.txt"


class TailBriefAgent(BasePlaybookBriefAgent):
    """尾盘简报 Agent（14:45 决策：五选一 + 数量与价格区间）"""

    name = "tail_brief"
    display_name = "尾盘简报"
    description = "尾盘决策窗口：五选一结论 + 执行细节（数量+价格区间），仅方案档案股票"

    action_map = TAIL_ACTION_MAP
    require_execution_detail = True
    brief_label = "尾盘简报"
    prompt_path = PROMPT_PATH
