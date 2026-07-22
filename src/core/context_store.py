from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import and_

from src.web.database import SessionLocal
from src.web.models import (
    AgentContextRun,
    AgentPredictionOutcome,
    NewsTopicSnapshot,
    StockContextSnapshot,
)
from src.web.tenant_context import DEFAULT_TENANT_ID, current_tenant
from src.core.json_safe import to_jsonable

logger = logging.getLogger(__name__)


def _resolve_tenant_id(tenant_id: int | None) -> int:
    """租户解析顺序（docs/25 §F12）：显式参数 → tenant_scope ctx → 默认租户 1。

    单租户直通模式恒落 1（全量存量行 tenant_id=1），显式过滤行为等价。
    """
    if tenant_id is not None:
        return int(tenant_id)
    ctx = current_tenant()
    if ctx is not None:
        return int(ctx.tenant_id)
    return DEFAULT_TENANT_ID


def save_stock_context_snapshot(
    *,
    symbol: str,
    market: str,
    snapshot_date: str,
    context_type: str,
    payload: dict,
    quality: dict | None = None,
    tenant_id: int | None = None,
) -> bool:
    tid = _resolve_tenant_id(tenant_id)
    db = SessionLocal()
    try:
        payload_safe = to_jsonable(payload or {})
        quality_safe = to_jsonable(quality or {})
        existing = (
            db.query(StockContextSnapshot)
            .filter(
                StockContextSnapshot.tenant_id == tid,
                StockContextSnapshot.symbol == symbol,
                StockContextSnapshot.market == market,
                StockContextSnapshot.snapshot_date == snapshot_date,
                StockContextSnapshot.context_type == context_type,
            )
            .first()
        )
        if existing:
            existing.payload = payload_safe
            existing.quality = quality_safe
        else:
            db.add(
                StockContextSnapshot(
                    tenant_id=tid,
                    symbol=symbol,
                    market=market,
                    snapshot_date=snapshot_date,
                    context_type=context_type,
                    payload=payload_safe,
                    quality=quality_safe,
                )
            )
        db.commit()
        return True
    except Exception as e:
        logger.warning(f"保存 stock context snapshot 失败: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def get_recent_stock_context_snapshots(
    *,
    symbol: str,
    market: str,
    context_type: str | None = None,
    days: int = 30,
    limit: int = 30,
    tenant_id: int | None = None,
) -> list[StockContextSnapshot]:
    tid = _resolve_tenant_id(tenant_id)
    db = SessionLocal()
    try:
        cutoff = (date.today() - timedelta(days=max(days, 1))).strftime("%Y-%m-%d")
        q = db.query(StockContextSnapshot).filter(
            StockContextSnapshot.tenant_id == tid,
            StockContextSnapshot.symbol == symbol,
            StockContextSnapshot.market == market,
            StockContextSnapshot.snapshot_date >= cutoff,
        )
        if context_type:
            q = q.filter(StockContextSnapshot.context_type == context_type)
        return (
            q.order_by(StockContextSnapshot.snapshot_date.desc())
            .limit(max(1, limit))
            .all()
        )
    finally:
        db.close()


def save_news_topic_snapshot(
    *,
    snapshot_date: str,
    window_days: int,
    symbols: list[str],
    summary: str,
    topics: list[str],
    sentiment: str,
    coverage: dict | None = None,
    tenant_id: int | None = None,
) -> bool:
    tid = _resolve_tenant_id(tenant_id)
    db = SessionLocal()
    try:
        existing = (
            db.query(NewsTopicSnapshot)
            .filter(
                NewsTopicSnapshot.tenant_id == tid,
                NewsTopicSnapshot.snapshot_date == snapshot_date,
                NewsTopicSnapshot.window_days == window_days,
            )
            .first()
        )
        payload = to_jsonable(
            {
                "symbols": symbols or [],
                "summary": summary or "",
                "topics": topics or [],
                "sentiment": sentiment or "neutral",
                "coverage": coverage or {},
            }
        )
        if existing:
            existing.symbols = payload["symbols"]
            existing.summary = payload["summary"]
            existing.topics = payload["topics"]
            existing.sentiment = payload["sentiment"]
            existing.coverage = payload["coverage"]
        else:
            db.add(
                NewsTopicSnapshot(
                    tenant_id=tid,
                    snapshot_date=snapshot_date,
                    window_days=int(window_days),
                    symbols=payload["symbols"],
                    summary=payload["summary"],
                    topics=payload["topics"],
                    sentiment=payload["sentiment"],
                    coverage=payload["coverage"],
                )
            )
        db.commit()
        return True
    except Exception as e:
        logger.warning(f"保存 news topic snapshot 失败: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def get_latest_news_topic_snapshot(
    *,
    window_days: int = 7,
    tenant_id: int | None = None,
) -> NewsTopicSnapshot | None:
    tid = _resolve_tenant_id(tenant_id)
    db = SessionLocal()
    try:
        return (
            db.query(NewsTopicSnapshot)
            .filter(
                NewsTopicSnapshot.tenant_id == tid,
                NewsTopicSnapshot.window_days == int(window_days),
            )
            .order_by(NewsTopicSnapshot.snapshot_date.desc())
            .first()
        )
    finally:
        db.close()


