import logging
import time
from typing import Callable, Awaitable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.agents.base import BaseAgent, AgentContext
from src.collectors.kline_collector import kline_source
from src.core.agent_runs import record_agent_run
from src.core.log_context import log_context
from src.models.market import MARKETS
from src.core.schedule_parser import parse_schedule
from src.web.tenant_context import (
    DEFAULT_TENANT_ID,
    single_tenant_mode,
    tenant_scope,
)

logger = logging.getLogger(__name__)


class AgentScheduler:
    """Agent 调度器"""

    def __init__(self, timezone: str = "UTC"):
        self.scheduler = AsyncIOScheduler()
        self.agents: dict[str, BaseAgent] = {}
        self.execution_modes: dict[str, str] = {}
        self.timezone = timezone
        # 改为存储 context 构建函数，而非固定 context
        # MT-P3（docs/23 P2）：签名前置重构为 (tenant_id, agent_name)，
        # 每次调度扇出按租户逐租户调用。
        self.context_builder: Callable[[int, str], AgentContext] | None = None

    def set_context_builder(self, builder: Callable[[int, str], AgentContext]):
        """设置 context 构建函数（每次执行时动态构建，首参 tenant_id）"""
        self.context_builder = builder

    def register(self, agent: BaseAgent, schedule: str, execution_mode: str = "batch"):
        """
        注册 Agent 到调度器。

        Args:
            agent: Agent 实例
            schedule: 调度表达式
                - cron 格式: "分 时 日 月 周" (5 部分)
                - interval 格式: "interval:3m" 或 "interval:30s"
            execution_mode: 执行模式 batch/single（single 将逐只股票执行 run_single）
        """
        self.agents[agent.name] = agent
        self.execution_modes[agent.name] = execution_mode or "batch"

        # 解析调度表达式
        # cron 使用 5 段: "分 时 日 月 周"
        # 其中 day_of_week 的数字按 POSIX cron 语义(1-5=周一到周五)，会在内部做一次归一化。
        trigger = parse_schedule(schedule, timezone=self.timezone)

        self.scheduler.add_job(
            self._run_agent,
            trigger=trigger,
            args=[agent.name],
            id=agent.name,
            name=agent.display_name,
            replace_existing=True,
        )

        logger.info(f"注册 Agent: {agent.display_name} (schedule: {schedule})")

    # NOTE: cron/interval 解析逻辑统一放在 src/core/schedule_parser.py

    # ------------------------------------------------------------------
    # MT-P3 调度扇出（docs/23 §2，T17 硬约束：单 job 内串行遍历租户，
    # 严禁 per-tenant 注册新 job；job id 保持 agent.name 原样）
    # ------------------------------------------------------------------

    def _list_fanout_tenant_ids(self) -> list[int]:
        """本次调度扇出的租户清单（id 升序，确定性顺序）。

        单租户直通模式（PANWATCH_SINGLE_TENANT='1'，默认）只跑默认租户 1，
        行为与单用户时代完全等价；多租户模式取「active 且含 is_active 用户」的租户。
        """
        if single_tenant_mode():
            return [DEFAULT_TENANT_ID]
        from src.web.database import SessionLocal
        from src.web.models import Tenant, User

        db = SessionLocal()
        try:
            rows = (
                db.query(Tenant.id)
                .filter(Tenant.status == "active")
                .filter(
                    db.query(User.id)
                    .filter(
                        User.tenant_id == Tenant.id,
                        User.is_active == True,  # noqa: E712
                    )
                    .exists()
                )
                .order_by(Tenant.id)
                .all()
            )
            tenant_ids = [r[0] for r in rows]
            return tenant_ids or [DEFAULT_TENANT_ID]
        except Exception as e:
            logger.error(f"获取调度扇出租户清单失败，回退默认租户: {e}", exc_info=True)
            return [DEFAULT_TENANT_ID]
        finally:
            db.close()

    @staticmethod
    def _agent_enabled_for_tenant(agent_name: str, tenant_id: int) -> bool:
        """T4/J6：租户 override enabled=False 时扇出跳过该租户（schedule 恒取模板）。

        单租户直通模式不查 override 表（机制点全直通，与现状等价）。
        """
        if single_tenant_mode():
            return True
        from src.web.database import SessionLocal
        from src.web.models import AgentConfigOverride

        db = SessionLocal()
        try:
            row = (
                db.query(AgentConfigOverride)
                .filter(
                    AgentConfigOverride.tenant_id == tenant_id,
                    AgentConfigOverride.agent_name == agent_name,
                )
                .first()
            )
            return not (row is not None and row.enabled is False)
        except Exception as e:
            # fail-open：override 查询失败不阻断该租户本次执行
            logger.warning(
                f"查询租户 Agent override 失败 tenant={tenant_id} agent={agent_name}: {e}"
            )
            return True
        finally:
            db.close()

    async def _run_agent(self, agent_name: str, tenant_id: int | None = None):
        """执行指定 Agent（按租户串行扇出，单租户异常隔离不影响其余租户）。

        tenant_id=None（调度 job 默认）：遍历全部应执行租户；
        指定 tenant_id：只跑该租户（trigger_now 单租户手动触发）。
        串行不并行（T1 N<=5，避免 SQLite 写竞争，docs/23 §2.3）。
        """
        if not self.context_builder:
            logger.error("context_builder 未设置")
            return

        agent = self.agents.get(agent_name)
        if not agent:
            logger.error(f"Agent 未找到: {agent_name}")
            return

        tenant_ids = (
            [tenant_id] if tenant_id is not None else self._list_fanout_tenant_ids()
        )
        for tid in tenant_ids:
            if not self._agent_enabled_for_tenant(agent_name, tid):
                logger.info(
                    f"[调度] 租户 {tid} 已禁用 Agent {agent_name}（override），跳过"
                )
                continue
            await self._run_agent_for_tenant(agent, agent_name, tid)

    async def _run_agent_for_tenant(
        self, agent: BaseAgent, agent_name: str, tenant_id: int
    ) -> None:
        """单租户粒度执行 + 运行记录（原 _run_agent 函数体下沉，逐租户容错）。

        tenant_scope 内所有 ORM 查询（含旁路自建 Session）自动按租户过滤（C7/P22）。
        """
        assert self.context_builder is not None
        start = time.monotonic()
        trace_id = f"sch-{agent_name}-t{tenant_id}-{int(time.time() * 1000)}"
        try:
            with tenant_scope(tenant_id):
                with log_context(
                    trace_id=trace_id,
                    run_id=trace_id,
                    agent_name=agent_name,
                    event="agent_run",
                    tags={"trigger_source": "schedule", "tenant_id": str(tenant_id)},
                ):
                    # 每次执行时动态构建 context（获取最新配置，P2/P3 tenant 前置）
                    context = self.context_builder(tenant_id, agent_name)
                    logger.info(
                        f"[调度] 开始执行 Agent: {agent.display_name}（租户 {tenant_id}）"
                    )
                    mode = self.execution_modes.get(agent_name, "batch")
                    if mode == "single" and hasattr(agent, "run_single"):
                        processed = 0
                        skipped = 0
                        errors: list[str] = []
                        for stock in list(context.watchlist):
                            market_def = MARKETS.get(stock.market)
                            if market_def and not market_def.is_trading_time():
                                skipped += 1
                                logger.info(
                                    f"[调度] 跳过 {agent.display_name} {stock.symbol}（{market_def.name} 非交易时段）"
                                )
                                continue
                            try:
                                with kline_source(f"agent:{agent_name}"):
                                    res = await agent.run_single(context, stock.symbol)  # type: ignore[attr-defined]
                                processed += 1
                                try:
                                    notify_error = (
                                        (res.raw_data or {}).get("notify_error")
                                        if res
                                        else ""
                                    )
                                except Exception:
                                    notify_error = ""
                                if notify_error:
                                    errors.append(f"{stock.symbol} notify: {notify_error}")
                            except Exception as e:
                                logger.error(
                                    f"Agent [{agent_name}] 单只执行失败 {stock.symbol}: {e}",
                                    exc_info=True,
                                )
                                errors.append(f"{stock.symbol}: {e}")
                        logger.info(
                            f"[调度] Agent 单只模式执行完成: {agent.display_name}（执行{processed}，跳过{skipped}，共{len(context.watchlist)}）"
                        )
                        duration_ms = int((time.monotonic() - start) * 1000)
                        record_agent_run(
                            agent_name=agent_name,
                            status="failed" if errors else "success",
                            result=f"single mode executed {processed}, skipped {skipped}, total {len(context.watchlist)}",
                            error="; ".join(errors),
                            duration_ms=duration_ms,
                            trace_id=trace_id,
                            trigger_source="schedule",
                            model_label=context.model_label,
                        )
                    else:
                        with kline_source(f"agent:{agent_name}"):
                            result = await agent.run(context)
                        duration_ms = int((time.monotonic() - start) * 1000)
                        notify_error = ""
                        try:
                            notify_error = (result.raw_data or {}).get("notify_error") or ""
                        except Exception:
                            notify_error = ""
                        raw = result.raw_data or {}
                        record_agent_run(
                            agent_name=agent_name,
                            status="failed" if notify_error else "success",
                            result=(result.content or "")[:2000],
                            error=(notify_error or "")[:2000],
                            duration_ms=duration_ms,
                            trace_id=trace_id,
                            trigger_source="schedule",
                            notify_attempted=(
                                "notified" in raw
                                or "notify_error" in raw
                                or "notify_skipped" in raw
                            ),
                            notify_sent=bool(raw.get("notified", False)),
                            model_label=context.model_label,
                        )
                    logger.info(f"[调度] Agent 执行完成: {agent.display_name}（租户 {tenant_id}）")
        except Exception as e:
            # 租户粒度容错：单租户异常仅记录，不影响扇出中的其余租户
            logger.error(
                f"Agent [{agent_name}] 调度执行异常（租户 {tenant_id}）: {e}",
                exc_info=True,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            record_agent_run(
                agent_name=agent_name,
                status="failed",
                error=str(e),
                duration_ms=duration_ms,
                trace_id=trace_id,
                trigger_source="schedule",
            )

    async def trigger_now(self, agent_name: str, tenant_id: int | None = None):
        """立即执行某个 Agent（手动触发）。

        tenant_id=None：全租户扇出（保留运维「全量立即跑」能力）；
        指定 tenant_id：仅该租户执行。
        """
        await self._run_agent(agent_name, tenant_id=tenant_id)

    def start(self):
        """启动调度器"""
        self.scheduler.start()
        from src.core.scheduler_registry import register
        register("agent", self.scheduler)
        logger.info(f"调度器已启动，已注册 {len(self.agents)} 个 Agent")

        # 打印所有已注册的任务
        jobs = self.scheduler.get_jobs()
        for job in jobs:
            logger.info(f"  - {job.name}: 下次执行 {job.next_run_time}")

    def shutdown(self):
        """关闭调度器"""
        self.scheduler.shutdown()
        logger.info("调度器已关闭")
