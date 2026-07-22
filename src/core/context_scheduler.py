"""上下文维护调度器：后验评估 + 过期数据清理 + 机会自动刷新。

MT-P3（docs/23 §2.2 / docs/24 §6，T17/T19）：
- market_regime / market_scan / strategy 链保持市场级全局单份，不扇出、
  不引入 tenant 维度（entry_candidates / strategy_signal_runs 走 tenant=0 哨兵）；
- 私有表维护（agent_prediction_outcomes 后验评估 + 私有快照清理）在
  多租户模式下于单 job 内遍历活跃租户逐租户执行（tenant_scope 显式入口），
  job id 原样保留，严禁 per-tenant 注册新 job；
- 单租户直通模式（PANWATCH_SINGLE_TENANT='1'）走原单次路径，行为与现状等价。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.collectors.kline_collector import kline_source
from src.core.context_store import cleanup_context_data
from src.core.entry_candidates import evaluate_entry_candidate_outcomes
from src.core.prediction_outcome import evaluate_pending_prediction_outcomes
from src.core.strategy_engine import (
    evaluate_strategy_outcomes,
    rebalance_strategy_weights,
    refresh_strategy_signals,
)
from src.web.database import SessionLocal
from src.web.models import Tenant
from src.web.tenant_context import (
    DEFAULT_TENANT_ID,
    single_tenant_mode,
    tenant_scope,
)

logger = logging.getLogger(__name__)


def _fanout_tenant_ids() -> list[int]:
    """多租户模式返回活跃租户 id（ORDER BY id 确定性顺序）。

    单租户直通模式返回空表 → 调用方走原单次路径（行为等价保障）；
    枚举失败回退默认租户，绝不让维护任务整体失效。
    """
    if single_tenant_mode():
        return []
    try:
        db = SessionLocal()
        try:
            rows = (
                db.query(Tenant.id)
                .filter(Tenant.status == "active")
                .order_by(Tenant.id)
                .all()
            )
            ids = [int(r[0]) for r in rows]
            return ids or [DEFAULT_TENANT_ID]
        finally:
            db.close()
    except Exception as e:
        logger.warning("[上下文维护] 租户枚举失败，回退默认租户: %s", e)
        return [DEFAULT_TENANT_ID]


def _merge_numeric_stats(total: dict, part: dict) -> dict:
    """逐租户聚合数值型统计字段（后验评估 stats 全为 int 计数）。"""
    for key, value in (part or {}).items():
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + value
    return total


class ContextMaintenanceScheduler:
    def __init__(
        self,
        timezone: str = "UTC",
        eval_interval_hours: int = 6,
        snapshot_retention_days: int = 180,
        outcome_retention_days: int = 365,
    ):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.eval_interval_hours = max(1, int(eval_interval_hours))
        self.snapshot_retention_days = max(30, int(snapshot_retention_days))
        self.outcome_retention_days = max(60, int(outcome_retention_days))
        self._evaluating = False
        self._cleaning = False
        self._refreshing = False

    async def _evaluate_agent_predictions(self) -> dict:
        """agent_prediction_outcomes 后验评估（J5 判私有：租户级）。

        单租户直通：原单次调用（无 ctx、无显式过滤，行为与现状等价）；
        多租户：单 job 内遍历活跃租户，tenant_scope 内逐租户评估，单租户
        失败不阻断后续租户。
        """
        tenant_ids = _fanout_tenant_ids()
        if not tenant_ids:
            return await asyncio.to_thread(evaluate_pending_prediction_outcomes)
        stats: dict = {}
        for tid in tenant_ids:
            try:
                with tenant_scope(tid):
                    part = await asyncio.to_thread(evaluate_pending_prediction_outcomes)
                _merge_numeric_stats(stats, part)
            except Exception as e:
                logger.exception("[上下文维护] 租户 %s 后验评估异常: %s", tid, e)
                continue
        return stats

    async def _evaluate_job(self):
        if self._evaluating:
            logger.debug("[上下文维护] 上一轮后验评估仍在执行，跳过本轮")
            return
        self._evaluating = True
        try:
            stats = await self._evaluate_agent_predictions()
            level = logging.INFO if stats.get("evaluated", 0) else logging.DEBUG
            logger.log(
                level,
                "[上下文维护] 后验评估完成: pending=%s eligible=%s evaluated=%s skipped_not_due=%s skipped_no_price=%s",
                stats.get("total_pending", 0),
                stats.get("eligible", 0),
                stats.get("evaluated", 0),
                stats.get("skipped_not_due", 0),
                stats.get("skipped_no_price", 0),
            )
            with kline_source("outcome_eval"):
                cand_stats = await asyncio.to_thread(
                    evaluate_entry_candidate_outcomes,
                    horizons=(1, 3, 5, 10),
                    snapshot_days=45,
                    limit=500,
                )
            level = logging.INFO if cand_stats.get("evaluated", 0) else logging.DEBUG
            logger.log(
                level,
                "[上下文维护] 候选后验评估完成: total=%s eligible=%s evaluated=%s skipped_not_due=%s skipped_no_price=%s",
                cand_stats.get("total_candidates", 0),
                cand_stats.get("eligible", 0),
                cand_stats.get("evaluated", 0),
                cand_stats.get("skipped_not_due", 0),
                cand_stats.get("skipped_no_price", 0),
            )
            with kline_source("outcome_eval"):
                strategy_stats = await asyncio.to_thread(
                    evaluate_strategy_outcomes,
                    horizons=(1, 3, 5, 10),
                    snapshot_days=60,
                    limit=1200,
                )
            level = logging.INFO if strategy_stats.get("evaluated", 0) else logging.DEBUG
            logger.log(
                level,
                "[上下文维护] 策略后验评估完成: total=%s eligible=%s evaluated=%s skipped_not_due=%s skipped_no_price=%s",
                strategy_stats.get("total_signals", 0),
                strategy_stats.get("eligible", 0),
                strategy_stats.get("evaluated", 0),
                strategy_stats.get("skipped_not_due", 0),
                strategy_stats.get("skipped_no_price", 0),
            )
            rebalance = await asyncio.to_thread(
                rebalance_strategy_weights,
                window_days=45,
                min_samples=8,
                alpha=0.35,
                regime="default",
            )
            level = logging.INFO if rebalance.get("changed", 0) else logging.DEBUG
            logger.log(
                level,
                "[上下文维护] 策略调权完成: changed=%s checked=%s skipped_low_sample=%s",
                rebalance.get("changed", 0),
                rebalance.get("checked", 0),
                rebalance.get("skipped_low_sample", 0),
            )

            # Phase 4 → 因子自校准闭环:把 IC/IR 接进每因子权重的轻量标定
            # (calibrate_all_markets 内部按市场算 IC 并据此调权,不再只是记录)。
            try:
                from src.core.factor_calibration import calibrate_all_markets

                fcal = await asyncio.to_thread(calibrate_all_markets)
                changed = sum(r.get("changed", 0) for r in fcal.values())
                logger.log(
                    logging.INFO if changed else logging.DEBUG,
                    "[上下文维护] 因子自校准完成: changed=%s detail=%s",
                    changed,
                    {m: r.get("changed", 0) for m, r in fcal.items()},
                )
            except Exception as fc_err:
                logger.debug("[上下文维护] 因子自校准跳过: %s", fc_err)
        except Exception as e:
            logger.exception(f"[上下文维护] 后验评估异常: {e}")
        finally:
            self._evaluating = False

    async def _cleanup_context_data(self) -> dict:
        """私有快照/运行/后验记录清理。

        单租户直通：原全局单次调用（tenant_id=None 不按租户过滤，行为等价）；
        多租户：单 job 内遍历活跃租户逐租户清理（docs/25：禁一次删全表），
        聚合各租户删除计数。
        """
        kwargs = {
            "snapshot_days": self.snapshot_retention_days,
            "topic_days": self.snapshot_retention_days,
            "context_run_days": self.snapshot_retention_days,
            "outcome_days": self.outcome_retention_days,
        }
        tenant_ids = _fanout_tenant_ids()
        if not tenant_ids:
            return await asyncio.to_thread(cleanup_context_data, **kwargs)
        deleted: dict = {}
        for tid in tenant_ids:
            try:
                with tenant_scope(tid):
                    part = await asyncio.to_thread(
                        cleanup_context_data, tenant_id=tid, **kwargs
                    )
                _merge_numeric_stats(deleted, part)
            except Exception as e:
                logger.exception("[上下文维护] 租户 %s 清理异常: %s", tid, e)
                continue
        return deleted

    async def _cleanup_job(self):
        if self._cleaning:
            logger.debug("[上下文维护] 上一轮清理仍在执行，跳过本轮")
            return
        self._cleaning = True
        try:
            deleted = await self._cleanup_context_data()
            # deleted 是 dict,任一字段 >0 就是有清理动作
            has_work = bool(deleted and any(deleted.values()) if isinstance(deleted, dict) else deleted)
            level = logging.INFO if has_work else logging.DEBUG
            logger.log(level, "[上下文维护] 清理完成: %s", deleted)
        except Exception as e:
            logger.exception(f"[上下文维护] 清理异常: {e}")
        finally:
            self._cleaning = False

    async def evaluate_once(self) -> dict:
        agent_task = asyncio.to_thread(evaluate_pending_prediction_outcomes)
        candidate_task = asyncio.to_thread(
            evaluate_entry_candidate_outcomes,
            horizons=(1, 3, 5, 10),
            snapshot_days=45,
            limit=500,
        )
        strategy_eval_task = asyncio.to_thread(
            evaluate_strategy_outcomes,
            horizons=(1, 3, 5, 10),
            snapshot_days=60,
            limit=1200,
        )
        strategy_rebalance_task = asyncio.to_thread(
            rebalance_strategy_weights,
            window_days=45,
            min_samples=8,
            alpha=0.35,
            regime="default",
        )
        agent_stats, candidate_stats, strategy_eval_stats, strategy_rebalance_stats = await asyncio.gather(
            agent_task,
            candidate_task,
            strategy_eval_task,
            strategy_rebalance_task,
        )
        # 因子自校准:须在 outcome 评估之后(IC 才新鲜),不能并进上面的 gather。
        from src.core.factor_calibration import calibrate_all_markets

        factor_calibration_stats = await asyncio.to_thread(calibrate_all_markets)
        return {
            "agent_predictions": agent_stats,
            "entry_candidates": candidate_stats,
            "strategy_outcomes": strategy_eval_stats,
            "strategy_rebalance": strategy_rebalance_stats,
            "factor_calibration": factor_calibration_stats,
        }

    async def _refresh_opportunities_job(self):
        """定时刷新机会池（候选 + 策略信号）。"""
        if self._refreshing:
            logger.debug("[上下文维护] 上一轮机会刷新仍在执行，跳过本轮")
            return
        self._refreshing = True
        try:
            with kline_source("refresh_opportunities"):
                result = await asyncio.to_thread(
                    refresh_strategy_signals,
                    rebuild_candidates=True,
                    max_inputs=500,
                    market_scan_limit=80,
                    max_kline_symbols=60,
                    limit_candidates=2000,
                )
            level = logging.INFO if result.get("count", 0) else logging.DEBUG
            logger.log(
                level,
                "[上下文维护] 机会自动刷新完成: snapshot_date=%s count=%s",
                result.get("snapshot_date", ""),
                result.get("count", 0),
            )
        except Exception as e:
            logger.exception(f"[上下文维护] 机会自动刷新异常: {e}")
        finally:
            self._refreshing = False

    async def refresh_opportunities_once(self) -> dict:
        """手动触发一次机会刷新。"""
        with kline_source("refresh_opportunities"):
            return await asyncio.to_thread(
                refresh_strategy_signals,
                rebuild_candidates=True,
                max_inputs=500,
                market_scan_limit=80,
                max_kline_symbols=60,
                limit_candidates=2000,
            )

    async def cleanup_once(self) -> dict:
        return await asyncio.to_thread(
            cleanup_context_data,
            snapshot_days=self.snapshot_retention_days,
            topic_days=self.snapshot_retention_days,
            context_run_days=self.snapshot_retention_days,
            outcome_days=self.outcome_retention_days,
        )

    def start(self):
        self.scheduler.add_job(
            self._evaluate_job,
            "interval",
            hours=self.eval_interval_hours,
            jitter=120,  # 错峰,避免与 price_alert/paper_trading(60s)同刻写 SQLite
            id="context_maintenance_evaluate",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._cleanup_job,
            "cron",
            hour=4,
            minute=15,
            jitter=120,
            id="context_maintenance_cleanup",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        # 机会自动刷新（北京时间 09:15 / 13:30 / 22:00）
        for job_hour, job_minute in ((1, 15), (5, 30), (14, 0)):
            self.scheduler.add_job(
                self._refresh_opportunities_job,
                "cron",
                hour=job_hour,
                minute=job_minute,
                jitter=120,  # 错峰,避免与其它调度同刻写 SQLite
                id=f"context_maintenance_refresh_opportunities_{job_hour:02d}{job_minute:02d}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
        # Run a bootstrap evaluation shortly after startup to warm up outcome stats.
        self.scheduler.add_job(
            self._evaluate_job,
            "date",
            run_date=datetime.now(self.scheduler.timezone) + timedelta(seconds=15),
            id="context_maintenance_bootstrap_evaluate",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()
        from src.core.scheduler_registry import register
        register("context", self.scheduler)
        logger.info(
            "上下文维护调度器已启动（后验评估间隔 %sh，启动补跑 +15s，快照保留 %s 天，后验保留 %s 天，机会自动刷新 01:15/05:30/14:00 UTC）",
            self.eval_interval_hours,
            self.snapshot_retention_days,
            self.outcome_retention_days,
        )

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("上下文维护调度器已关闭")
