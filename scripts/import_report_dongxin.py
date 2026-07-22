#!/usr/bin/env python3
"""东芯股份（688110.SH）47 页研究报告归档导入脚本 —— Phase 4 P4。

把 kimi chat 产出的终稿 markdown 导入 analysis_history 表：
- agent_name = "research_report"
- stock_symbol = "688110"
- analysis_date = "2026-07-21"（报告完成日）

同时把配套 Word 文档复制到 <db 所在目录>/reports/ 下（保持中文文件名），
供 src/web/app.py 的 /reports/{filename} 下载路由（非 /api/ 前缀，
避开 ResponseWrapper 大响应缓冲）提供下载。

幂等：按 (agent_name, stock_symbol, analysis_date) 唯一约束先查后插，
已存在则就地更新 content/title/raw_data，重复执行不产生重复记录。

用法:
    python scripts/import_report_dongxin.py
    python scripts/import_report_dongxin.py --db /path/to/panwatch.db
    python scripts/import_report_dongxin.py --reports-dir /path/to/reports
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 允许以脚本方式直接运行（python scripts/import_report_dongxin.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.agent_catalog import infer_agent_kind  # noqa: E402
from src.web.database import Base  # noqa: E402
from src.web.models import AnalysisHistory  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("import_report_dongxin")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_MD = (
    PROJECT_ROOT
    / "doc/finance_project/03_deliverable/dongxin_report.agent.final.md"
)
REPORT_DOCX = (
    PROJECT_ROOT / "doc/finance_project/03_deliverable/东芯股份抄底分析报告.docx"
)

AGENT_NAME = "research_report"
STOCK_SYMBOL = "688110"
ANALYSIS_DATE = "2026-07-21"  # 报告完成日


def _extract_title(markdown: str) -> str:
    """取 markdown 首个一级标题作为记录标题。"""
    for line in markdown.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return "东芯股份抄底分析报告"


def import_report(db_path: str, reports_dir: str | None = None) -> dict:
    """幂等导入。返回 {"imported": 0|1, "updated": 0|1, "docx_copied": 0|1, ...}。"""
    if not REPORT_MD.exists():
        raise SystemExit(f"报告 markdown 不存在: {REPORT_MD}")
    if not REPORT_DOCX.exists():
        raise SystemExit(f"报告 docx 不存在: {REPORT_DOCX}")

    content = REPORT_MD.read_text(encoding="utf-8")
    title = _extract_title(content)

    # docx 复制目标：默认 <db 所在目录>/reports/
    dest_dir = Path(reports_dir) if reports_dir else Path(db_path).parent / "reports"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_docx = dest_dir / REPORT_DOCX.name
    docx_copied = 0
    if not dest_docx.exists() or dest_docx.stat().st_size != REPORT_DOCX.stat().st_size:
        shutil.copy2(REPORT_DOCX, dest_docx)
        docx_copied = 1
        logger.info(f"docx 已复制: {dest_docx}")
    else:
        logger.info(f"docx 已存在，跳过复制: {dest_docx}")

    try:
        source_md = str(REPORT_MD.relative_to(PROJECT_ROOT))
    except ValueError:  # 测试等场景源文件不在项目目录内
        source_md = str(REPORT_MD)
    raw_data = {
        "source": "kimi_chat",
        "source_md": source_md,
        "docx_file": REPORT_DOCX.name,
        "download_url": f"/reports/{REPORT_DOCX.name}",
        "note": "47 页深度研究报告归档（P4）",
    }

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        row = (
            db.query(AnalysisHistory)
            .filter(
                AnalysisHistory.agent_name == AGENT_NAME,
                AnalysisHistory.stock_symbol == STOCK_SYMBOL,
                AnalysisHistory.analysis_date == ANALYSIS_DATE,
            )
            .first()
        )
        imported = 0
        updated = 0
        if row is None:
            row = AnalysisHistory(
                agent_name=AGENT_NAME,
                stock_symbol=STOCK_SYMBOL,
                analysis_date=ANALYSIS_DATE,
                title=title,
                content=content,
                raw_data=raw_data,
                agent_kind_snapshot=infer_agent_kind(AGENT_NAME),
            )
            db.add(row)
            imported = 1
        elif (
            row.content != content
            or (row.title or "") != title
            or (row.raw_data or {}) != raw_data
        ):
            row.title = title
            row.content = content
            row.raw_data = raw_data
            updated = 1
        else:
            logger.info(
                f"跳过（记录已存在且内容一致）: id={row.id} "
                f"{AGENT_NAME}/{STOCK_SYMBOL}/{ANALYSIS_DATE}"
            )
        db.commit()
        db.refresh(row)
        logger.info(
            f"归档完成: id={row.id} imported={imported} updated={updated} "
            f"content_chars={len(content)}"
        )
        return {
            "imported": imported,
            "updated": updated,
            "docx_copied": docx_copied,
            "record_id": row.id,
            "docx_path": str(dest_docx),
        }
    except Exception:
        db.rollback()
        logger.exception("导入失败，已回滚")
        raise
    finally:
        db.close()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="东芯 47 页研究报告归档导入（幂等）")
    parser.add_argument(
        "--db",
        type=str,
        default=os.path.join("./data", "panwatch.db"),
        help="sqlite 数据库路径（默认 ./data/panwatch.db；web 层 DB_PATH 硬编码该值）",
    )
    parser.add_argument(
        "--reports-dir",
        type=str,
        default=None,
        help="docx 复制目标目录（默认 <db 所在目录>/reports）",
    )
    args = parser.parse_args()

    result = import_report(args.db, args.reports_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
