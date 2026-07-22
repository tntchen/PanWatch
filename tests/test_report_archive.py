"""P4 报告归档测试（scripts/import_report_dongxin.py + /reports/{filename} 路由）。

对应 doc/14 §1 P4：导入后按 agent+股票+日期可查；docx 下载路由不走 /api/ 前缀。

测试模式：临时目录 sqlite + 临时 reports 目录，不碰真实库（doc/14 §2）。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import scripts.import_report_dongxin as imp
from src.web import models as M
from src.web.database import Base


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """临时报告源文件 + 临时库 + 临时 reports 目录。"""
    md = tmp_path / "dongxin_report.agent.final.md"
    md.write_text("# 东芯股份（688110.SH）抄底分析报告\n\n正文内容\n", encoding="utf-8")
    docx = tmp_path / "东芯股份抄底分析报告.docx"
    docx.write_bytes(b"fake-docx-bytes")
    monkeypatch.setattr(imp, "REPORT_MD", md)
    monkeypatch.setattr(imp, "REPORT_DOCX", docx)
    db_path = str(tmp_path / "panwatch.db")
    reports_dir = str(tmp_path / "reports")
    return {
        "md": md,
        "docx": docx,
        "db_path": db_path,
        "reports_dir": reports_dir,
    }


def _query_record(db_path: str) -> M.AnalysisHistory | None:
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        return (
            db.query(M.AnalysisHistory)
            .filter(
                M.AnalysisHistory.agent_name == imp.AGENT_NAME,
                M.AnalysisHistory.stock_symbol == imp.STOCK_SYMBOL,
                M.AnalysisHistory.analysis_date == imp.ANALYSIS_DATE,
            )
            .first()
        )
    finally:
        db.close()
        engine.dispose()


def test_import_creates_record(workspace):
    """导入后按 agent+股票+日期可查，字段正确。"""
    result = imp.import_report(workspace["db_path"], workspace["reports_dir"])
    assert result["imported"] == 1
    assert result["docx_copied"] == 1

    row = _query_record(workspace["db_path"])
    assert row is not None
    assert row.agent_name == "research_report"
    assert row.stock_symbol == "688110"
    assert row.analysis_date == "2026-07-21"
    assert row.title == "东芯股份（688110.SH）抄底分析报告"
    assert "正文内容" in row.content
    assert row.raw_data["docx_file"] == "东芯股份抄底分析报告.docx"
    assert row.raw_data["download_url"] == "/reports/东芯股份抄底分析报告.docx"

    dest = os.path.join(workspace["reports_dir"], "东芯股份抄底分析报告.docx")
    assert os.path.isfile(dest)


def test_import_idempotent(workspace):
    """重复运行不产生重复记录，内容一致时 imported/updated 均为 0。"""
    imp.import_report(workspace["db_path"], workspace["reports_dir"])
    result = imp.import_report(workspace["db_path"], workspace["reports_dir"])
    assert result["imported"] == 0
    assert result["updated"] == 0
    assert result["docx_copied"] == 0

    engine = create_engine(f"sqlite:///{workspace['db_path']}")
    db = sessionmaker(bind=engine)()
    try:
        count = (
            db.query(M.AnalysisHistory)
            .filter(M.AnalysisHistory.agent_name == imp.AGENT_NAME)
            .count()
        )
    finally:
        db.close()
        engine.dispose()
    assert count == 1


def test_import_updates_on_content_change(workspace):
    """源 markdown 变化后重跑，就地更新同一记录而非新增。"""
    first = imp.import_report(workspace["db_path"], workspace["reports_dir"])
    workspace["md"].write_text("# 新标题\n\n更新后的正文\n", encoding="utf-8")
    second = imp.import_report(workspace["db_path"], workspace["reports_dir"])
    assert second["imported"] == 0
    assert second["updated"] == 1
    assert second["record_id"] == first["record_id"]

    row = _query_record(workspace["db_path"])
    assert row is not None
    assert row.title == "新标题"
    assert "更新后的正文" in row.content


def test_import_missing_source_fails(workspace):
    """源文件缺失时报错退出。"""
    workspace["md"].unlink()
    with pytest.raises(SystemExit):
        imp.import_report(workspace["db_path"], workspace["reports_dir"])


# ---------------------------------------------------------------------------
# /reports/{filename} 下载路由
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient + 临时 reports 目录（不碰真实 data/reports）。"""
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "东芯股份抄底分析报告.docx").write_bytes(b"fake-docx-bytes")
    monkeypatch.setattr("src.web.app.REPORTS_DIR", str(reports))
    from src.web.app import app

    return TestClient(app)


def test_download_route_not_under_api(client):
    """下载路由不走 /api/ 前缀且可下载，带 Content-Disposition。"""
    resp = client.get("/reports/东芯股份抄底分析报告.docx")
    assert resp.status_code == 200
    assert resp.content == b"fake-docx-bytes"
    disposition = resp.headers.get("content-disposition", "")
    assert "attachment" in disposition
    # ResponseWrapper 只包装 /api/ 前缀，响应不应被包成 {code, success, data}
    assert resp.headers.get("content-type", "") != "application/json"


def test_download_missing_file_404(client):
    """文件不存在返回 404。"""
    resp = client.get("/reports/not-exist.docx")
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "filename",
    ["../panwatch.db", "..\\panwatch.db", "sub/dir.docx", "..", "a/../../b"],
)
def test_download_path_traversal_rejected(filename):
    """路由函数层拒绝路径穿越文件名（400）。

    注：HTTP 层含斜杠/.. 的请求会被归一化或被 server.py SPA 兜底接管，
    不保证到达本路由；此处直接测路由函数本身的校验（双保险）。
    """
    import asyncio

    from fastapi import HTTPException

    from src.web.app import download_report

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(download_report(filename))
    assert exc_info.value.status_code == 400