def save_agent_context_run(
    *,
    agent_name: str,
    stock_symbol: str,
    analysis_date: str,
    context_payload: dict,
    quality: dict | None = None,
    tenant_id: int | None = None,
) -> bool:
    tid = _resolve_tenant_id(tenant_id)
    db = SessionLocal()
    try:
        payload_safe = to_jsonable(context_payload or {})
        quality_safe = to_jsonable(quality or {})
        db.add(
            AgentContextRun(
                tenant_id=tid,
                agent_name=agent_name,
                stock_symbol=stock_symbol or "*",
                analysis_date=analysis_date,
                context_payload=payload_safe,
                quality=quality_safe,
            )
        )
        db.commit()
        return True
    except Exception as e:
        logger.warning(f"保存 agent context run 失败: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def list_recent_agent_context_runs(
    *,
    agent_name: str,
    stock_symbol: str | None = None,
    days: int = 30,
    limit: int = 50,
    tenant_id: int | None = None,
) -> list[AgentContextRun]:
    tid = _resolve_tenant_id(tenant_id)
    db = SessionLocal()
    try:
        cutoff = (date.today() - timedelta(days=max(days, 1))).strftime("%Y-%m-%d")
        q = db.query(AgentContextRun).filter(
            AgentContextRun.tenant_id == tid,
            AgentContextRun.agent_name == agent_name,
            AgentContextRun.analysis_date >= cutoff,
        )
        if stock_symbol:
            q = q.filter(AgentContextRun.stock_symbol == stock_symbol)
        return q.order_by(AgentContextRun.created_at.desc()).limit(max(1, limit)).all()
    finally:
        db.close()


def save_agent_prediction_outcome(
    *,
    agent_name: str,
    stock_symbol: str,
    stock_market: str,
    prediction_date: str,
    horizon_days: int,
    action: str,
    action_label: str,
    confidence: float | None = None,
    trigger_price: float | None = None,
    meta: dict | None = None,
    tenant_id: int | None = None,
) -> bool:
    tid = _resolve_tenant_id(tenant_id)
    db = SessionLocal()
    try:
        meta_safe = to_jsonable(meta or {})
        db.add(
            AgentPredictionOutcome(
                tenant_id=tid,
                agent_name=agent_name,
                stock_symbol=stock_symbol,
                stock_market=stock_market,
                prediction_date=prediction_date,
                horizon_days=max(1, int(horizon_days)),
                action=action or "watch",
                action_label=action_label or "观望",
                confidence=confidence,
                trigger_price=trigger_price,
                outcome_status="pending",
                meta=meta_safe,
            )
        )
        db.commit()
        return True
    except Exception as e:
        logger.warning(f"保存 prediction outcome 失败: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def mark_agent_prediction_outcome(
    *,
    record_id: int,
    outcome_price: float | None,
    outcome_return_pct: float | None,
    status: str = "evaluated",
    tenant_id: int | None = None,
) -> bool:
    tid = _resolve_tenant_id(tenant_id)
    db = SessionLocal()
    try:
        rec = (
            db.query(AgentPredictionOutcome)
            .filter(
                AgentPredictionOutcome.id == int(record_id),
                AgentPredictionOutcome.tenant_id == tid,
            )
            .first()
        )
        if not rec:
            return False
        rec.outcome_price = outcome_price
        rec.outcome_return_pct = outcome_return_pct
        rec.outcome_status = status
        rec.evaluated_at = datetime.now()
        db.commit()
        return True
    except Exception as e:
        logger.warning(f"更新 prediction outcome 失败: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def list_pending_prediction_outcomes(
    *,
    max_horizon_days: int = 10,
    limit: int = 300,
    tenant_id: int | None = None,
) -> list[AgentPredictionOutcome]:
    tid = _resolve_tenant_id(tenant_id)
    db = SessionLocal()
    try:
        today = date.today().strftime("%Y-%m-%d")
        q = db.query(AgentPredictionOutcome).filter(
            and_(
                AgentPredictionOutcome.tenant_id == tid,
                AgentPredictionOutcome.outcome_status == "pending",
                AgentPredictionOutcome.horizon_days <= max_horizon_days,
                AgentPredictionOutcome.prediction_date <= today,
            )
        )
        return (
            q.order_by(
                AgentPredictionOutcome.prediction_date.asc(),
                AgentPredictionOutcome.created_at.asc(),
            )
            .limit(limit)
            .all()
        )
    finally:
        db.close()


