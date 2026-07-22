"""早盘简报 Agent（P3d）—— 10:00 开盘半小时后定势，仅对有方案档案的股票执行。

契约（doc/12 §3 P3d / doc/14 §1 P3d）：
- single 逐股传参式 run_single：浅克隆 context + 窄化 watchlist，
  不改共享 watchlist（并发安全）；无方案档案的股票跳过并记日志。
- 输出模板契约校验：正文必须含「可执行/观望/冻结」三选一结论；
  校验不过则不推送、落日志（结果仍落 analysis_history 便于排查）。
- 建议词表与现有 *_ACTION_MAP 约定保持同步（action ∈ ALLOWED_ACTIONS）。
"""

from __future__ import annotations

import logging
import re
from copy import copy
from datetime import datetime
from pathlib import Path

from src.agents.base import BaseAgent, AgentContext, AnalysisResult
from src.config import AppConfig
from src.core.analysis_history import save_analysis
from src.core.context_builder import ContextBuilder
from src.core.signals import SignalPackBuilder
from src.core.signals.structured_output import (
    strip_tagged_json,
    try_extract_tagged_json,
)
from src.models.market import MarketCode

logger = logging.getLogger(__name__)

# 早盘简报结论三选一（词表与现有 *_ACTION_MAP 的 action 取值同步）
MORNING_ACTION_MAP = {
    "可执行": {"action": "add", "label": "可执行"},
    "观望": {"action": "watch", "label": "观望"},
    "冻结": {"action": "avoid", "label": "冻结"},
}

PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "morning_brief.txt"

# 简报正文预算（字符）。模板要求 ≤180 字，留出标题行与免责声明余量，
# 超限只告警不拦截（硬契约是结论关键词，长度是软约束）。
BRIEF_CHAR_SOFT_LIMIT = 240

_QUANTITY_RE = re.compile(r"\d+\s*股")
_PRICE_RANGE_RE = re.compile(r"\d+(?:\.\d+)?\s*[-~—–至]\s*\d+(?:\.\d+)?")


def _load_playbook_summary(symbol: str, market) -> str | None:
    """读取该股票激活方案档案的摘要；无档案/股票不存在/异常均返回 None。

    惰性 import + 每次现取 SessionLocal，便于测试用内存库 monkeypatch。
    """
    try:
        from src.core.playbook import load_active_playbook, summarize_playbook
        from src.web.database import SessionLocal
        from src.web.models import Stock

        db = SessionLocal()
        try:
            mkt = market.value if isinstance(market, MarketCode) else str(market or "")
            stock = (
                db.query(Stock)
                .filter(Stock.symbol == symbol, Stock.market == mkt)
                .first()
            )
            if not stock:
                return None
            row = load_active_playbook(db, stock.id)
            if row is None or not isinstance(row.payload, dict):
                return None
            return summarize_playbook(row.payload) or None
        finally:
            db.close()
    except Exception as e:
        logger.warning("读取方案档案失败 %s: %s", symbol, e)
        return None


def validate_brief_contract(
    display_text: str,
    structured: dict | None,
    *,
    action_map: dict[str, dict],
    require_execution_detail: bool,
) -> tuple[bool, str, str | None]:
    """校验简报模板契约。

    Returns:
        (是否通过, 原因, 命中的结论标签)。
        原因取值: ok / missing_action_keyword / missing_quantity /
        missing_price_range。
    """
    text = display_text or ""
    structured = structured if isinstance(structured, dict) else {}

    hit: str | None = None
    # 1) 优先结构化 JSON 的 action_label / action
    label = str(structured.get("action_label") or "").strip()
    if label in action_map:
        hit = label
    else:
        action = str(structured.get("action") or "").strip()
        for lbl, info in action_map.items():
            if info.get("action") == action:
                hit = lbl
                break
    # 2) 回退正文关键词（长标签优先，避免子串误命中）
    if hit is None:
        for lbl in sorted(action_map.keys(), key=len, reverse=True):
            if lbl in text:
                hit = lbl
                break
    if hit is None:
        return False, "missing_action_keyword", None

    if require_execution_detail:
        if not _QUANTITY_RE.search(text):
            return False, "missing_quantity", hit
        if not _PRICE_RANGE_RE.search(text):
            return False, "missing_price_range", hit
    return True, "ok", hit


