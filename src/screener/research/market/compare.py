"""全市場股票池與觀察清單並列（RESEARCH_PLAN.md 4.3「報告」）：量化觀察清單的選擇偏差。

兩邊用相同的規則、相同的期間（compare_start 起，到兩個資料庫都有日 K 的最後一天），觀察段與驗證段的切分
也相同。訊號回測比較基準（每天等權、還原權息）的年化報酬與各規則的平均報酬、超額報酬；交易回測比較
相同檔數上限與挑選方式的帳戶結果。
"""

from collections.abc import Sequence
from datetime import date, timedelta
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from screener.backtest.report import _pct, _table
from screener.backtest.signals import StockHistory, dedupe
from screener.backtest.stats import OBSERVE, VALIDATE, select, summarize
from screener.backtest.trades import AccountResult, account_metrics, benchmark_series
from screener.config import BacktestConfig

if TYPE_CHECKING:
    from screener.jobs.backtest import BacktestResult

MARKET, WATCHLIST = "全市場", "觀察清單"
BENCHMARK = "基準（每天等權）年化"


def segments(backtest: BacktestConfig, start: date, end: date) -> list[tuple[str, date, date]]:
    """(段, 第一天, 最後一天)；期間沒有涵蓋的段不列出。"""
    observe_end = backtest.validation_start - timedelta(days=1)
    spans = [(OBSERVE, start, min(end, observe_end)), (VALIDATE, max(start, backtest.validation_start), end)]
    return [(name, first, last) for name, first, last in spans if first <= last]


def benchmark_cagr(histories: Sequence[StockHistory], calendar: Sequence[date]) -> float:
    """每天等權的報酬（還原權息，同交易回測的基準）累積後的年化報酬（%）。"""
    if len(calendar) < 2:
        return np.nan
    equity = benchmark_series(histories, calendar, 1.0)
    years = (calendar[-1] - calendar[0]).days / 365.25
    return float((equity.iloc[-1] ** (1 / years) - 1) * 100)


def signal_comparison(market: "BacktestResult", watchlist: "BacktestResult", market_histories: Sequence[StockHistory],
                      watchlist_histories: Sequence[StockHistory], backtest: BacktestConfig, *, start: date, end: date,
                      horizon: int) -> pd.DataFrame:
    """每段一列基準、每條規則每段一列：次數、勝率、平均報酬、平均超額（去重、排除漲跌停鎖死，持有 horizon 日）。

    去重在期間內重新計算，兩邊的訊號都從 start 起算。
    """
    calendar = sorted({day for h in market_histories for day in h.bars.index if start <= day <= end})
    rows = []
    for segment, first, last in segments(backtest, start, end):
        days = [d for d in calendar if first <= d <= last]
        rows.append({"item": BENCHMARK, "side": "", "segment": segment,
                     "market_mean": benchmark_cagr(market_histories, days),
                     "watchlist_mean": benchmark_cagr(watchlist_histories, days)})
    summaries = {}
    for label, result in ((MARKET, market), (WATCHLIST, watchlist)):
        signals = result.signals[(result.signals["date"] >= start) & (result.signals["date"] <= end)].copy()
        signals["kept"] = dedupe(signals, backtest.dedupe_days) if len(signals) else pd.Series(dtype=bool)
        signals = signals.astype({"kept": bool})
        summaries[label] = summarize(signals, [horizon], backtest.cost.round_trip_pct, list(market.rule_names))
    for rule_id, name in market.rule_names.items():
        for segment, _, _ in segments(backtest, start, end):
            row = {"item": name, "side": market.rule_sides[rule_id], "segment": segment}
            for label, prefix in ((MARKET, "market"), (WATCHLIST, "watchlist")):
                stats = select(summaries[label], rule_id=rule_id, segment=segment, horizon=horizon).iloc[0]
                row |= {f"{prefix}_count": int(stats["count"]), f"{prefix}_win_rate": stats["win_rate"],
                        f"{prefix}_mean": stats["mean"], f"{prefix}_excess": stats["excess"]}
            rows.append(row)
    table = pd.DataFrame(rows, columns=["item", "side", "segment", "market_count", "market_win_rate", "market_mean",
                                        "market_excess", "watchlist_count", "watchlist_win_rate", "watchlist_mean",
                                        "watchlist_excess"])
    return table.astype({"market_count": "Int64", "watchlist_count": "Int64"})     # 基準列沒有次數


def format_signal_comparison(table: pd.DataFrame, *, start: date, end: date, horizon: int) -> str:
    lines = [f"■ 與觀察清單並列（{start} ~ {end}，相同規則；持有 {horizon} 日，去重、排除漲跌停鎖死）",
             "  基準為每天等權的年化報酬（還原權息、不扣成本）；平均、超額為訊號的平均報酬與減去同日基準後的平均超額"]
    rows = []
    for r in table.itertuples():
        if r.item == BENCHMARK:
            rows.append([r.item, r.segment, "", _pct(r.market_mean), "", "", _pct(r.watchlist_mean), "", ""])
        else:
            rows.append([r.item, r.segment, f"{int(r.market_count):,}", _pct(r.market_mean), _pct(r.market_excess),
                         f"{int(r.watchlist_count):,}", _pct(r.watchlist_mean), _pct(r.watchlist_excess),
                         _pct(r.watchlist_excess - r.market_excess)])
    lines += _table(["項目", "段", "全市場 次數", "平均", "超額", "觀察清單 次數", "平均", "超額", "超額差"], rows, indent=2)
    lines.append("  超額差 = 觀察清單 − 全市場；基準的差距即觀察清單的選擇偏差（清單是 2026-10 依熱門族群挑的）")
    return "\n".join(lines)


def trade_comparison(runs: Sequence[tuple[str, int, str, AccountResult, pd.Series]]) -> pd.DataFrame:
    """runs：(股票範圍, 檔數上限, 挑選方式, 帳戶結果, 基準)。每個帳戶一列。"""
    rows = []
    for universe, cap, pick, result, benchmark in runs:
        metrics = account_metrics(result, benchmark)
        rows.append({"universe": universe, "max_positions": cap, "pick": pick,
                     **{key: metrics[key] for key in ("total_return", "cagr", "max_drawdown", "trades", "win_rate",
                                                      "payoff", "benchmark_return", "benchmark_cagr",
                                                      "benchmark_max_drawdown")}})
    return pd.DataFrame(rows)


def format_trade_comparison(table: pd.DataFrame, labels: dict[str, str], *, start: date, end: date) -> str:
    lines = [f"■ 與觀察清單並列（{start} ~ {end}，各自從初始資金開始，相同的檔數上限與挑選方式）"]
    rows = []
    for r in table.itertuples():
        rows.append([r.universe, str(r.max_positions), labels.get(r.pick, r.pick), _pct(r.total_return), _pct(r.cagr),
                     _pct(r.max_drawdown), f"{r.trades:,.0f}", _pct(r.win_rate, sign=False),
                     f"{r.payoff:.2f}" if not np.isnan(r.payoff) else "-"])
    lines += _table(["範圍", "上限", "挑選", "總報酬", "年化", "最大回撤", "交易", "勝率", "賺賠比"], rows, indent=2)
    for universe, group in table.groupby("universe", sort=False):
        first = group.iloc[0]
        lines.append(f"  基準（{universe}每天等權，不扣成本）：總報酬 {_pct(first['benchmark_return'])}、"
                     f"年化 {_pct(first['benchmark_cagr'])}、最大回撤 {_pct(first['benchmark_max_drawdown'])}")
    return "\n".join(lines)
