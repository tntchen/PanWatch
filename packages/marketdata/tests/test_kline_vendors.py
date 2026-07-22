import marketdata.vendors.kline as kv
from marketdata.symbol import Symbol
from marketdata.types import Bar


def test_tencent_kline_parses(monkeypatch):
    js = 'kline_dayqfq={"data":{"sh600519":{"day":[["2026-07-01","1","3","4","0.5","100"],["2026-07-02","3","5","6","2","200"]]}}};'
    monkeypatch.setattr(kv, "market_get", lambda *a, **k: js)
    out = kv.TencentKlineVendor().fetch([Symbol.parse("600519")], {"days": 60})
    assert len(out) == 2 and isinstance(out[0], Bar)
    assert out[0].date == "2026-07-01" and out[0].close == 3.0 and out[1].volume == 200.0


def test_eastmoney_kline_parses(monkeypatch):
    payload = {"data": {"klines": ["2026-07-01,1,3,4,0.5,100", "2026-07-02,3,5,6,2,200"]}}
    monkeypatch.setattr(kv, "market_get", lambda *a, **k: payload)
    out = kv.EastmoneyKlineVendor().fetch([Symbol.parse("600519")], {"days": 60})
    assert len(out) == 2 and out[1].high == 6.0


def test_eastmoney_kline_parses_turnover(monkeypatch):
    """fields2 含 f57(成交额):7 段行映射进 Bar.turnover;6 段老行 → None(不伪造)。"""
    captured = {}

    def fake_market_get(url, *, params=None, **kwargs):
        captured["fields2"] = (params or {}).get("fields2")
        return {"data": {"klines": [
            "2026-07-01,1,3,4,0.5,100,123456.7",
            "2026-07-02,3,5,6,2,200",          # 老响应形态:无成交额段
            "2026-07-03,3,5,6,2,200,-",         # 占位符:成交额缺失
        ]}}

    monkeypatch.setattr(kv, "market_get", fake_market_get)
    out = kv.EastmoneyKlineVendor().fetch([Symbol.parse("600519")], {"days": 60})
    assert captured["fields2"] == "f51,f52,f53,f54,f55,f56,f57"
    assert len(out) == 3
    assert out[0].turnover == 123456.7
    assert out[1].turnover is None
    assert out[2].turnover is None


def test_eastmoney_board_klines_uses_board_secid(monkeypatch):
    """板块日K:secid=90.BKxxxx,复用东财K线解析;非法 code → [] 且不发请求。"""
    captured = {}

    def fake_market_get(url, *, params=None, **kwargs):
        captured["secid"] = (params or {}).get("secid")
        return {"data": {"klines": ["2026-07-01,1000,1010,1020,990,1e8,2.5e10"]}}

    monkeypatch.setattr(kv, "market_get", fake_market_get)
    out = kv.fetch_eastmoney_board_klines("bk0475", 120)
    assert captured["secid"] == "90.BK0475"
    assert len(out) == 1 and out[0].close == 1010.0 and out[0].turnover == 2.5e10

    captured.clear()
    assert kv.fetch_eastmoney_board_klines("600519", 120) == []
    assert kv.fetch_eastmoney_board_klines("", 120) == []
    assert "secid" not in captured


def test_stooq_kline_parses(monkeypatch):
    csv = "Date,Open,High,Low,Close,Volume\n2026-07-01,1,4,0.5,3,100\n2026-07-02,3,6,2,5,200\n"
    monkeypatch.setattr(kv, "market_get", lambda *a, **k: csv)
    out = kv.StooqKlineVendor().fetch([Symbol.parse("AAPL")], {})
    assert len(out) == 2 and out[0].close == 3.0 and out[1].close == 5.0
