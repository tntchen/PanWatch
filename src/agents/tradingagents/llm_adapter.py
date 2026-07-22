"""桥接 PanWatch AIClient 配置 → TradingAgents LLM config。

TradingAgents 通过 langchain-openai / langchain-anthropic 等驱动 LLM,
读取 config 字典 + 环境变量(`OPENAI_API_KEY`/`DEEPSEEK_API_KEY` 等)。
本模块把 PanWatch 的 AIClient 配置桥接过去。
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from src.core.ai_client import AIClient

logger = logging.getLogger(__name__)


# TradingAgents selected_analysts 字段的合法值(见上游 graph/trading_graph.py)
VALID_ANALYSTS = {"market", "social", "news", "fundamentals"}


# ---------------------------------------------------------------------------
# 按调用传 API key（风险 #20：禁止往进程 env 注 key）
# ---------------------------------------------------------------------------

_ta_api_key: ContextVar[str | None] = ContextVar("panwatch_ta_api_key", default=None)


@contextmanager
def ta_api_key_context(api_key: str | None) -> Iterator[None]:
    """把本次调用的 API key 放入 ContextVar（不写进程 env）。

    多租户并发场景下，进程级 env 注 key 会被后跑租户覆盖/泄漏给子进程；
    ContextVar 随调用隔离，TradingAgentsGraph 构造 LLM client 时经
    apply_ta_api_key_patch() 的补丁从此处取 key。
    """
    token = _ta_api_key.set(api_key or None)
    try:
        yield
    finally:
        _ta_api_key.reset(token)


_PATCH_APPLIED = False


def apply_ta_api_key_patch() -> bool:
    """幂等 patch TradingAgents：LLM client 从 ContextVar 取 key，不读进程 env。

    两处手术（上游 tradingagents 行为）：
    1. 包装 ``TradingAgentsGraph._get_provider_kwargs``——把 ContextVar 中的
       key 以 ``api_key`` kwarg 传给 create_llm_client；
       ``OpenAIClient._PASSTHROUGH_KWARGS`` 含 api_key，会透传到
       ``ChatOpenAI(api_key=...)``（anthropic/google client 同样支持）。
    2. openrouter ProviderSpec 改 key_optional——上游 get_llm() 在 env 缺失
       且非 key_optional 时会**先于透传** raise ValueError；改为 placeholder
       后由透传覆盖为真实 key（ctx 缺 key 时以占位 key 发请求，上游 401
       可见失败，不会静默用错别的租户的 key）。
    """
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return True
    try:
        from dataclasses import replace

        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.llm_clients.openai_client import (
            OPENAI_COMPATIBLE_PROVIDERS,
        )
    except ImportError as e:
        logger.warning("[TA] api_key patch 未应用（tradingagents 不可用）: %s", e)
        return False

    orig_get_provider_kwargs = TradingAgentsGraph._get_provider_kwargs

    def _patched_get_provider_kwargs(self) -> dict:
        kwargs = orig_get_provider_kwargs(self)
        key = _ta_api_key.get()
        if key:
            kwargs["api_key"] = key
        return kwargs

    TradingAgentsGraph._get_provider_kwargs = _patched_get_provider_kwargs

    spec = OPENAI_COMPATIBLE_PROVIDERS.get("openrouter")
    if spec is not None and not spec.key_optional:
        OPENAI_COMPATIBLE_PROVIDERS["openrouter"] = replace(spec, key_optional=True)

    _PATCH_APPLIED = True
    return True


def build_ta_llm_config(
    ai_client: AIClient,
    *,
    debate_rounds: int = 1,
    selected_analysts: list[str] | None = None,
    output_language: str = "Chinese",
    deep_model: str | None = None,
    quick_model: str | None = None,
) -> dict[str, Any]:
    """生成 TradingAgents 期望的 config dict。

    继承 tradingagents.default_config.DEFAULT_CONFIG (含 data_cache_dir / project_dir /
    memory_log_path 等必需字段),再覆盖 PanWatch 配置:
    - llm_provider: 统一走 openrouter 兼容协议(走 chat completions,避开 OpenAI Responses API)
    - backend_url: PanWatch AI 服务的 base_url
    - deep_think_llm: 推理/辩论/风控/PM 用的"强模型"。默认走 ai_client.model;
      可由 deep_model 参数覆盖,允许辩论用 claude-sonnet / o3 这种贵但准的模型
    - quick_think_llm: 分析师工具调用用的"快模型"。默认 deep_model;
      可由 quick_model 参数覆盖,允许分析师用 haiku / gpt-4o-mini 等便宜模型
    - max_debate_rounds: 辩论轮次
    - selected_analysts: ["market", "social", "news", "fundamentals"]
    - output_language: "Chinese" / "English"

    注意:TA 上游 deep + quick 共用 backend_url,所以两个模型必须在**同一个 endpoint** 后面。
    要混 Claude + GPT 推荐 LiteLLM proxy 把多 provider 聚合到一个 endpoint。
    """
    analysts = list(selected_analysts or VALID_ANALYSTS)
    invalid = [a for a in analysts if a not in VALID_ANALYSTS]
    if invalid:
        raise ValueError(
            f"非法 analyst 名: {invalid}; 合法值: {sorted(VALID_ANALYSTS)}"
        )

    # 继承上游默认 config(含 data_cache_dir / project_dir / memory_log_path 等),
    # 否则 TradingAgentsGraph.__init__ 用 os.makedirs(config["data_cache_dir"]) 会 KeyError。
    try:
        from tradingagents.default_config import DEFAULT_CONFIG as _UPSTREAM_DEFAULT
        config = dict(_UPSTREAM_DEFAULT)
    except ImportError:
        config = {}

    # PanWatch 覆盖。
    # ⚠️ llm_provider 故意不用 "openai":TA 检测到 openai 会强制开 use_responses_api=True
    # (OpenAI Responses API,/v1/responses 端点),硅基流动/智谱/Ollama 等第三方 OpenAI 兼容
    # 服务不支持这个端点,会 404。
    # 用 "openrouter" 走标准 chat completions (/v1/chat/completions),同时 backend_url
    # 覆盖默认 openrouter 端点为 PanWatch 配置的真实 base_url。
    # 双模型解析:
    # - deep_model 未指定 → 用 ai_client.model
    # - quick_model 未指定 → 用 deep_model(单模型场景退化)
    deep_llm = (deep_model or ai_client.model or "").strip() or ai_client.model
    quick_llm = (quick_model or deep_llm or "").strip() or deep_llm

    config.update({
        "llm_provider": "openrouter",
        "backend_url": ai_client.base_url,
        "deep_think_llm": deep_llm,
        "quick_think_llm": quick_llm,
        "max_debate_rounds": max(1, int(debate_rounds)),
        "max_risk_discuss_rounds": 1,
        "selected_analysts": analysts,
        "output_language": output_language,
        "online_tools": True,
        "checkpoint_enabled": False,  # 避免 sqlite checkpoint 文件污染
    })
    return config


def inject_api_key_env(ai_client: AIClient) -> None:
    """【已废弃，仅保留兼容】把 PanWatch AI 服务的 API key 注入到环境变量。

    ⚠️ 风险 #20：进程级 env 注 key 在多租户并发下会被后跑租户覆盖、并泄漏
    给子进程。生产链路（agent.py）已改走 ta_api_key_context +
    apply_ta_api_key_patch 按调用传 key，不再调用本函数。
    """
    if not ai_client.api_key:
        logger.warning("[TA] AIClient 没有 api_key,TradingAgents LLM 调用大概率失败")
        return
    # 覆盖多个候选 env var,让 TA 不管走哪条 provider 分支都能取到 key
    os.environ["OPENROUTER_API_KEY"] = ai_client.api_key
    os.environ["OPENAI_API_KEY"] = ai_client.api_key
    os.environ["DEEPSEEK_API_KEY"] = ai_client.api_key