class BasePlaybookBriefAgent(BaseAgent):
    """早盘/尾盘简报公共基类：仅档案股票、传参式 run_single、契约校验。"""

    # 子类必填
    action_map: dict[str, dict] = {}
    require_execution_detail: bool = False
    brief_label: str = ""  # 日志/标题里的中文简报名
    prompt_path: Path = PROMPT_PATH

    async def run_single(
        self, context: AgentContext, symbol: str
    ) -> AnalysisResult | None:
        """单只股票传参式入口（供 AgentScheduler single 模式逐股调用）。

        浅克隆 context + 窄化 AppConfig.watchlist，不改共享 context；
        无方案档案的股票跳过（返回 None）并记日志。
        """
        targets = [s for s in context.watchlist if s.symbol == symbol]
        if not targets:
            logger.warning(
                "[%s] run_single: symbol=%s 不在 watchlist 中,跳过", self.name, symbol
            )
            return None
        stock = targets[0]

        playbook_summary = _load_playbook_summary(stock.symbol, stock.market)
        if not playbook_summary:
            logger.info(
                "[%s] %s(%s) 无方案档案,跳过%s",
                self.name,
                stock.name or symbol,
                symbol,
                self.brief_label,
            )
            return None

        narrow_config = AppConfig(
            settings=context.config.settings,
            watchlist=[stock],
        )
        narrow_context = copy(context)
        narrow_context.config = narrow_config
        # 把摘要随窄化 context 传给 collect/build_prompt（避免重复查库）
        setattr(narrow_context, "_playbook_summary", playbook_summary)
        return await self.run(narrow_context)

    async def collect(self, context: AgentContext) -> dict:
        """采集单只股票的行情/技术/新闻/事件 + 上下文（含 playbook 键）。"""
        stock = context.watchlist[0]
        builder = SignalPackBuilder()
        packs = await builder.build_for_symbols(
            symbols=[(stock.symbol, stock.market, stock.name)],
            include_news=True,
            news_hours=24,
            portfolio=context.portfolio,
            include_technical=True,
            include_capital_flow=True,
            include_events=True,
            events_days=7,
        )
        context_pack = await ContextBuilder().build_symbol_contexts(
            agent_name=self.name,
            context=context,
            packs=packs,
            realtime_hours=12,
            extended_hours=48,
            history_days=30,
            kline_days=120,
            persist_snapshot=True,
        )
        symbol_ctx = (context_pack.get("symbols") or {}).get(stock.symbol) or {}
        playbook_summary = getattr(context, "_playbook_summary", None) or symbol_ctx.get(
            "playbook"
        )
        return {
            "stock": stock,
            "pack": packs.get(stock.symbol),
            "symbol_ctx": symbol_ctx,
            "playbook_summary": playbook_summary,
            "timestamp": datetime.now().isoformat(),
        }

    def build_prompt(self, data: dict, context: AgentContext) -> tuple[str, str]:
        """构建简报 prompt：行情 + 方案摘要 + 持仓 + 近期流水（P3a v1.5）。"""
        system_prompt = self.prompt_path.read_text(encoding="utf-8")
        stock = data["stock"]
        pack = data.get("pack")
        symbol_ctx = data.get("symbol_ctx") or {}

        def safe_num(value, default=0.0):
            return value if value is not None else default

        lines: list[str] = []
        lines.append(
            f"## 日期：{datetime.now().strftime('%Y-%m-%d')} {self.brief_label}\n"
        )
        lines.append(f"### {stock.name or stock.symbol}（{stock.symbol}）")

        quote = getattr(pack, "quote", None) if pack else None
        tech = (getattr(pack, "technical", None) if pack else None) or {}
        current_price = getattr(quote, "current_price", None) if quote else None
        if current_price is not None:
            lines.append(
                f"- 现价：{current_price:.2f}（{safe_num(getattr(quote, 'change_pct', 0)):+.2f}%）"
            )
        if getattr(quote, "prev_close", None) is not None:
            lines.append(f"- 昨收：{quote.prev_close:.2f}")
        if tech.get("volume_ratio") is not None:
            lines.append(f"- 量比：{tech['volume_ratio']:.2f}")
        if tech.get("trend"):
            lines.append(f"- 均线趋势：{tech['trend']}")
        support_m = tech.get("support_m")
        resistance_m = tech.get("resistance_m")
        if support_m is not None and resistance_m is not None:
            lines.append(f"- 支撑压力：中期支撑{support_m:.2f} / 中期压力{resistance_m:.2f}")
        elif tech.get("support") is not None and tech.get("resistance") is not None:
            lines.append(f"- 支撑压力：{tech['support']:.2f} / {tech['resistance']:.2f}")

        rs = symbol_ctx.get("relative_strength") or {}
        if rs.get("excess_5d") is not None:
            lines.append(
                f"- 板块/大盘参照：相对{rs.get('index_label', '指数')}5日超额{float(rs['excess_5d']):+.1f}%"
            )

        playbook_summary = data.get("playbook_summary")
        if playbook_summary:
            lines.append("\n### 方案档案摘要")
            lines.append(playbook_summary)

        # 持仓 + 近期流水（P3a v1.5：trades_text 紧凑文本）
        positions = context.portfolio.get_positions_for_stock(stock.symbol)
        aggregated = context.portfolio.get_aggregated_position(stock.symbol)
        if aggregated:
            style_labels = {"short": "短线", "swing": "波段", "long": "长线"}
            style = style_labels.get(aggregated.get("trading_style", "swing"), "波段")
            avg_cost = safe_num(aggregated.get("avg_cost"), 0.0)
            total_qty = aggregated.get("total_quantity", 0)
            lines.append("\n### 持仓")
            lines.append(f"- {total_qty}股 成本{avg_cost:.2f}（{style}）")
            if current_price is not None and avg_cost > 0:
                pnl_pct = (current_price - avg_cost) / avg_cost * 100
                pnl_amt = (current_price - avg_cost) * total_qty
                lines.append(f"- 浮盈速算：{pnl_pct:+.1f}%（{pnl_amt:+.0f}元）")
            for pos in positions:
                if pos.trades_text:
                    lines.append(f"- 近期流水：{pos.trades_text}")
        else:
            lines.append("\n### 持仓")
            lines.append("- 当前无持仓")

        lines.append(f"\n请按系统模板输出{self.brief_label}（≤180字）。")
        return system_prompt, "\n".join(lines)

    async def analyze(self, context: AgentContext, data: dict) -> AnalysisResult:
        """调用 AI 生成简报并做模板契约校验；校验不过则不推送、落日志。"""
        stock = data["stock"]
        system_prompt, user_content = self.build_prompt(data, context)
        content = await context.ai_client.chat(system_prompt, user_content)

        structured = try_extract_tagged_json(content) or {}
        display_content = strip_tagged_json(content)

        valid, reason, action_label = validate_brief_contract(
            display_content,
            structured,
            action_map=self.action_map,
            require_execution_detail=self.require_execution_detail,
        )
        if not valid:
            logger.error(
                "[%s] %s(%s) 简报契约校验失败(%s),本次不推送: %.200s",
                self.name,
                stock.name or stock.symbol,
                stock.symbol,
                reason,
                display_content,
            )
        elif len(display_content) > BRIEF_CHAR_SOFT_LIMIT:
            logger.warning(
                "[%s] %s(%s) 简报正文 %d 字,超出 %d 字模板预算",
                self.name,
                stock.name or stock.symbol,
                stock.symbol,
                len(display_content),
                BRIEF_CHAR_SOFT_LIMIT,
            )

        if context.model_label:
            display_content = display_content.rstrip() + f"\n\n---\nAI: {context.model_label}"

        title = f"【{self.display_name}】{stock.name or stock.symbol}({stock.symbol})"
        result = AnalysisResult(
            agent_name=self.name,
            title=title,
            content=display_content,
            raw_data={
                "timestamp": data.get("timestamp"),
                "symbol": stock.symbol,
                "structured": structured,
                "contract_valid": valid,
                "contract_reason": reason,
                "action_label": action_label,
                "prompt_context": user_content[:8000],
            },
        )

        saved = save_analysis(
            agent_name=self.name,
            stock_symbol=stock.symbol,
            content=result.content,
            title=title,
            raw_data=result.raw_data,
        )
        if not saved:
            logger.error(
                "[%s] %s(%s) 简报保存历史记录失败",
                self.name,
                stock.name or stock.symbol,
                stock.symbol,
            )
        return result

    async def should_notify(self, result: AnalysisResult) -> bool:
        """契约校验不过则不推送（已落日志）。"""
        return bool(result.raw_data.get("contract_valid"))


class MorningBriefAgent(BasePlaybookBriefAgent):
    """早盘简报 Agent（10:00 定势：缺口方向、量能、与触发位距离）"""

    name = "morning_brief"
    display_name = "早盘简报"
    description = "开盘半小时后定势：行情/位置/三选一结论/持仓浮盈（仅有方案档案的股票）"

    action_map = MORNING_ACTION_MAP
    require_execution_detail = False
    brief_label = "早盘简报"
    prompt_path = PROMPT_PATH
