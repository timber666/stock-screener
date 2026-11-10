"""交易回測的輸出：終端摘要、CSV 與 Plotly 圖表報表（RESEARCH_PLAN.md 4.2）。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from screener.backtest.report import _PAGE, _table
from screener.backtest.trades import RANDOM, AccountResult, Candidate, account_metrics, drawdown, equity_series
from screener.config import CostConfig, TradeConfig

RANDOM_LOW, RANDOM_HIGH = 5, 95     # 隨機挑選列出的範圍（百分位）


@dataclass(frozen=True)
class TradeBacktestResult:
    segment: str                                    # 觀察段或驗證段
    start: date
    end: date                                       # 最後一個可以買進的交易日；之後只賣出
    candidates: tuple[Candidate, ...]
    runs: tuple[AccountResult, ...]                 # 固定的挑選方式
    random_runs: dict[int, tuple[AccountResult, ...]]   # 檔數上限 → 各次隨機挑選
    benchmark: pd.Series                            # 全清單（全市場為當月股票池）每天等權的帳戶權益


def pick_label(pick: str, trade: TradeConfig, runs: int | None = None) -> str:
    if pick == "nearest_ma":
        return f"距 MA{trade.pick_ma} 最近"
    if pick == "volume_ratio":
        return "量比最大"
    if pick == RANDOM:
        return f"隨機（{runs} 次中位數）" if runs is not None else "隨機"
    return f"{pick} 最高"                       # 因子挑選（RESEARCH_PLAN.md 4.7）


# --- 統計 -------------------------------------------------------------------

def summary_table(result: TradeBacktestResult, trade: TradeConfig) -> pd.DataFrame:
    """每個（檔數上限, 挑選方式）一列；隨機為各次的中位數，總報酬另列 5%～95% 範圍。"""
    rows = []
    caps = sorted({run.settings.max_positions for run in result.runs} | set(result.random_runs))
    for cap in caps:
        for run in (r for r in result.runs if r.settings.max_positions == cap):
            rows.append({"max_positions": cap, "pick": run.settings.pick, **account_metrics(run, result.benchmark)})
        randoms = result.random_runs.get(cap, ())
        if randoms:
            metrics = pd.DataFrame([account_metrics(run, result.benchmark) for run in randoms])
            row = {"max_positions": cap, "pick": RANDOM, **metrics.median(numeric_only=True).to_dict(),
                   "total_return_low": float(np.percentile(metrics["total_return"], RANDOM_LOW)),
                   "total_return_high": float(np.percentile(metrics["total_return"], RANDOM_HIGH)),
                   "random_runs": len(randoms)}
            row["drawdown_recovered"] = bool(metrics["drawdown_recovered"].mean() >= 0.5)
            rows.append(row)
    return pd.DataFrame(rows)


# --- 終端摘要 ---------------------------------------------------------------

def format_summary(result: TradeBacktestResult, trade: TradeConfig, cost: CostConfig, *, universe_label: str,
                   rules_label: str, output_dir: Path, warnings: Sequence[str] = (),
                   baseline_label: str = "全清單") -> str:
    """universe_label：股票的範圍，例「觀察清單 290 檔」；baseline_label：基準的名稱（X 每天等權）。"""
    table = summary_table(result, trade)
    lines = [
        f"交易回測｜{result.segment} {result.start} ~ {result.end}｜初始資金 {trade.initial_capital:,.0f} 元｜"
        f"{universe_label}｜規則檔 {rules_label}",
        f"進場訊號 {len(result.candidates):,} 個（收漲停不成交）｜每天收盤先賣再買，單筆金額 = 當天權益 ÷ 檔數上限，買零股",
        f"成本：手續費 {cost.commission_pct:g}% × {cost.commission_discount:g}（每筆最低 {trade.min_commission:g} 元）、"
        f"證交稅 {cost.tax_pct:g}%；沒有計入滑價",
        *(f"⚠️ {warning}" for warning in warnings),
        "",
    ]
    rows = []
    for _, row in table.iterrows():
        label = pick_label(row["pick"], trade, int(row["random_runs"]) if row["pick"] == RANDOM else None)
        total = _pct(row["total_return"])
        if row["pick"] == RANDOM:
            total += f"（{_pct(row['total_return_low'])}～{_pct(row['total_return_high'])}）"
        rows.append([str(row["max_positions"]), label, total, _pct(row["cagr"]), _pct(row["max_drawdown"]),
                     f"{row['trades']:,.0f}", _pct(row["win_rate"], sign=False), _num(row["payoff"]),
                     _num(row["avg_positions"], 1), _pct(row["capital_usage"], sign=False),
                     _pct(row["flat_days"], sign=False), f"{row['rejected_full']:,.0f}", f"{row['costs']:,.0f}"])
    lines += _table(["上限", "挑選", "總報酬", "年化", "最大回撤", "交易", "勝率", "賺賠比", "平均檔數",
                     "資金使用", "空手日", "額滿放棄", "成本(元)"], rows)
    first = table.iloc[0]
    caps = {run.settings.max_positions for run in result.runs}
    picks = {run.settings.pick for run in result.runs}
    lines += ["", f"基準（{baseline_label}每天等權，不扣成本）：總報酬 {_pct(first['benchmark_return'])}、"
                  f"年化 {_pct(first['benchmark_cagr'])}、最大回撤 {_pct(first['benchmark_max_drawdown'])}",
              f"隨機挑選的總報酬括號內為 {RANDOM_LOW}%～{RANDOM_HIGH}% 範圍；固定的挑選方式落在範圍內，代表看不出挑選能力",
              f"這次比較 {len(caps)} 種上限 × {len(picks)} 種挑選方式 = {len(result.runs)} 組（另有隨機）："
              "比較得越多，碰巧最好的一組越容易高估",
              "",
              f"統計：{output_dir / 'summary.csv'}　交易明細：{output_dir / 'trades.csv'}　"
              f"每日帳戶：{output_dir / 'equity.csv'}　圖表：{output_dir / 'report.html'}"]
    return "\n".join(lines)


def _pct(value: float, *, sign: bool = True, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "-"
    return f"{value:+.{digits}f}%" if sign else f"{value:.0f}%"


def _num(value: float, digits: int = 2) -> str:
    return "-" if value is None or np.isnan(value) else f"{value:.{digits}f}"


# --- CSV --------------------------------------------------------------------

_SUMMARY_COLUMNS = {
    "max_positions": "上限", "pick": "挑選", "total_return": "總報酬(%)", "total_return_low": "總報酬5%(%)",
    "total_return_high": "總報酬95%(%)", "cagr": "年化(%)", "max_drawdown": "最大回撤(%)",
    "drawdown_days": "回撤天數", "drawdown_recovered": "已回復", "volatility": "波動(%)", "trades": "交易數",
    "win_rate": "勝率(%)", "payoff": "賺賠比", "avg_held_days": "平均持有天數", "max_losing_streak": "最長連虧",
    "avg_positions": "平均持股檔數", "capital_usage": "資金使用率(%)", "flat_days": "空手天數(%)",
    "rejected_full": "額滿放棄", "skipped_cash": "資金不足跳過", "costs": "成本(元)", "final_equity": "期末權益(元)",
    "random_runs": "隨機次數", "benchmark_return": "基準總報酬(%)", "benchmark_cagr": "基準年化(%)",
    "benchmark_max_drawdown": "基準最大回撤(%)",
}


def write_csv(result: TradeBacktestResult, trade: TradeConfig, output_dir: Path) -> None:
    """summary.csv、trades.csv、equity.csv；以 UTF-8 BOM 編碼，Excel 開啟中文不會亂碼。
    交易明細與每日帳戶只列固定的挑選方式，隨機挑選只列在統計。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    table = summary_table(result, trade)
    table["pick"] = [pick_label(p, trade) for p in table["pick"]]
    table = table.reindex(columns=list(_SUMMARY_COLUMNS)).rename(columns=_SUMMARY_COLUMNS)
    table.round(4).to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")

    trades, days = [], []
    for run in result.runs:
        label = {"上限": run.settings.max_positions, "挑選": pick_label(run.settings.pick, trade)}
        trades += [{**label, "代號": t.code, "名稱": t.name, "買進日": t.entry_day, "買進價": t.entry_price,
                    "股數": t.shares, "買進金額": round(t.buy_amount, 2), "買進手續費": round(t.buy_fee, 2),
                    "賣出日": t.exit_day, "賣出價": t.exit_price, "賣出金額": round(t.sell_amount, 2),
                    "賣出手續費": round(t.sell_fee, 2), "證交稅": round(t.tax, 2), "損益": round(t.pnl, 2),
                    "報酬率(%)": round(t.return_pct, 4), "持有天數": t.held_days, "賣出原因": t.exit_reason,
                    "跌停順延": t.delayed}
                   for t in sorted(run.trades, key=lambda t: (t.entry_day, t.code))]
        dd = drawdown(equity_series(run))
        days += [{**label, "日期": d.day, "現金": round(d.cash, 2), "持股市值": round(d.holdings_value, 2),
                  "權益": round(d.equity, 2), "回撤(%)": round(float(dd.loc[d.day]), 4), "持股檔數": d.positions,
                  "候選": d.candidates, "買進": d.bought, "額滿放棄": d.rejected_full, "資金不足跳過": d.skipped_cash,
                  "基準權益": round(float(result.benchmark.get(d.day, np.nan)), 2)}
                 for d in run.days]
    pd.DataFrame(trades).to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(days).to_csv(output_dir / "equity.csv", index=False, encoding="utf-8-sig")


