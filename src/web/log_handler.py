"""Custom logging handler that writes log entries to SQLite.

MT-P3（docs/23 §1.2-P21 / docs/26-J11）：emit 时点从 tenant_scope
contextvar 捕获 tenant_id 写入 log_entries（Timer 线程 flush 时
contextvar 已不可考，必须在 emit 捕获）；系统级日志（无 ctx）归 tenant_id=0。
bulk_insert_mappings 不触发 do_orm_execute，故显式写列（docs/25 §F12）。
"""

import logging
import threading
from datetime import datetime, timezone

from sqlalchemy import or_

from src.web.database import SessionLocal
from src.web.models import LogEntry
from src.web.tenant_context import current_tenant

MAX_LOG_ENTRIES_TOTAL = 120_000
MAX_INFRA_LOG_ENTRIES = 30_000
MAX_BUFFERED_ENTRIES = 2_000
BUFFER_SIZE = 80
FLUSH_INTERVAL = 1.0  # seconds
CLEANUP_EVERY_FLUSHES = 10
INFRA_LOGGER_PREFIXES = (
    "httpx",
    "httpcore",
    "urllib3",
    "uvicorn.access",
    "sqlalchemy.engine",
)

_ACTIVE_HANDLER = None


def _capture_tenant_id(record: logging.LogRecord) -> int:
    """emit 时点捕获租户身份（flush 在 Timer 线程，contextvar 不可考）。

    解析顺序：record.tenant_id（log_context 注入，预留）→ tenant_scope ctx
    → 0（系统级日志，docs/26-J11）。绝不抛异常——日志链路 fail-soft。
    """
    try:
        tid = getattr(record, "tenant_id", None)
        if isinstance(tid, int):
            return tid
        ctx = current_tenant()
        if ctx is not None:
            return int(ctx.tenant_id)
    except Exception:
        pass
    return 0


def get_log_handler_stats() -> dict:
    """Get runtime health stats of DB log handler."""
    h = _ACTIVE_HANDLER
    if not h:
        return {
            "enabled": False,
            "pending_entries": 0,
            "dropped_entries": 0,
            "flush_errors": 0,
            "last_flush_error": "",
            "last_flush_at": "",
        }
    with h._lock:
        return {
            "enabled": True,
            "pending_entries": len(h._buffer),
            "dropped_entries": h._dropped_entries,
            "flush_errors": h._flush_errors,
            "last_flush_error": h._last_flush_error,
            "last_flush_at": h._last_flush_at.isoformat() if h._last_flush_at else "",
        }


class DBLogHandler(logging.Handler):
    """Buffered logging handler that writes to the log_entries table."""

    def __init__(self, level=logging.DEBUG):
        super().__init__(level)
        self._buffer: list[dict] = []
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._dropped_entries = 0
        self._flush_errors = 0
        self._last_flush_error = ""
        self._last_flush_at = None
        self._flush_count = 0
        global _ACTIVE_HANDLER
        _ACTIVE_HANDLER = self
        self._start_flush_timer()

    def emit(self, record: logging.LogRecord):
        try:
            tags = getattr(record, "tags", {})
            if not isinstance(tags, dict):
                tags = {}
            entry = {
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc),
                "level": record.levelname,
                # Persist original module logger name; UI maps to Chinese for display
                "logger_name": getattr(record, "name", ""),
                "message": self.format(record),
                "trace_id": str(getattr(record, "trace_id", "") or "")[:64],
                "run_id": str(getattr(record, "run_id", "") or "")[:64],
                "agent_name": str(getattr(record, "agent_name", "") or "")[:64],
                "event": str(getattr(record, "event", "") or "")[:64],
                "tags": tags,
                "notify_status": str(getattr(record, "notify_status", "") or "")[:32],
                "notify_reason": str(getattr(record, "notify_reason", "") or "")[:255],
                "tenant_id": _capture_tenant_id(record),
            }
            with self._lock:
                if len(self._buffer) >= MAX_BUFFERED_ENTRIES:
                    overflow = len(self._buffer) - MAX_BUFFERED_ENTRIES + 1
                    if overflow > 0:
                        del self._buffer[:overflow]
                        self._dropped_entries += overflow
                self._buffer.append(entry)
                if record.levelno >= logging.ERROR or len(self._buffer) >= BUFFER_SIZE:
                    self._flush_unlocked()
        except Exception:
            # Avoid recursion if logging path fails
            pass

    def _start_flush_timer(self):
        self._timer = threading.Timer(FLUSH_INTERVAL, self._timed_flush)
        self._timer.daemon = True
        self._timer.start()

    def _timed_flush(self):
        with self._lock:
            self._flush_unlocked()
        self._start_flush_timer()

    def _flush_unlocked(self):
        if not self._buffer:
            return
        entries = self._buffer[:]
        self._buffer.clear()

        try:
            db = SessionLocal()
            try:
                db.bulk_insert_mappings(LogEntry, entries)
                db.commit()
                self._last_flush_at = datetime.now(timezone.utc)
                self._flush_count += 1
                if self._flush_count % CLEANUP_EVERY_FLUSHES == 0:
                    self._cleanup(db)
            finally:
                db.close()
        except Exception as e:
            self._flush_errors += 1
            self._last_flush_error = str(e)[:500]

    def _cleanup(self, db):
        """Retention policy: prioritize preserving business logs."""
        # 1) cap infrastructure noise first
        infra_filters = [LogEntry.logger_name.startswith(p) for p in INFRA_LOGGER_PREFIXES]
        infra_count = db.query(LogEntry).filter(or_(*infra_filters)).count()
        if infra_count > MAX_INFRA_LOG_ENTRIES:
            overflow = infra_count - MAX_INFRA_LOG_ENTRIES
            # delete oldest infra logs in one batch
            victim_ids = (
                db.query(LogEntry.id)
                .filter(or_(*infra_filters))
                .order_by(LogEntry.id.asc())
                .limit(overflow)
                .all()
            )
            if victim_ids:
                ids = [x[0] for x in victim_ids]
                db.query(LogEntry).filter(LogEntry.id.in_(ids)).delete(
                    synchronize_session=False
                )
                db.commit()

        # 2) global hard cap
        total = db.query(LogEntry).count()
        if total > MAX_LOG_ENTRIES_TOTAL:
            cutoff = (
                db.query(LogEntry.id)
                .order_by(LogEntry.id.desc())
                .offset(MAX_LOG_ENTRIES_TOTAL)
                .first()
            )
            if cutoff:
                db.query(LogEntry).filter(LogEntry.id <= cutoff[0]).delete(
                    synchronize_session=False
                )
                db.commit()

    def close(self):
        if self._timer:
            self._timer.cancel()
        with self._lock:
            self._flush_unlocked()
        super().close()
