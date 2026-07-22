"""Tushare K 线 vendor(可选,仅 A 股日线,需用户配 token)。

软依赖(对齐 D2 决策,不写进 requirements):
- 未安装 `tushare` 包 → 记日志返回 [],由 Engine 落到下一优先级源
- token 取 config["token"] → 环境变量 TUSHARE_TOKEN 兜底;缺失同样返回 []
- `tushare` 包在 fetch 内惰性 import,模块级导入本文件不引入重依赖
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from marketdata.http import record_error
from marketdata.symbol import Market, Symbol, _cn_exchange
from marketdata.types import Bar
from marketdata.vendors.base import KlineVendor

logger = logging.getLogger(__name__)


def _ts_code(sym: Symbol) -> str:
    """A 股代码转 Tushare 格式(600519 → 600519.SH / 000001 → 000001.SZ / 北交所 → .BJ)。"""
    return f"{sym.code}.{_cn_exchange(sym.code).upper()}"


def _days(config: dict, default: int = 60) -> int:
    try:
        return int(config.get("days") or default)
    except Exception:
        return default


class TushareKlineVendor(KlineVendor):
    name = "tushare"
    supports_markets = {"CN"}  # 仅 A 股(回归旧行为,不扩展 HK/US)

    def fetch(self, symbols: list[Symbol], config: dict) -> list[Bar]:
        if not symbols:
            return []
        sym = symbols[0]
        if sym.market != Market.CN:
            return []

        try:
            import tushare as ts
        except ImportError:
            msg = "tushare: 未安装 tushare 包,执行 `pip install tushare` 后启用"
            logger.warning(msg)
            record_error(msg)
            return []

        token = (config or {}).get("token") or os.environ.get("TUSHARE_TOKEN", "")
        if not token:
            msg = "tushare: token 未配置(DataSource.config.token 或环境变量 TUSHARE_TOKEN)"
            logger.warning(msg)
            record_error(msg)
            return []

        days = _days(config)
        ts_code = _ts_code(sym)
        # pro.daily 按交易日取数,start 多预留自然日保证拿够 days 条(回归旧实现 days*2)
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")

        try:
            ts.set_token(token)
            pro = ts.pro_api()
            df = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
        except Exception as e:
            msg = f"tushare: daily 调用失败({ts_code}): {type(e).__name__}: {e}"
            logger.warning(msg)
            record_error(msg)
            return []

        if df is None or len(df) == 0:
            return []

        # df 列:ts_code/trade_date/open/high/low/close/pre_close/change/pct_chg/vol/amount
        # vol=成交量(手,保持与旧实现一致原样透传);amount=成交额(千元)×1000 → 元
        df = df.sort_values("trade_date")  # 升序,与其他 K 线源保持一致
        out: list[Bar] = []
        for _, row in df.iterrows():
            try:
                d = str(row["trade_date"])
                date_fmt = f"{d[:4]}-{d[4:6]}-{d[6:8]}"  # YYYYMMDD → YYYY-MM-DD
                amount = row.get("amount")
                turnover = float(amount) * 1000 if amount is not None else None
                if turnover is not None and turnover != turnover:  # NaN → None,不伪造
                    turnover = None
                out.append(Bar(
                    date=date_fmt,
                    open=float(row["open"]),
                    close=float(row["close"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    volume=float(row.get("vol") or 0),
                    turnover=turnover,
                ))
            except Exception as e:
                logger.debug(f"tushare row 解析失败: {e}")
                continue
        return out[-days:] if days > 0 else out
