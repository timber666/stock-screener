"""組合回測與模擬組合的輸出：終端摘要、CSV、Plotly 報表與月報訊息（RESEARCH_PLAN.md 4.6）。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from screener.backtest.report import _DIVERGING, _PAGE, _table
from screener.backtest.trades import drawdown
from screener.config import TOP_FACTOR, TOP_VALUE, CostConfig, PaperPortfolio, PortfolioConfig
from screener.notify.message import Section, pack_sections
from screener.research.factor_output import _SERIES
from screener.research.portfolio import (CLOSE, OPEN, AccountResult, Rebalance, RebalanceLog, account_metrics,
                                         series_metrics, yearly_returns)
from screener.research.signal_report_output import DISCLAIMER

RANDOM_LOW, RANDOM_HIGH = 5, 95     # 隨機挑選列出的範圍（百分位）
PICK_LABELS = {TOP_FACTOR: "因子值最高", TOP_VALUE: "最高組成交金額最大"}
EXECUTION_LABELS = {OPEN: "隔日開盤", CLOSE: "訊號日收盤"}
_INDEX_LABEL = "報酬指數"
_UNIVERSE_LABEL = "股票池等權"
_INK, _GRAY = "#1f1f1d", "#8d8c87"


@dataclass(frozen=True)
class CombinationRun:
    factor: str
    top_n: int
    pick: str
    result: AccountResult


@dataclass(frozen=True)
class RandomRuns:
    """基準③：最高組隨機 N 檔。只保留每天的權益與統計，交易明細不保留。"""

    factor: str
    top_n: int
    equity: np.ndarray              # 列為各次隨機、欄為交易日
    metrics: pd.DataFrame           # 每次隨機一列（account_metrics）


@dataclass(frozen=True)
class PortfolioBacktestResult:
    segment: str                    # 觀察段或驗證段
    start: date                     # 第一個訊號日
    end: date
    rebalances: int                 # 換股次數
    runs: tuple[CombinationRun, ...]
    randoms: dict[tuple[str, int], RandomRuns]     # （因子, 檔數）→ 隨機
    index: pd.Series                # 基準①：報酬指數累積成的帳戶
    universe: pd.Series             # 基準②：當月股票池每天等權


def _combo_label(factor: str, top_n: int, pick: str) -> str:
    return f"{factor} {top_n} 檔 {PICK_LABELS[pick]}"


# --- 統計 -------------------------------------------------------------------

def summary_table(result: PortfolioBacktestResult) -> pd.DataFrame:
    """每種組合、每種成交價一列；隨機只有隔日開盤成交，總報酬列中位數與 5%～95%。"""
    rows = []
    for run in result.runs:
        row = {"factor": run.factor, "top_n": run.top_n, "pick": run.pick, "execution": run.result.execution,
               **account_metrics(run.result, result.index, result.universe)}
        random = result.randoms.get((run.factor, run.top_n))
        if random is not None and run.result.execution == OPEN:
            returns = random.metrics["total_return"]
            row |= {"random_median": float(returns.median()), "random_low": float(np.percentile(returns, RANDOM_LOW)),
                    "random_high": float(np.percentile(returns, RANDOM_HIGH))}
        rows.append(row)
    return pd.DataFrame(rows)


# --- 終端摘要 ---------------------------------------------------------------

def _pct(value: float) -> str:
    return "-" if value is None or np.isnan(value) else f"{value:+.1f}%"


def format_summary(result: PortfolioBacktestResult, portfolio: PortfolioConfig, cost: CostConfig, min_commission: float,
                   output_dir: Path) -> str:
    table = summary_table(result)
    closes = {(r.factor, r.top_n, r.pick): r for r in table.itertuples() if r.execution == CLOSE}
    lines = [
        f"組合回測｜{result.segment} {result.start} ~ {result.end}｜初始資金 {portfolio.initial_capital:,.0f} 元｜"
        f"換股 {result.rebalances} 次",
        "每月 D 那一週的最後一個交易日收盤算分數，下一個交易日開盤成交；先賣再買，留在名單的不調整，買零股",
        f"成本：手續費 {cost.commission_pct:g}% × {cost.commission_discount:g}（每筆最低 {min_commission:g} 元）、"
        f"證交稅 {cost.tax_pct:g}%；沒有計入滑價",
        "",
    ]
    rows = []
    for row in table[table["execution"] == OPEN].itertuples():
        random = (f"{_pct(row.random_median)}（{_pct(row.random_low)}～{_pct(row.random_high)}）"
                  if hasattr(row, "random_median") and not np.isnan(row.random_median) else "-")
        close = closes.get((row.factor, row.top_n, row.pick))
        rows.append([f"{row.factor}", str(row.top_n), PICK_LABELS[row.pick], _pct(row.total_return), _pct(row.cagr),
                     _pct(row.max_drawdown), f"{row.turnover:.1f}", f"{row.trades:,.0f}", f"{row.costs:,.0f}",
                     f"{row.unbuyable:,.0f}", f"{row.deferred:,.0f}", f"{row.final_equity:,.0f}", random,
                     _pct(close.total_return) if close is not None else "-"])
    lines += _table(["因子", "檔數", "挑法", "總報酬", "年化", "最大回撤", "換手", "交易", "成本(元)", "遞補", "順延",
                     "期末權益", f"隨機中位數（{RANDOM_LOW}%～{RANDOM_HIGH}%）", "收盤成交"], rows)
    first = table.iloc[0]
    lines += ["",
              f"基準①{_INDEX_LABEL}：總報酬 {_pct(first['index_return'])}、年化 {_pct(first['index_cagr'])}、"
              f"最大回撤 {_pct(first['index_max_drawdown'])}",
              f"基準②{_UNIVERSE_LABEL}（每天再平衡、不扣成本）：總報酬 {_pct(first['universe_return'])}、"
              f"年化 {_pct(first['universe_cagr'])}、最大回撤 {_pct(first['universe_max_drawdown'])}",
              f"基準③為最高組隨機 N 檔 {portfolio.random_runs} 次，規則與組合相同；落在範圍內代表看不出挑選能力",
              "只描述統計，不構成投資建議",
              "",
              f"統計：{output_dir / 'summary.csv'}　交易明細：{output_dir / 'trades.csv'}　"
              f"每日帳戶：{output_dir / 'equity.csv'}　圖表：{output_dir / 'report.html'}"]
    return "\n".join(lines)


# --- CSV --------------------------------------------------------------------

_SUMMARY_COLUMNS = {
    "factor": "因子", "top_n": "檔數", "pick": "挑法", "execution": "成交價", "total_return": "總報酬(%)", "cagr": "年化(%)",
    "max_drawdown": "最大回撤(%)", "volatility": "波動(%)", "turnover": "換手(倍/年)", "trades": "交易數", "win_rate": "勝率(%)",
    "costs": "成本(元)", "unbuyable": "買不到遞補", "deferred": "賣不掉順延", "skipped_cash": "資金不足略過",
    "avg_positions": "平均持股檔數", "final_equity": "期末權益(元)", "index_return": "報酬指數(%)", "index_cagr": "報酬指數年化(%)",
    "index_max_drawdown": "報酬指數最大回撤(%)", "universe_return": "股票池等權(%)", "universe_cagr": "股票池等權年化(%)",
    "universe_max_drawdown": "股票池等權最大回撤(%)", "random_median": "隨機中位數(%)", "random_low": "隨機5%(%)",
    "random_high": "隨機95%(%)",
}


def _trade_rows(result: AccountResult, label: Mapping[str, object], names: Mapping[str, str]) -> list[dict[str, object]]:
    return [{**label, "代號": t.code, "名稱": names.get(t.code, ""), "買進日": t.buy_day, "買進價": t.buy_price, "股數": t.shares,
             "買進金額": round(t.buy_amount, 2), "買進手續費": round(t.buy_fee, 2), "賣出日": t.sell_day, "賣出價": t.sell_price,
             "賣出金額": round(t.sell_amount, 2), "賣出手續費": round(t.sell_fee, 2), "證交稅": round(t.tax, 2),
             "損益": round(t.pnl, 2), "賣出原因": t.reason, "順延天數": t.deferred_days}
            for t in sorted(result.trades, key=lambda t: (t.buy_day, t.code))]


def _equity_rows(result: AccountResult, label: Mapping[str, object], index: pd.Series,
                 universe: pd.Series) -> list[dict[str, object]]:
    equity = result.equity
    dd = drawdown(equity)
    return [{**label, "日期": day, "現金": round(float(result.cash[i]), 2), "持股市值": round(float(result.holdings[i]), 2),
             "權益": round(float(equity.iloc[i]), 2), "回撤(%)": round(float(dd.iloc[i]), 4), "持股檔數": int(result.positions[i]),
             "成本": round(float(result.costs[i]), 2), _INDEX_LABEL: round(float(index.get(day, np.nan)), 2),
             _UNIVERSE_LABEL: round(float(universe.get(day, np.nan)), 2)}
            for i, day in enumerate(result.days)]


def write_csv(result: PortfolioBacktestResult, names: Mapping[str, str], output_dir: Path) -> None:
    """summary.csv、trades.csv、equity.csv；以 UTF-8 BOM 編碼，Excel 開啟中文不會亂碼。隨機只列在統計。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    table = summary_table(result)
    table["pick"] = [PICK_LABELS[p] for p in table["pick"]]
    table["execution"] = [EXECUTION_LABELS[e] for e in table["execution"]]
    table = table.reindex(columns=list(_SUMMARY_COLUMNS)).rename(columns=_SUMMARY_COLUMNS)
    table.round(4).to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    trades, days = [], []
    for run in result.runs:
        label = {"因子": run.factor, "檔數": run.top_n, "挑法": PICK_LABELS[run.pick], "成交價": EXECUTION_LABELS[run.result.execution]}
        trades += _trade_rows(run.result, label, names)
        days += _equity_rows(run.result, label, result.index, result.universe)
    pd.DataFrame(trades).to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(days).to_csv(output_dir / "equity.csv", index=False, encoding="utf-8-sig")


