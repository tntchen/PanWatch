"""Notification dedupe helpers.

We reuse the existing notify_throttle table to implement idempotency for
batch agents (avoid duplicate notifications on restarts/manual triggers).

MT-P3（docs/23 §3 / docs/26-J4）：tenant 编进 scope 字符串
``__notify__:{tenant_id}:{hash}``，不重建 UQ；函数显式接收 tenant_id
做双保险（行级 tenant_id 列归因 + 查询过滤），单租户直通模式恒为 1，
与 v120 回填后的旧行（``__notify__:1:{hash}``）键格式一致。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from src.core.timezone import utc_now
from src.web.database import SessionLocal
from src.web.models import NotifyThrottle
from src.web.tenant_context import DEFAULT_TENANT_ID


def build_notify_dedupe_key(agent_name: str, title: str, content: str) -> str:
    base = "|".join(
        [
            (agent_name or "").strip(),
            (title or "").strip(),
            " ".join((content or "").strip().split())[:1200],
        ]
    )
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def build_notify_scope(tenant_id: int, dedupe_key: str) -> str:
    """全局去重 scope：tenant 前缀 + 内容 hash（docs/26-J4，UQ 不重建）。

    单租户直通模式 tenant_id 恒为 1，与 v120 幂等回填后的旧行格式一致，
    去重连续性由迁移保证。
    """
    tid = int(tenant_id) if tenant_id else DEFAULT_TENANT_ID
    return f"__notify__:{tid}:{dedupe_key}"


def _now_utc_naive():
    return utc_now().replace(tzinfo=None)


def check_and_mark_notify(
    *,
    agent_name: str,
    scope: str,
    ttl_minutes: int,
    mark: bool,
    tenant_id: int,
) -> bool:
    """Check whether a notification should be allowed.

    Args:
        agent_name: agent name.
        scope: a unique scope under the agent (we store in stock_symbol);
            由 ``build_notify_scope`` 构造，自身已含 tenant 前缀。
        ttl_minutes: within this window we treat as duplicate.
        mark: whether to update last_notify_at when allowed.
        tenant_id: 行级归因 + 查询双保险过滤（单租户直通恒为 1）。

    Returns:
        True if allowed; False if deduped.
    """

    if ttl_minutes <= 0:
        return True

    tid = int(tenant_id) if tenant_id else DEFAULT_TENANT_ID
    db = SessionLocal()
    try:
        now = _now_utc_naive()
        threshold = now - timedelta(minutes=ttl_minutes)

        record = (
            db.query(NotifyThrottle)
            .filter(
                NotifyThrottle.agent_name == agent_name,
                NotifyThrottle.stock_symbol == scope,
                NotifyThrottle.tenant_id == tid,
            )
            .first()
        )

        if record and record.last_notify_at and record.last_notify_at >= threshold:
            return False

        if mark:
            if record:
                record.last_notify_at = now
                record.notify_count = (record.notify_count or 0) + 1
            else:
                db.add(
                    NotifyThrottle(
                        tenant_id=tid,
                        agent_name=agent_name,
                        stock_symbol=scope,
                        last_notify_at=now,
                        notify_count=1,
                    )
                )
            db.commit()
        return True
    except Exception:
        db.rollback()
        # If dedupe fails, prefer sending rather than dropping.
        return True
    finally:
        db.close()
