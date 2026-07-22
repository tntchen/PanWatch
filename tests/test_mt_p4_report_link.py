"""MT-P4-C：/reports 下载 HMAC 签名 URL（docs/25 §6 验收）。

验收三用例（§6.2 / MT-P5 门禁）：
- 伪造 sig → 403
- 过期 exp（签名本身合法）→ 410
- 跨租户（A 的 sig 取 B 的 tenant_id 路径）→ 403

另覆盖：正确签名 200、单租户直通旧无签名链接 200、单租户下新签名链接 200、
多租户模式旧无签名链接 403、多租户签名链接缺参数 403。

测试模式：临时 reports 目录 + monkeypatch 固定 jwt_secret，不碰真实库与真实密钥。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.core.report_link import make_signed_report_url, verify_report_signature

_TEST_SECRET = "mt-p4-report-link-test-secret"
_FILENAME = "东芯股份抄底分析报告.docx"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient + 临时 reports 目录 + 固定 jwt_secret（不碰真实 data/reports 与 DB 密钥）。"""
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / _FILENAME).write_bytes(b"fake-docx-bytes")
    monkeypatch.setattr("src.web.app.REPORTS_DIR", str(reports))
    monkeypatch.setattr(
        "src.core.report_link.get_jwt_secret", lambda: _TEST_SECRET
    )
    from src.web.app import app

    return TestClient(app)


@pytest.fixture
def multi_tenant(monkeypatch):
    """强制多租户模式（PANWATCH_SINGLE_TENANT != '1'）。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "0")


@pytest.fixture
def single_tenant(monkeypatch):
    """强制单租户直通模式。"""
    monkeypatch.setenv("PANWATCH_SINGLE_TENANT", "1")


def _signed_url(tenant_id: int = 1, exp_seconds: int = 7 * 86400) -> str:
    return make_signed_report_url(
        tenant_id, _FILENAME, base_url="http://testserver", exp_seconds=exp_seconds
    )


# ---------------------------------------------------------------------------
# docs/25 §6.2 验收三用例
# ---------------------------------------------------------------------------


def test_forged_sig_403(client, multi_tenant):
    """伪造 sig → 403。"""
    exp = int(time.time()) + 3600
    resp = client.get(f"/reports/1/{_FILENAME}?exp={exp}&sig={'0' * 64}")
    assert resp.status_code == 403


def test_expired_exp_410(client, multi_tenant):
    """过期 exp（签名本身合法）→ 410。"""
    url = _signed_url(1, exp_seconds=-10)  # 已过期 10 秒的合法签名
    resp = client.get(url)
    assert resp.status_code == 410


def test_cross_tenant_sig_403(client, multi_tenant):
    """跨租户：A 租户的 sig 挪到 B 租户路径 → 403（msg 绑定 tenant_id）。"""
    url_a = _signed_url(1)
    url_b = url_a.replace("/reports/1/", "/reports/2/")
    resp = client.get(url_b)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 正确签名 / 模式回退
# ---------------------------------------------------------------------------


def test_valid_signed_url_200(client, multi_tenant):
    """多租户模式下正确签名 → 200，内容一致。"""
    resp = client.get(_signed_url(1))
    assert resp.status_code == 200
    assert resp.content == b"fake-docx-bytes"


def test_legacy_unsigned_url_single_tenant_200(client, single_tenant):
    """单租户直通：旧形态无签名链接放行（等价现状，存量通知外链不断）。"""
    resp = client.get(f"/reports/{_FILENAME}")
    assert resp.status_code == 200
    assert resp.content == b"fake-docx-bytes"


def test_signed_url_also_works_in_single_tenant(client, single_tenant):
    """单租户模式下新签名链接同样可用（验签通过即放行）。"""
    resp = client.get(_signed_url(1))
    assert resp.status_code == 200
    assert resp.content == b"fake-docx-bytes"


def test_legacy_unsigned_url_multi_tenant_403(client, multi_tenant):
    """多租户模式：旧无签名链接一次性失效（无兼容期，§6.2 已裁决）。"""
    resp = client.get(f"/reports/{_FILENAME}")
    assert resp.status_code == 403


def test_signed_url_missing_params_403(client, multi_tenant):
    """签名链接缺 exp/sig 参数 → 403。"""
    assert client.get(f"/reports/1/{_FILENAME}").status_code == 403
    exp = int(time.time()) + 3600
    assert client.get(f"/reports/1/{_FILENAME}?exp={exp}").status_code == 403
    assert client.get(f"/reports/1/{_FILENAME}?sig={'0' * 64}").status_code == 403


def test_tampered_filename_403(client, multi_tenant):
    """篡改 filename（换文件）→ 403（msg 绑定 filename）。"""
    url = _signed_url(1)
    query = url.split("?", 1)[1]  # exp=..&sig=..
    tampered = f"/reports/1/other.docx?{query}"
    assert client.get(tampered).status_code == 403


def test_valid_sig_but_missing_file_404(client, multi_tenant):
    """签名合法但文件不存在 → 404（验签通过后才走文件逻辑）。"""
    url = make_signed_report_url(
        1, "not-exist.docx", base_url="http://testserver"
    )
    assert client.get(url).status_code == 404


# ---------------------------------------------------------------------------
# helper 单测
# ---------------------------------------------------------------------------


def test_make_signed_report_url_shape(monkeypatch):
    """生成的 URL 形态符合 /reports/{tenant_id}/{filename}?exp=..&sig=..，且自验通过。"""
    monkeypatch.setattr(
        "src.core.report_link.get_jwt_secret", lambda: _TEST_SECRET
    )
    url = make_signed_report_url(3, "a b.docx", base_url="https://x.example/")
    assert url.startswith("https://x.example/reports/3/")
    assert "a%20b.docx" in url
    assert "exp=" in url and "sig=" in url


def test_verify_report_signature_bad_exp():
    """exp 非整数 / sig 为空 → False。"""
    assert verify_report_signature(1, "f.docx", "not-a-number", "ab" * 32) is False
    assert verify_report_signature(1, "f.docx", int(time.time()), "") is False
