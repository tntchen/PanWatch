"""板块定向行情/K线(P3e:secid=90.BKxxxx 通路)。

离线 monkeypatch market_get,不实抓。
"""

from __future__ import annotations

import marketdata.vendors.eastmoney as em
import marketdata.vendors.kline as kv
from marketdata.client import MarketData
from marketdata.defaults import StaticConfigProvider
from marketdata.types import Quote


def _board_payload() -> dict:
    return {
        "data": {
            "f43": 123456,   # 最新价(原始值,/10^f59)
            "f44": 125000,
            "f45": 122000,
            "f46": 123000,
            "f47": 9.8e8,    # 成交量
            "f48": 1.2e10,   # 成交额
            "f50": 135,      # 量比(/100)
            "f57": "BK0475",
            "f58": "存储器",
            "f59": 2,        # 小数位
            "f60": 121000,   # 昨收
            "f169": 2456,    # 涨跌额
            "f170": 203,     # 涨跌幅(/100)
        }
    }


class TestNormalizeBoardCode:
    def test_valid_codes(self):
        assert kv.normalize_board_code("BK0475") == "BK0475"
        assert kv.normalize_board_code(" bk0475 ") == "BK0475"

    def test_invalid_codes(self):
        assert kv.normalize_board_code("") == ""
        assert kv.normalize_board_code(None) == ""
        assert kv.normalize_board_code("600519") == ""
        assert kv.normalize_board_code("BK") == ""
        assert kv.normalize_board_code("BKABC") == ""


class TestBoardQuote:
    def test_parses_board_quote(self, monkeypatch):
        captured = {}

        def fake_market_get(url, *, params=None, **kwargs):
            captured["secid"] = (params or {}).get("secid")
            return _board_payload()

        monkeypatch.setattr(em, "market_get", fake_market_get)
        q = em.fetch_eastmoney_board_quote("bk0475")
        assert captured["secid"] == "90.BK0475"
        assert isinstance(q, Quote)
        assert q.symbol == "BK0475"
        assert q.market == "CN"
        assert q.name == "存储器"
        assert q.current_price == 1234.56
        assert q.prev_close == 1210.0
        assert q.change_pct == 2.03
        assert q.volume == 9.8e8
        assert q.turnover == 1.2e10

    def test_invalid_code_returns_none_without_request(self, monkeypatch):
        calls = {"n": 0}

        def fake_market_get(*a, **k):
            calls["n"] += 1
            return _board_payload()

        monkeypatch.setattr(em, "market_get", fake_market_get)
        assert em.fetch_eastmoney_board_quote("600519") is None
        assert calls["n"] == 0

    def test_empty_payload_returns_none(self, monkeypatch):
        monkeypatch.setattr(em, "market_get", lambda *a, **k: None)
        assert em.fetch_eastmoney_board_quote("BK0475") is None

    def test_missing_price_returns_none(self, monkeypatch):
        monkeypatch.setattr(em, "market_get", lambda *a, **k: {"data": {"f43": None}})
        assert em.fetch_eastmoney_board_quote("BK0475") is None


class TestClientBoardMethods:
    def _md(self) -> MarketData:
        return MarketData(config=StaticConfigProvider({}))

    def test_board_quote(self, monkeypatch):
        monkeypatch.setattr(em, "market_get", lambda *a, **k: _board_payload())
        q = self._md().board_quote("BK0475")
        assert q is not None and q.name == "存储器" and q.current_price == 1234.56

    def test_board_quote_invalid_code_returns_none(self):
        assert self._md().board_quote("600519") is None

    def test_board_klines(self, monkeypatch):
        captured = {}

        def fake_market_get(url, *, params=None, **kwargs):
            captured["secid"] = (params or {}).get("secid")
            return {"data": {"klines": ["2026-07-01,1000,1010,1020,990,1e8,2.5e10"]}}

        monkeypatch.setattr(kv, "market_get", fake_market_get)
        out = self._md().board_klines("BK0475", days=60)
        assert captured["secid"] == "90.BK0475"
        assert len(out) == 1 and out[0].close == 1010.0 and out[0].turnover == 2.5e10

    def test_board_klines_invalid_code_returns_empty(self):
        assert self._md().board_klines("000001") == []
