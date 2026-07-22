#!/usr/bin/env python3
"""东芯股份（688110.SH）抄底实施方案 v3.1 档案幂等导入脚本 —— Phase 2 P2a。

从以下两份文档提取内容，按四方契约 A 结构化为 stock_playbooks.payload：
- doc/finance_project/03_deliverable/东芯股份抄底实施方案_v3.md（raw_markdown 放全文）
- doc/finance_project/06_tracking/定时监控配置说明.md（价位全表 / 触发器 / 策略模式）

幂等：同一股票下已存在相同 payload.meta.version_label 的档案则跳过，重复执行安全。

用法:
    python scripts/import_playbook_dongxin.py
    python scripts/import_playbook_dongxin.py --db /path/to/panwatch.db
    python scripts/import_playbook_dongxin.py --symbol 688110 --stock-id 3

--db 默认取环境变量 DATA_DIR（缺省 ./data）下的 panwatch.db。
股票不存在时自动创建（symbol/name/market 可用参数覆盖）。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 允许以脚本方式直接运行（python scripts/import_playbook_dongxin.py）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.playbook import summarize_playbook  # noqa: E402
from src.web.database import Base  # noqa: E402
from src.web.models import Stock, StockPlaybook  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("import_playbook_dongxin")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLAN_MD = PROJECT_ROOT / "doc/finance_project/03_deliverable/东芯股份抄底实施方案_v3.md"

VERSION_LABEL = "v3.1"


def build_payload(raw_markdown: str) -> dict:
    """按契约 A 组装东芯 v3.1 方案 payload。"""
    return {
        "schema_version": 1,
        "meta": {
            "name": "东芯股份抄底实施方案",
            "version_label": VERSION_LABEL,
            "strategy_mode": "激进·满仓单票",
            "base_date": "2026-07-20",
            "base_price": 100.38,
        },
        "price_levels": [
            {"label": "高点", "value": 209.55, "note": "07-01 峰值"},
            {"label": "低点", "value": 94.46, "note": "07-20 盘中低点"},
            {"label": "6月平台上沿", "value": 135, "note": "做T卖出区上沿"},
            {"label": "6月平台下沿", "value": 122, "note": ""},
            {"label": "缺口", "value": 109.75, "note": "回补缺口跌破且放量→冻结"},
            {"label": "受让价", "value": 100, "note": "大股东受让价+整数关口双锚"},
            {"label": "前低", "value": 104.01, "note": "已破"},
            {"label": "防线", "value": 98, "note": "连续2日收盘<98触发"},
        ],
        "batches": [
            {
                "name": "①",
                "trigger": "120±3",
                "logic": "6月平台下沿",
                "status": "executed",
            },
            {
                "name": "②",
                "trigger": "110±3",
                "logic": "4-5月整理区",
                "status": "executed",
            },
            {
                "name": "③",
                "trigger": "100±3",
                "logic": "大股东受让价+整数关口双锚",
                "status": "executed",
            },
            {
                "name": "④右侧批",
                "trigger": "三信号同时满足",
                "logic": "缩量(日换手率<4%)+企稳(连续2日收盘站稳100)+资金(主力转净流入)；"
                "回踩110-115缩量企稳后投入12-15万一次性打满(约1000-1300股)；"
                "强催化(砺算B轮/大基金进驻/IPO辅导备案)可豁免资金信号",
                "status": "frozen",
            },
        ],
        "t_zone": {
            "sell_range": [125, 135],
            "buyback_range": [110, 115],
            "size": "500-1000股",
            "mode": "先卖后买",
        },
        "defense": {
            "rule": "连续2日收盘<98",
            "action": "减仓1/3~1/2至核心仓（满仓模式最高纪律，必须执行）；"
            "缩量守住100→持有但不追加",
        },
        "stop_loss_tracks": [
            {
                "track": "行业轨",
                "trigger": "Q4合约价环比走平/转跌（跟踪Q3末-Q4谈判）",
                "action": "减仓至观察仓，认赔区间90-100",
            },
            {
                "track": "公司轨",
                "trigger": "08-22半年报Q2毛利率<40%(证伪)/40-45%(警戒)",
                "action": "证伪→减仓1/2；警戒→冻结一切加仓",
            },
            {
                "track": "期权轨",
                "trigger": "砺算资金链断裂/核心团队流失/产品重大事故",
                "action": "不触发卖出（期权定价≈0），取消上行情景加码，退回纯存储持有逻辑",
            },
        ],
        "calendar": [
            {
                "date": "2026-07-27",
                "event": "长鑫科技挂牌（预计）",
                "bias": "中性偏多（抽血落地）",
                "plan": "挂牌后板块止跌→情绪底信号；长鑫暴涨而板块续跌→警惕挤出",
            },
            {
                "date": "2026-08-22",
                "event": "东芯半年报",
                "bias": "关键验证",
                "plan": "前轻仓试探、确认（毛利率≥45%）后加码；证伪执行公司轨止损",
            },
            {
                "date": "2026-09-01",
                "event": "9-10月Q3合约价落地+Q4谈判开启",
                "bias": "方向标",
                "plan": "涨幅兑现→持有；走平→行业轨预警",
            },
            {
                "date": "2026-10-13",
                "event": "询价受让442万股解禁（成本100元）",
                "bias": "偏空",
                "plan": "解禁前后利用供给冲击低吸（若右侧信号已现）",
            },
        ],
        "scenarios": [
            {
                "name": "上行",
                "trigger": "半年报毛利率≥45%+Q3合约价兑现+20%以上+"
                "（砺算B轮/辅导备案|H股落地|GPU板块情绪修复，任一）",
                "action": "右侧批执行，修复目标120-130；催化强度高（砺算辅导落地）看至140+",
            },
            {
                "name": "基准",
                "trigger": "涨价延续但涨幅收窄，砺算/H股无进展",
                "action": "持有核心仓+100-120区间波段，不加不减",
            },
            {
                "name": "下行",
                "trigger": "Q4合约价转跌|半年报毛利率<40%|连续2日收盘<98（任一）",
                "action": "按三轨止损执行，减仓至观察仓，回归纯存储价值区间（76-90）再评估",
            },
        ],
        "trigger_hints": {
            "右侧批触发区": "右侧批：回踩110-115缩量企稳，三信号（换手率<4%+连续2日站稳100+"
            "主力净流入）同时满足→投入12-15万一次性打满",
            "做T卖出区": "做T（先卖后买）：125-135卖出500-1000股回笼资金，回踩110-115接回",
            "防线98": "防线：连续2日收盘<98→减仓1/3~1/2（满仓模式最高纪律，必须执行）",
            "换手率<4%": "右侧批条件1/3：日换手率回落至4%以下（缩量）",
            "主力净流入": "右侧批条件3/3：主力资金转为净流入（强催化可豁免）",
            "冻结信号": "冻结：跌回105下方/回补缺口跌破109.75且放量→右侧批冻结",
        },
        "raw_markdown": raw_markdown,
    }


def _find_or_create_stock(db, symbol: str, name: str, market: str, stock_id: int | None) -> Stock:
    if stock_id is not None:
        stock = db.query(Stock).filter(Stock.id == stock_id).first()
        if not stock:
            raise SystemExit(f"股票不存在: stock_id={stock_id}")
        return stock
    stock = (
        db.query(Stock)
        .filter(Stock.symbol == symbol, Stock.market == market)
        .first()
    )
    if not stock:
        stock = Stock(symbol=symbol, name=name, market=market)
        db.add(stock)
        db.flush()
        logger.info(f"股票不存在，已创建: {symbol} {name} ({market}) id={stock.id}")
    return stock


def import_playbook(db_path: str, symbol: str, name: str, market: str, stock_id: int | None) -> dict:
    """幂等导入。返回 {"imported": 0|1, "skipped": 0|1, "playbook_id": ...}。"""
    if not PLAN_MD.exists():
        raise SystemExit(f"方案文档不存在: {PLAN_MD}")
    raw_markdown = PLAN_MD.read_text(encoding="utf-8")
    payload = build_payload(raw_markdown)

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        stock = _find_or_create_stock(db, symbol, name, market, stock_id)

        # 幂等：同 version_label 已存在则跳过
        for row in (
            db.query(StockPlaybook)
            .filter(StockPlaybook.stock_id == stock.id)
            .all()
        ):
            meta = (row.payload or {}).get("meta") or {}
            if meta.get("version_label") == VERSION_LABEL:
                logger.info(
                    f"跳过（已存在）: stock_id={stock.id} version_label={VERSION_LABEL} "
                    f"playbook_id={row.id}"
                )
                db.commit()
                return {"imported": 0, "skipped": 1, "playbook_id": row.id}

        max_version = (
            db.query(StockPlaybook.version)
            .filter(StockPlaybook.stock_id == stock.id)
            .order_by(StockPlaybook.version.desc())
            .first()
        )
        next_version = (max_version[0] if max_version else 0) + 1

        db.query(StockPlaybook).filter(
            StockPlaybook.stock_id == stock.id,
            StockPlaybook.is_active.is_(True),
        ).update({"is_active": False}, synchronize_session="fetch")

        row = StockPlaybook(
            stock_id=stock.id,
            version=next_version,
            is_active=True,
            payload=payload,
            summary=summarize_playbook(payload),
            note="东芯 v3.1 方案导入（激进·满仓单票模式）",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info(
            f"导入完成: stock_id={stock.id} playbook_id={row.id} "
            f"version={row.version} version_label={VERSION_LABEL}"
        )
        return {"imported": 1, "skipped": 0, "playbook_id": row.id}
    except SystemExit:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("导入失败，已回滚")
        raise
    finally:
        db.close()
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="东芯 v3.1 方案档案幂等导入")
    parser.add_argument(
        "--db",
        type=str,
        default=os.path.join(os.environ.get("DATA_DIR", "./data"), "panwatch.db"),
        help="sqlite 数据库路径（默认 $DATA_DIR/panwatch.db 或 ./data/panwatch.db）",
    )
    parser.add_argument("--symbol", type=str, default="688110", help="股票代码")
    parser.add_argument("--name", type=str, default="东芯股份", help="股票名称（自动创建时使用）")
    parser.add_argument("--market", type=str, default="CN", help="市场（默认 CN）")
    parser.add_argument("--stock-id", type=int, default=None, help="直接指定股票 ID（跳过 symbol 查找）")
    args = parser.parse_args()

    result = import_playbook(args.db, args.symbol, args.name, args.market, args.stock_id)
    import json

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