def list_agent_prediction_outcomes(
    *,
    agent_name: str | None = None,
    stock_symbol: str | None = None,
    status: str | None = None,
    days: int = 90,
    limit: int = 200,
    tenant_id: int | None = None,
) -> list[AgentPredictionOutcome]:
    tid = _resolve_tenant_id(tenant_id)
    db = SessionLocal()
    try:
        cutoff = (date.today() - timedelta(days=max(days, 1))).strftime("%Y-%m-%d")
        q = db.query(AgentPredictionOutcome).filter(
            AgentPredictionOutcome.tenant_id == tid,
            AgentPredictionOutcome.prediction_date >= cutoff,
        )
        if agent_name:
            q = q.filter(AgentPredictionOutcome.agent_name == agent_name)
        if stock_symbol:
            q = q.filter(AgentPredictionOutcome.stock_symbol == stock_symbol)
        if status:
            q = q.filter(AgentPredictionOutcome.outcome_status == status)
        return (
            q.order_by(
                AgentPredictionOutcome.prediction_date.desc(),
                AgentPredictionOutcome.created_at.desc(),
            )
            .limit(max(1, limit))
            .all()
        )
    finally:
        db.close()


def cleanup_context_data(
    *,
    snapshot_days: int = 180,
    topic_days: int = 180,
    context_run_days: int = 180,
    outcome_days: int = 365,
    tenant_id: int | None = None,
) -> dict:
    """按日期清理过期上下文数据。

    tenant_id 语义与其他函数不同（docs/24 §6 / docs/25 §F12）：
    - None（默认）= 全局清理，不按租户过滤（单租户直通唯一路径，行为与现状等价）；
    - 显式传入 = 仅清理该租户的行（多租户由调度器逐租户扇出调用，禁一次删全表）。
    """
    tid = int(tenant_id) if tenant_id is not None else None
    db = SessionLocal()
    deleted = {
        "stock_context_snapshots": 0,
        "news_topic_snapshots": 0,
        "agent_context_runs": 0,
        "agent_prediction_outcomes": 0,
    }
    try:
        snapshot_cutoff = (
            date.today() - timedelta(days=max(1, int(snapshot_days)))
        ).strftime("%Y-%m-%d")
        topic_cutoff = (
            date.today() - timedelta(days=max(1, int(topic_days)))
        ).strftime("%Y-%m-%d")
        context_run_cutoff = (
            date.today() - timedelta(days=max(1, int(context_run_days)))
        ).strftime("%Y-%m-%d")
        outcome_cutoff = (
            date.today() - timedelta(days=max(1, int(outcome_days)))
        ).strftime("%Y-%m-%d")

        snapshot_q = db.query(StockContextSnapshot).filter(
            StockContextSnapshot.snapshot_date < snapshot_cutoff
        )
        topic_q = db.query(NewsTopicSnapshot).filter(
            NewsTopicSnapshot.snapshot_date < topic_cutoff
        )
        context_run_q = db.query(AgentContextRun).filter(
            AgentContextRun.analysis_date < context_run_cutoff
        )
        outcome_q = db.query(AgentPredictionOutcome).filter(
            AgentPredictionOutcome.prediction_date < outcome_cutoff
        )
        if tid is not None:
            snapshot_q = snapshot_q.filter(StockContextSnapshot.tenant_id == tid)
            topic_q = topic_q.filter(NewsTopicSnapshot.tenant_id == tid)
            context_run_q = context_run_q.filter(AgentContextRun.tenant_id == tid)
            outcome_q = outcome_q.filter(AgentPredictionOutcome.tenant_id == tid)

        deleted["stock_context_snapshots"] = snapshot_q.delete(
            synchronize_session=False
        )
        deleted["news_topic_snapshots"] = topic_q.delete(synchronize_session=False)
        deleted["agent_context_runs"] = context_run_q.delete(
            synchronize_session=False
        )
        deleted["agent_prediction_outcomes"] = outcome_q.delete(
            synchronize_session=False
        )
        db.commit()
        return deleted
    except Exception as e:
        logger.warning(f"清理 context 数据失败: {e}")
        db.rollback()
        return deleted
    finally:
        db.close()