# --- 圖表報表 ---------------------------------------------------------------

def write_html(result: PortfolioBacktestResult, portfolio: PortfolioConfig, path: Path) -> None:
    """每組（因子, 檔數）兩張圖：帳戶權益（含隨機範圍與基準）、回撤；最後一張各年報酬的熱圖。圖表程式庫內嵌，離線也能看。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pairs = sorted({(run.factor, run.top_n) for run in result.runs}, key=lambda p: (portfolio.factors.index(p[0]), p[1]))
    figures: list[tuple[str, str, go.Figure, int]] = []
    for factor, top_n in pairs:
        runs = [r for r in result.runs if (r.factor, r.top_n) == (factor, top_n) and r.result.execution == OPEN]
        random = result.randoms.get((factor, top_n))
        band = f"灰色區塊為隨機 {random.equity.shape[0]} 次的 {RANDOM_LOW}%～{RANDOM_HIGH}% 範圍；" if random is not None else ""
        figures.append((f"帳戶權益：{factor} {top_n} 檔（元）", f"{band}點圖例可開關。",
                        _equity_figure(runs, random, result.index, result.universe), 420))
        figures.append((f"回撤：{factor} {top_n} 檔（%）", "相對之前最高點的回落。",
                        _drawdown_figure(runs, result.index, result.universe), 320))
    heatmap = _yearly_heatmap(result)
    figures.append(("各年報酬（%）", "隔日開盤成交；最下面兩列為基準。", heatmap, max(360, 26 * len(heatmap.data[0].y) + 80)))
    parts = []
    for i, (title, note, figure, height) in enumerate(figures):
        figure.update_layout(template="plotly_white", margin={"l": 50, "r": 20, "t": 30, "b": 40}, height=height,
                             font={"family": "Microsoft JhengHei, PingFang TC, sans-serif"})
        html = figure.to_html(full_html=False, include_plotlyjs="inline" if i == 0 else False)
        parts.append(f"<h2>{title}</h2><p class='note'>{note}</p>{html}")
    header = (f"{result.segment} {result.start} ~ {result.end}｜初始資金 {portfolio.initial_capital:,.0f} 元｜"
              f"換股 {result.rebalances} 次｜基準為{_INDEX_LABEL}、{_UNIVERSE_LABEL}（不扣成本）")
    path.write_text(_PAGE.format(title="組合回測報表", header=header, body="\n".join(parts)), encoding="utf-8")


def _equity_figure(runs: Sequence[CombinationRun], random: RandomRuns | None, index: pd.Series,
                   universe: pd.Series) -> go.Figure:
    figure = go.Figure()
    days = list(runs[0].result.days)
    if random is not None and random.equity.size:
        low, high = np.percentile(random.equity, [RANDOM_LOW, RANDOM_HIGH], axis=0)
        figure.add_trace(go.Scatter(x=days, y=high, line={"width": 0}, hoverinfo="skip", showlegend=False))
        figure.add_trace(go.Scatter(x=days, y=low, line={"width": 0}, fill="tonexty", fillcolor="rgba(141,140,135,0.25)",
                                    name=f"隨機 {RANDOM_LOW}%～{RANDOM_HIGH}%"))
        figure.add_trace(go.Scatter(x=days, y=np.median(random.equity, axis=0), name="隨機中位數",
                                    line={"color": _GRAY, "dash": "dot"}))
    for color, run in zip(_SERIES, runs):
        figure.add_trace(go.Scatter(x=days, y=run.result.equity.to_numpy(), name=PICK_LABELS[run.pick], line={"color": color}))
    figure.add_trace(go.Scatter(x=days, y=index.reindex(days).ffill().to_numpy(), name=_INDEX_LABEL,
                                line={"color": _INK, "dash": "dash"}))
    figure.add_trace(go.Scatter(x=days, y=universe.reindex(days).ffill().to_numpy(), name=_UNIVERSE_LABEL,
                                line={"color": _SERIES[2], "dash": "dash"}))
    figure.update_yaxes(tickformat=",.0f")
    return figure


def _drawdown_figure(runs: Sequence[CombinationRun], index: pd.Series, universe: pd.Series) -> go.Figure:
    figure = go.Figure()
    days = list(runs[0].result.days)
    for color, run in zip(_SERIES, runs):
        figure.add_trace(go.Scatter(x=days, y=drawdown(run.result.equity).to_numpy(), name=PICK_LABELS[run.pick],
                                    line={"color": color}))
    figure.add_trace(go.Scatter(x=days, y=drawdown(index.reindex(days).ffill()).to_numpy(), name=_INDEX_LABEL,
                                line={"color": _INK, "dash": "dash"}))
    figure.add_trace(go.Scatter(x=days, y=drawdown(universe.reindex(days).ffill()).to_numpy(), name=_UNIVERSE_LABEL,
                                line={"color": _SERIES[2], "dash": "dash"}))
    return figure


def _yearly_heatmap(result: PortfolioBacktestResult) -> go.Figure:
    runs = [run for run in result.runs if run.result.execution == OPEN]
    days = list(runs[0].result.days)
    series = {_combo_label(run.factor, run.top_n, run.pick): yearly_returns(run.result.equity) for run in runs}
    series[_INDEX_LABEL] = yearly_returns(result.index.reindex(days).ffill())
    series[_UNIVERSE_LABEL] = yearly_returns(result.universe.reindex(days).ffill())
    table = pd.DataFrame(series).T
    limit = float(np.nanmax(np.abs(table.to_numpy(dtype=float))))
    z = table.to_numpy(dtype=float)
    return go.Figure(go.Heatmap(z=z[::-1], x=[str(year) for year in table.columns], y=list(table.index[::-1]),
                                colorscale=_DIVERGING, zmid=0, zmin=-limit, zmax=limit,
                                text=[[("" if np.isnan(v) else f"{v:+.0f}") for v in row] for row in z[::-1]],
                                texttemplate="%{text}", hovertemplate="%{y} %{x}：%{z:+.1f}%<extra></extra>"))


# --- 模擬組合的月報 ----------------------------------------------------------

@dataclass(frozen=True)
class TrackReport:
    latest: Rebalance
    previous: Rebalance | None      # 上一次換股；第一份名單時為 None
    result: AccountResult           # 從第一個訊號日模擬到本次訊號日收盤
    index: pd.Series
    universe: pd.Series
    kept: tuple[str, ...]
    bought: tuple[str, ...]
    sold: tuple[str, ...]
    skipped: bool                   # 有因子值的股票不足，當月不換股


def _names(codes: Sequence[str], names: Mapping[str, str]) -> str:
    return "、".join(f"{code} {names.get(code, '')}".rstrip() for code in codes) or "（無）"


def _period_return(series: pd.Series, start: date, end: date) -> float:
    return (float(series.loc[end]) / float(series.loc[start]) - 1) * 100


def _last_log(report: TrackReport) -> RebalanceLog | None:
    if report.previous is None:
        return None
    return next((log for log in report.result.logs if log.signal == report.previous.signal), None)


def format_track_messages(report: TrackReport, track: PaperPortfolio, names: Mapping[str, str], csv_dir: Path | None,
                          limit: int) -> list[str]:
    latest = report.latest
    start = report.result.days[0]
    equity = report.result.equity
    if latest.trade is None:
        when = "下一個交易日開盤成交（週六產生名單）"
    else:
        when = f"{latest.trade:%m/%d} 開盤成交（補跑）"
    head = [f"【模擬組合】{latest.signal:%Y-%m} 換股｜{track.factor} {track.top_n} 檔，{PICK_LABELS[track.pick]}",
            f"訊號日 {latest.signal:%m/%d} 收盤｜名單：{when}；不下單，僅追蹤"]
    if report.previous is None:
        head.append("第一份名單，尚無績效")
    head.append("")
    sections = [Section(head=tuple(head))]

    if report.previous is not None:
        previous = report.previous.signal
        log = _last_log(report)
        costs = float(report.result.costs[[previous < day <= latest.signal for day in report.result.days]].sum())
        sections.append(Section(head=(
            f"━━ 上月（{previous:%m/%d} 收盤 → {latest.signal:%m/%d} 收盤）━━",
            f"組合 {_signed(_period_return(equity, previous, latest.signal))}｜"
            f"{_INDEX_LABEL} {_signed(_period_return(report.index, previous, latest.signal))}｜"
            f"{_UNIVERSE_LABEL} {_signed(_period_return(report.universe, previous, latest.signal))}",
            f"買不到遞補 {len(log.unbuyable) if log else 0} 次｜賣不掉順延 {len(log.deferred) if log else 0} 次｜成本 {costs:,.0f} 元",
            "")))

    title = ("━━ 本月不換股（有因子值的股票不足）━━" if report.skipped else
             f"━━ 本月換股（賣 {len(report.sold)}、買 {len(report.bought)}、留任 {len(report.kept)}）━━")
    items = (((f"賣出：{_names(report.sold, names)}",), (f"買進：{_names(report.bought, names)}",),
              (f"留任：{_names(report.kept, names)}",)))
    sections.append(Section(head=(title,), items=items, tail=("",), resume_inside=(f"{title}（續）",)))

    sections.append(Section(head=(
        f"━━ 累積（{start:%Y-%m-%d} 起）━━",
        f"權益 {float(equity.iloc[-1]):,.0f} 元（{_signed(series_metrics(equity)['return'])}）｜"
        f"{_INDEX_LABEL} {_signed(_period_return(report.index, start, latest.signal))}｜"
        f"{_UNIVERSE_LABEL} {_signed(_period_return(report.universe, start, latest.signal))}｜"
        f"最大回撤 {_signed(float(drawdown(equity).min()))}",
        *([f"明細：{csv_dir.as_posix()}"] if csv_dir is not None else []),
        DISCLAIMER)))
    return pack_sections(sections, limit)


def _signed(value: float) -> str:
    return f"{value:+.2f}%"


def write_track_csv(report: TrackReport, names: Mapping[str, str], ranking: Sequence[str], output_dir: Path) -> None:
    """equity.csv、trades.csv 與本次名單 <訊號日>.csv。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_equity_rows(report.result, {}, report.index, report.universe)).to_csv(
        output_dir / "equity.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(_trade_rows(report.result, {}, names)).to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    actions = {**{code: "留任" for code in report.kept}, **{code: "買進" for code in report.bought},
               **{code: "賣出" for code in report.sold}}
    rows = [{"排名": rank, "代號": code, "名稱": names.get(code, ""), "動作": actions.get(code, "")}
            for rank, code in enumerate(ranking, start=1)]
    pd.DataFrame(rows).to_csv(output_dir / f"{report.latest.signal}.csv", index=False, encoding="utf-8-sig")
