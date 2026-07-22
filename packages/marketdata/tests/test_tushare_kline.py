"""Tushare K 线 vendor 单测:mock pro_api,不打真实网络。

覆盖:① 未装包 ② 无 token ③ 正常返回映射 Bar 字段 ④ 空数据 ⑤ env token 兜底。
"""

import sys
import types

import pandas as pd
import pytest

import marketdata.vendors.tushare as tv
from marketdata.symbol import Symbol


def _fake_tushare_module(df):
    """构造假的 tushare 模块:set_token/pro_api 可被调用,daily 返回固定 DataFrame。"""
    fake = types.ModuleType("tushare")
    calls = {}

    class _Pro:
        def daily(self, ts_code=None, start_date=None, end_date=None):
            calls["ts_code"] = ts_code
            calls["start_date"] = start_date
            calls["end_date"] = end_date
            return df

    fake.set_token = lambda token: calls.__setitem__("token", token)
    fake.pro_api = lambda: _Pro()
    fake._calls = calls
    return fake


def _daily_df(rows):
    """rows: (trade_date, open, high, low, close, vol, amount)。列对齐 pro.daily 真实产出。"""
    return pd.DataFrame(
        rows,
        columns=["trade_date", "open", "high", "low", "close", "vol", "amount"],
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每个用例默认清掉 TUSHARE_TOKEN,避免环境污染。"""
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)


def test_tushare_missing_lib_returns_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "tushare", None)  # import 触发 ImportError
    out = tv.TushareKlineVendor().fetch([Symbol.parse("600519")], {"token": "x", "days": 30})
    assert out == []


def test_tushare_missing_token_returns_empty(monkeypatch):
    df = _daily_df([("20260702", 1500.0, 1520.0, 1490.0, 1510.0, 100.0, 15000.0)])
    monkeypatch.setitem(sys.modules, "tushare", _fake_tushare_module(df))
    out = tv.TushareKlineVendor().fetch([Symbol.parse("600519")], {"days": 30})
    assert out == []


def test_tushare_parses_bars(monkeypatch):
    # pro.daily 倒序返回(最新在前),vendor 应升序排序
    df = _daily_df([
        ("20260702", 1500.0, 1520.0, 1490.0, 1510.0, 100.0, 15000.0),
        ("20260701", 1480.0, 1505.0, 1475.0, 1498.0, 120.0, 17800.0),
    ])
    fake = _fake_tushare_module(df)
    monkeypatch.setitem(sys.modules, "tushare", fake)

    out = tv.TushareKlineVendor().fetch([Symbol.parse("600519")], {"token": "tok123", "days": 30})
    assert len(out) == 2
    # 升序 + YYYYMMDD → YYYY-MM-DD
    assert out[0].date == "2026-07-01" and out[1].date == "2026-07-02"
    # 字段映射
    b = out[0]
    assert b.open == 1480.0 and b.high == 1505.0 and b.low == 1475.0 and b.close == 1498.0
    assert b.volume == 120.0
    # amount 单位千元 ×1000 → 元
    assert b.turnover == 17800.0 * 1000
    # ts_code 转换:600519 → 600519.SH;日期为 YYYYMMDD
    assert fake._calls["ts_code"] == "600519.SH"
    assert fake._calls["token"] == "tok123"
    assert len(fake._calls["start_date"]) == 8 and len(fake._calls["end_date"]) == 8


def test_tushare_ts_code_shenzhen(monkeypatch):
    df = _daily_df([("20260702", 10.0, 10.5, 9.8, 10.2, 500.0, 5000.0)])
    fake = _fake_tushare_module(df)
    monkeypatch.setitem(sys.modules, "tushare", fake)
    out = tv.TushareKlineVendor().fetch([Symbol.parse("000001")], {"token": "t", "days": 5})
    assert fake._calls["ts_code"] == "000001.SZ"
    assert len(out) == 1


def test_tushare_truncates_to_days(monkeypatch):
    df = _daily_df([
        ("20260701", 1.0, 1.5, 0.5, 1.2, 10, 100),
        ("20260702", 2.0, 2.5, 1.5, 2.2, 20, 200),
        ("20260703", 3.0, 3.5, 2.5, 3.2, 30, 300),
    ])
    monkeypatch.setitem(sys.modules, "tushare", _fake_tushare_module(df))
    out = tv.TushareKlineVendor().fetch([Symbol.parse("600519")], {"token": "t", "days": 2})
    assert len(out) == 2
    assert out[0].date == "2026-07-02" and out[1].date == "2026-07-03"


def test_tushare_empty_dataframe_returns_empty(monkeypatch):
    df = _daily_df([])
    monkeypatch.setitem(sys.modules, "tushare", _fake_tushare_module(df))
    out = tv.TushareKlineVendor().fetch([Symbol.parse("600519")], {"token": "t", "days": 30})
    assert out == []


def test_tushare_none_dataframe_returns_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "tushare", _fake_tushare_module(None))
    out = tv.TushareKlineVendor().fetch([Symbol.parse("600519")], {"token": "t", "days": 30})
    assert out == []


def test_tushare_env_token_fallback(monkeypatch):
    df = _daily_df([("20260702", 1.0, 1.5, 0.5, 1.2, 10, 100)])
    fake = _fake_tushare_module(df)
    monkeypatch.setitem(sys.modules, "tushare", fake)
    monkeypatch.setenv("TUSHARE_TOKEN", "env_tok")
    out = tv.TushareKlineVendor().fetch([Symbol.parse("600519")], {"days": 5})
    assert len(out) == 1
    assert fake._calls["token"] == "env_tok"


def test_tushare_config_token_beats_env(monkeypatch):
    df = _daily_df([("20260702", 1.0, 1.5, 0.5, 1.2, 10, 100)])
    fake = _fake_tushare_module(df)
    monkeypatch.setitem(sys.modules, "tushare", fake)
    monkeypatch.setenv("TUSHARE_TOKEN", "env_tok")
    tv.TushareKlineVendor().fetch([Symbol.parse("600519")], {"token": "cfg_tok", "days": 5})
    assert fake._calls["token"] == "cfg_tok"


def test_tushare_daily_raises_returns_empty(monkeypatch):
    fake = types.ModuleType("tushare")

    class _Pro:
        def daily(self, **k):
            raise RuntimeError("rate limited")

    fake.set_token = lambda token: None
    fake.pro_api = lambda: _Pro()
    monkeypatch.setitem(sys.modules, "tushare", fake)
    out = tv.TushareKlineVendor().fetch([Symbol.parse("600519")], {"token": "t", "days": 30})
    assert out == []


def test_tushare_no_symbols_returns_empty():
    assert tv.TushareKlineVendor().fetch([], {"token": "t", "days": 30}) == []


def test_tushare_rejects_non_cn_market(monkeypatch):
    df = _daily_df([("20260702", 1.0, 1.5, 0.5, 1.2, 10, 100)])
    monkeypatch.setitem(sys.modules, "tushare", _fake_tushare_module(df))
    out = tv.TushareKlineVendor().fetch([Symbol.parse("AAPL", market="US")], {"token": "t", "days": 30})
    assert out == []