# --- 圖表報表 ---------------------------------------------------------------

_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]   # 依序指派，不循環
_GRAY = "#8d8c87"


def write_html(result: TradeBacktestResult, trade: TradeConfig, path: Path, baseline_label: str = "全清單") -> None:
    """每個檔數上限兩張圖：帳戶權益（含隨機範圍與基準）、回撤。圖表程式庫內嵌，離線也能看。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    caps = sorted({run.settings.max_positions for run in result.runs} | set(result.random_runs))
    parts = []
    for cap in caps:
        runs = [run for run in result.runs if run.settings.max_positions == cap]
        randoms = result.random_runs.get(cap, ())
        for title, note, figure in (
            (f"帳戶權益：同時最多 {cap} 檔（元）",
             f"灰色區塊為隨機挑選 {len(randoms)} 次的 {RANDOM_LOW}%～{RANDOM_HIGH}% 範圍；點圖例可開關。",
             _equity_figure(runs, randoms, result.benchmark, trade)),
            (f"回撤：同時最多 {cap} 檔（%）", "相對之前最高點的回落。", _drawdown_figure(runs, result.benchmark, trade)),
        ):
            figure.update_layout(template="plotly_white", margin={"l": 50, "r": 20, "t": 30, "b": 40}, height=420,
                                 font={"family": "Microsoft JhengHei, PingFang TC, sans-serif"})
            html = figure.to_html(full_html=False, include_plotlyjs="inline" if not parts else False)
            parts.append(f"<h2>{title}</h2><p class='note'>{note}</p>{html}")
    header = (f"{result.segment} {result.start} ~ {result.end}｜初始資金 {trade.initial_capital:,.0f} 元｜"
              f"進場訊號 {len(result.candidates):,} 個｜基準為{baseline_label}每天等權（不扣成本）")
    path.write_text(_PAGE.format(title="交易回測報表", header=header, body="\n".join(parts)), encoding="utf-8")


def _equity_figure(runs: Sequence[AccountResult], randoms: Sequence[AccountResult], benchmark: pd.Series,
                   trade: TradeConfig) -> go.Figure:
    figure = go.Figure()
    if randoms:
        curves = pd.concat([equity_series(run) for run in randoms], axis=1).ffill()
        low, high = curves.quantile(RANDOM_LOW / 100, axis=1), curves.quantile(RANDOM_HIGH / 100, axis=1)
        figure.add_trace(go.Scatter(x=list(high.index), y=high, line={"width": 0}, hoverinfo="skip", showlegend=False))
        figure.add_trace(go.Scatter(x=list(low.index), y=low, line={"width": 0}, fill="tonexty",
                                    fillcolor="rgba(141,140,135,0.25)", name=f"隨機 {RANDOM_LOW}%～{RANDOM_HIGH}%"))
        median = curves.median(axis=1)
        figure.add_trace(go.Scatter(x=list(median.index), y=median, name="隨機中位數",
                                    line={"color": _GRAY, "dash": "dot"}))
    for color, run in zip(_COLORS, runs):
        equity = equity_series(run)
        figure.add_trace(go.Scatter(x=list(equity.index), y=equity, name=pick_label(run.settings.pick, trade),
                                    line={"color": color}))
    span = runs[0].days if runs else randoms[0].days
    bench = benchmark.reindex([d.day for d in span]).ffill()
    figure.add_trace(go.Scatter(x=list(bench.index), y=bench, name="基準", line={"color": "#1f1f1d", "dash": "dash"}))
    figure.update_yaxes(tickformat=",.0f")
    return figure


def _drawdown_figure(runs: Sequence[AccountResult], benchmark: pd.Series, trade: TradeConfig) -> go.Figure:
    figure = go.Figure()
    for color, run in zip(_COLORS, runs):
        dd = drawdown(equity_series(run))
        figure.add_trace(go.Scatter(x=list(dd.index), y=dd, name=pick_label(run.settings.pick, trade),
                                    line={"color": color}))
    if runs:
        bench = benchmark.reindex([d.day for d in runs[0].days]).ffill()
        dd = drawdown(bench)
        figure.add_trace(go.Scatter(x=list(dd.index), y=dd, name="基準", line={"color": "#1f1f1d", "dash": "dash"}))
    return figure


# --- 訊號分組（RESEARCH_PLAN.md 4.7）------------------------------------------

_GROUP_COLUMNS = {
    "factor": "因子", "group": "組", "count": "次數", "ret_mean": "持有平均(%)", "ret_median": "持有中位數(%)",
    "excess_mean": "超額平均(%)", "win_rate": "持有勝率(%)", "trade_mean": "照出場規則平均(%)",
    "trade_median": "照出場規則中位數(%)", "trade_win_rate": "照出場規則勝率(%)",
}


def group_label(group: object, quantiles: int) -> str:
    if group == "top_minus_bottom":
        return f"第 {quantiles} 組 − 第 1 組"
    if group == 0:
        return "沒有值"
    if group == -1:
        return "不分組"
    return f"第 {group} 組"


def format_signal_groups(groups: pd.DataFrame, *, segment: str, horizon: int, quantiles: int, round_trip_pct: float,
                         path: Path) -> str:
    """終端摘要的一段：每個因子 × 組一列。"""
    lines = [f"■ 訊號分組（{segment}的全部進場訊號，不受檔數上限影響）：依進場日的因子值分 {quantiles} 組，"
             f"分組用當天股票池內有值的股票（同 M9），第 {quantiles} 組因子值最大",
             f"  持有 {horizon} 日：進場日收盤 → 第 {horizon} 根 K 棒收盤，超額 = 減去同一天的基準；"
             f"照出場規則：交易回測的每筆報酬，扣來回成本 {round_trip_pct:.2f}%"]
    rows = []
    for _, row in groups.iterrows():
        diff = row["group"] == "top_minus_bottom"
        rows.append([row["factor"], group_label(row["group"], quantiles), "" if diff else f"{row['count']:,.0f}",
                     _pct(row["ret_mean"], digits=2), "" if diff else _pct(row["ret_median"], digits=2),
                     _pct(row["excess_mean"], digits=2), "" if diff else _pct(row["win_rate"], sign=False),
                     _pct(row["trade_mean"], digits=2), "" if diff else _pct(row["trade_median"], digits=2),
                     "" if diff else _pct(row["trade_win_rate"], sign=False)])
    lines += _table(["因子", "組", "次數", "持有平均", "中位數", "超額", "勝率", "照出場規則平均", "中位數", "勝率"],
                    rows, indent=2)
    lines.append(f"  明細：{path}")
    return "\n".join(lines)


def write_signal_groups(groups: pd.DataFrame, *, segment: str, quantiles: int, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = groups.reindex(columns=list(_GROUP_COLUMNS)).copy()
    table["group"] = [group_label(g, quantiles) for g in table["group"]]
    table.insert(0, "段", segment)
    table.rename(columns=_GROUP_COLUMNS).round(4).to_csv(path, index=False, encoding="utf-8-sig")
