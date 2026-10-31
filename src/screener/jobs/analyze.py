"""工具指令：以資料庫的日 K 輸出指定股票的轉折點與趨勢，供檢查分析結果（PROJECT_PLAN.md 第 12 節）。"""

import math
import sys
from collections.abc import Sequence
from datetime import date
from typing import TextIO

from screener.analysis.context import bars_to_frame, build_context, pivot_window
from screener.analysis.trend import basic_trend, closes_since_last_pivot, last_pivot
from screener.config import AppConfig, TrendConfig
from screener.jobs import JobFailed, open_existing_store
from screener.models import StockContext, Trend


def run(config: AppConfig, codes: Sequence[str], *, as_of: date | None, out: TextIO | None = None) -> None:
    """as_of 那天（預設為資料庫最後一筆）的 K 棒視為「今日」，即該日收盤後的狀態。預設輸出到 stdout。"""
    out = out or sys.stdout
    stocks = {stock.code: stock for stock in config.watchlist.stocks}
    unknown = [code for code in codes if code not in stocks]
    if unknown:
        raise JobFailed(f"觀察清單沒有這些代號：{'、'.join(unknown)}")
    missing: list[str] = []
    with open_existing_store(config.settings.db_path) as store:
        for code in codes:
            bars = [bar for bar in store.get_bars(code) if as_of is None or bar.date <= as_of]
            if not bars or (as_of is not None and bars[-1].date != as_of):
                missing.append(code)
                continue
            ctx = build_context(stocks[code], bars_to_frame(bars), config.rules.trend, is_tentative=False)
            out.write(format_report(ctx, config.rules.trend) + "\n\n")

    if missing:
        when = f" {as_of} " if as_of else ""
        raise JobFailed(
            f"資料庫沒有這些股票{when}的日 K（非交易日、當天沒有成交，或尚未執行 update）：{'、'.join(missing)}"
        )


def format_report(ctx: StockContext, config: TrendConfig) -> str:
    info, bars = ctx.info, ctx.bars
    window = pivot_window(bars, config)
    lines = [
        f"{info.code} {info.name}｜{info.industry}",
        f"  今日 {bars.index[-1]}：收盤 {_price(bars['close'].iloc[-1])}，"
        f"MA{config.pivot_ma} {_price(ctx.ma(config.pivot_ma).iloc[-1])}",
        f"  轉折點（{window[0]} ~ {window[-1]}，{len(window)} 根 K 棒）："
        if len(window) else "  轉折點：沒有 K 棒",
    ]
    lines += [f"    {p.date}  {p.label}  {_price(p.close)}" for p in ctx.pivots] or ["    （沒有已確認的轉折點）"]

    basic = basic_trend(ctx.pivots)
    lines.append(f"  基本趨勢：{basic.label}（{_basic_detail(ctx)}）")
    lines.append(f"  趨勢：{ctx.trend.label}{_break_detail(ctx, basic)}")
    return "\n".join(lines)


def _basic_detail(ctx: StockContext) -> str:
    peaks = [p.close for p in ctx.pivots if p.kind == "peak"]
    troughs = [p.close for p in ctx.pivots if p.kind == "trough"]
    if len(peaks) < 2 or len(troughs) < 2:
        return f"已確認的頭 {len(peaks)} 個、底 {len(troughs)} 個，各需要至少 2 個"
    return (f"頭 {_price(peaks[-2])} → {_price(peaks[-1])}，"
            f"底 {_price(troughs[-2])} → {_price(troughs[-1])}")


def _break_detail(ctx: StockContext, basic: Trend) -> str:
    if ctx.trend is basic:
        return ""
    since = closes_since_last_pivot(ctx.pivots, ctx.bars["close"])
    after = f"{ctx.pivots[-1].date} 之後"
    if basic is Trend.BULL:
        return f"（破位修正：{after}最低收盤 {_price(since.min())} < 前底 {_price(last_pivot(ctx.pivots, 'trough').close)}）"
    return f"（破位修正：{after}最高收盤 {_price(since.max())} > 前頭 {_price(last_pivot(ctx.pivots, 'peak').close)}）"


def _price(value: float) -> str:
    return "—" if math.isnan(value) else f"{value:.2f}"
