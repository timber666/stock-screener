"""盤中 job：合成暫定 K 棒、執行規則引擎並推播（PROJECT_PLAN.md 7.2）。

目前只完成回放模式（M3）：以資料庫中該日（含）以前的日 K 建立 StockContext，該日 K 棒視為
「今日」。盤中掃描於 M4、Discord 推播於 M5 實作。
"""

import logging
import sys
from collections.abc import Sequence
from datetime import date
from typing import TextIO

from screener.analysis.context import bars_to_frame, build_context
from screener.config import AppConfig, TrendConfig
from screener.data.base import PriceStore
from screener.engine import Engine
from screener.jobs import JobFailed, open_existing_store
from screener.models import Hit, ScanReport, Side, StockContext, StockInfo
from screener.notify.message import format_report

logger = logging.getLogger(__name__)


def run(config: AppConfig, *, dry_run: bool, replay: date | None, out: TextIO | None = None) -> None:
    if replay is None:
        raise NotImplementedError("盤中掃描尚未實作（M4）；目前可用 --replay YYYY-MM-DD --dry-run 以歷史資料重跑")
    if not dry_run:
        raise NotImplementedError("Discord 推播尚未實作（M5）；請加上 --dry-run，結果會印在終端")

    engine = Engine.from_config(config.rules)
    with open_existing_store(config.settings.db_path) as store:
        report = replay_scan(config.watchlist.stocks, store=store, engine=engine,
                             trend_config=config.rules.trend, day=replay)
    (out or sys.stdout).write(format_report(report, config.watchlist.industries) + "\n")


def replay_scan(
    stocks: Sequence[StockInfo],
    *,
    store: PriceStore,
    engine: Engine,
    trend_config: TrendConfig,
    day: date,
) -> ScanReport:
    """以 day 的日 K 當作「今日」評估每一檔；當天沒有日 K 的股票不評估。"""
    hits: list[Hit] = []
    not_evaluated: list[str] = []
    for stock in stocks:
        bars = [bar for bar in store.get_bars(stock.code) if bar.date <= day]
        if not bars or bars[-1].date != day:
            not_evaluated.append(stock.code)
            continue
        ctx = build_context(stock, bars_to_frame(bars), trend_config, is_tentative=False)
        signals = engine.evaluate(ctx)
        for side in Side:
            side_signals = tuple(signal for signal in signals if signal.side is side)
            if side_signals:
                hits.append(Hit(ctx=ctx, side=side, signals=side_signals,
                                change_pct=_change_from_previous_close(ctx)))
        for signal in signals:
            logger.info("%s %s %s｜%s %s：%s", stock.code, stock.name, ctx.trend.label,
                        signal.side.label, signal.rule_name, signal.detail)

    if len(not_evaluated) == len(stocks):
        raise JobFailed(f"資料庫沒有任何股票 {day} 的日 K：非交易日，或尚未執行 update")
    logger.info("回放 %s：觀察 %d 檔，評估 %d 檔，觸發多方 %d 檔、空方 %d 檔",
                day, len(stocks), len(stocks) - len(not_evaluated),
                sum(hit.side is Side.LONG for hit in hits), sum(hit.side is Side.SHORT for hit in hits))
    return ScanReport(day=day, is_replay=True, observed=len(stocks), hits=tuple(hits),
                      not_evaluated=tuple(not_evaluated))


def _change_from_previous_close(ctx: StockContext) -> float | None:
    """回放沒有 MIS 參考價，漲跌幅改用前一日收盤計算（第 11 節）。"""
    if ctx.yesterday is None:
        return None
    return float((ctx.today["close"] - ctx.yesterday["close"]) / ctx.yesterday["close"] * 100)
