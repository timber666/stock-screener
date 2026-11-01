"""訊號回測的輸出：終端摘要、CSV 與 Plotly 圖表報表（PROJECT_PLAN.md 14.1、D36）。"""

import unicodedata
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from screener.backtest.stats import ALL, EXCLUDE, OBSERVE, VALIDATE, select
from screener.config import BacktestConfig
from screener.models import Side

if TYPE_CHECKING:
    from screener.jobs.backtest import BacktestResult

_SIDE_LABELS = {side.value: side.label for side in Side}


# --- 終端摘要 ---------------------------------------------------------------

def format_summary(result: "BacktestResult", backtest: BacktestConfig, horizon: int, observed: int,
                   rules_label: str, output_dir: Path) -> str:
    signals, summary = result.signals, result.summary
    kept = int(signals["kept"].sum()) if len(signals) else 0
    lines = [
        f"訊號回測 {result.start} ~ {result.end}｜觀察段至 {backtest.validation_start - timedelta(days=1)}，"
        f"驗證段 {backtest.validation_start} 起",
        f"觀察清單 {observed} 檔｜訊號 {len(signals):,} 個（去重後 {kept:,}）｜規則檔 {rules_label}｜"
        f"來回成本 {backtest.cost.round_trip_pct:.3f}%",
        "報酬以還原權息的收盤價計算；勝率 = 方向報酬 > 0 的比例；超額 = 減去同日觀察清單的平均報酬（空方同樣反向）",
        "",
        f"■ 持有 {horizon} 日（去重，排除漲跌停鎖死）",
    ]
    rows = []
    for rule_id, name in result.rule_names.items():
        for segment in (OBSERVE, VALIDATE):
            s = _one(select(summary, rule_id=rule_id, segment=segment, horizon=horizon))
            rows.append([name if segment == OBSERVE else "", _SIDE_LABELS[result.rule_sides[rule_id]]
                         if segment == OBSERVE else "", segment, f"{s['count']:,}", _pct(s["win_rate"], sign=False),
                         _pct(s["mean"]), _pct(s["median"]), _pct(s["excess"]), _pct(s["net"])])
    lines += _table(["規則", "方向", "段", "次數", "勝率", "平均", "中位數", "超額", "扣成本"], rows)

    lines += ["", "■ 各持有天數的平均超額報酬（全期間，去重，排除漲跌停鎖死）"]
    rows = [[name] + [_pct(_one(select(summary, rule_id=rule_id, segment=ALL, horizon=n))["excess"])
                      for n in backtest.horizons] for rule_id, name in result.rule_names.items()]
    lines += _table(["規則"] + [f"{n}日" for n in backtest.horizons], rows)

    lines += ["", "■ 標記"]
    total = max(len(signals), 1)
    locked, gaps = int(signals["limit_locked"].sum()), int(signals["gap_event"].sum())
    lines.append(f"漲跌停鎖死：{locked:,} 個訊號（{locked / total:.1%}），上面兩張表已排除")
    lines.append(f"跳空事件（計算區間內單次價格調整 ≥ {backtest.gap_flag_pct:g}%）：{gaps:,} 個訊號（{gaps / total:.1%}）；"
                 f"排除後持有 {horizon} 日的平均超額報酬（全期間，去重）：")
    rows = []
    for rule_id, name in result.rule_names.items():
        before = _one(select(summary, rule_id=rule_id, segment=ALL, horizon=horizon))
        after = _one(select(summary, rule_id=rule_id, segment=ALL, horizon=horizon, gap_event=EXCLUDE))
        rows.append([name, f"{before['count']:,}", _pct(before["excess"]), f"{after['count']:,}", _pct(after["excess"])])
    lines += _table(["規則", "含跳空 次數", "超額", "排除後 次數", "超額"], rows, indent=2)

    lines += ["", "■ 還原價對照（D34）：以還原權息後的價格判斷訊號",
              f"  訊號數為全部訊號；超額為持有 {horizon} 日的平均（全期間，去重，排除漲跌停鎖死）"]
    keys = ["date", "code", "rule_id"]
    raw_keys = set(map(tuple, signals[keys].to_numpy())) if len(signals) else set()
    adj_keys = set(map(tuple, result.adjusted_signals[keys].to_numpy())) if len(result.adjusted_signals) else set()
    rows = []
    for rule_id, name in result.rule_names.items():
        raw = {k for k in raw_keys if k[2] == rule_id}
        adj = {k for k in adj_keys if k[2] == rule_id}
        a = _one(select(summary, rule_id=rule_id, segment=ALL, horizon=horizon))
        b = _one(select(result.adjusted_summary, rule_id=rule_id, segment=ALL, horizon=horizon))
        rows.append([name, f"{len(raw):,}", f"{len(adj):,}", f"{len(raw & adj):,}", _pct(a["excess"]), _pct(b["excess"])])
    lines += _table(["規則", "未還原", "還原", "兩者皆有", "超額（未還原）", "超額（還原）"], rows, indent=2)

    lines += ["", f"明細：{output_dir / 'signals.csv'}（還原價對照：{output_dir / 'signals_adjusted.csv'}）",
              f"完整統計：{output_dir / 'summary.csv'}　圖表：{output_dir / 'report.html'}"]
    return "\n".join(lines)


def _one(rows: pd.DataFrame) -> pd.Series:
    return rows.iloc[0]


def _pct(value: float, *, sign: bool = True) -> str:
    if value is None or np.isnan(value):
        return "-"
    return f"{value:+.2f}%" if sign else f"{value:.0f}%"


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], indent: int = 0) -> list[str]:
    """第一欄靠左、其餘靠右；中文字以兩格計算寬度。"""
    widths = [max(_width(str(row[i])) for row in [header, *rows]) for i in range(len(header))]

    def line(cells: Sequence[str]) -> str:
        parts = [str(c) + " " * (widths[0] - _width(str(c))) if i == 0 else " " * (widths[i] - _width(str(c))) + str(c)
                 for i, c in enumerate(cells)]
        return " " * indent + "  ".join(parts).rstrip()

    return [line(header)] + [line(row) for row in rows]


# --- CSV --------------------------------------------------------------------

_SIGNAL_COLUMNS = {"date": "日期", "code": "代號", "name": "名稱", "group": "族群", "rule_name": "規則",
                   "side": "方向", "trend": "趨勢", "entry_price": "進場價（收盤）", "segment": "段",
                   "kept": "去重後計入", "limit_locked": "漲跌停鎖死", "gap_event": "跳空事件"}
_RETURN_COLUMNS = {"ret": "報酬", "base": "基準", "excess": "超額", "mfe": "最大有利", "mae": "最大不利"}
_SUMMARY_COLUMNS = {"price": "判斷價格", "rule_name": "規則", "side": "方向", "segment": "段", "sample": "樣本",
                    "limit_locked": "漲跌停鎖死", "gap_event": "跳空事件", "horizon": "持有天數", "count": "次數",
                    "win_rate": "勝率(%)", "mean": "平均報酬(%)", "median": "中位數(%)",
                    "excess": "平均超額報酬(%)", "net": "扣成本後平均(%)"}


def write_csv(result: "BacktestResult", backtest: BacktestConfig, output_dir: Path) -> None:
    """signals.csv、signals_adjusted.csv、summary.csv；以 UTF-8 BOM 編碼，Excel 開啟中文不會亂碼。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, signals in (("signals.csv", result.signals), ("signals_adjusted.csv", result.adjusted_signals)):
        _signal_table(signals, backtest.horizons).to_csv(output_dir / name, index=False, encoding="utf-8-sig")
    summaries = []
    for price, summary in (("未還原", result.summary), ("還原", result.adjusted_summary)):
        table = summary.assign(price=price, rule_name=summary["rule_id"].map(result.rule_names),
                               side=summary["rule_id"].map(result.rule_sides).map(_SIDE_LABELS))
        summaries.append(table[list(_SUMMARY_COLUMNS)].rename(columns=_SUMMARY_COLUMNS))
    pd.concat(summaries).round(4).to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")


def _signal_table(signals: pd.DataFrame, horizons: Sequence[int]) -> pd.DataFrame:
    table = signals[list(_SIGNAL_COLUMNS)].assign(side=signals["side"].map(_SIDE_LABELS))
    table = table.rename(columns=_SIGNAL_COLUMNS)
    for n in horizons:
        for key, label in _RETURN_COLUMNS.items():
            table[f"{label}{n}日(%)"] = signals[f"{key}_{n}"].round(4)
    return table


# --- 圖表報表 ---------------------------------------------------------------

_DIVERGING = [[0.0, "#E24B4A"], [0.5, "#f0efec"], [1.0, "#2a78d6"]]     # 紅 = 劣於基準、藍 = 優於基準
_BLUE = "#2a78d6"


def write_html(result: "BacktestResult", backtest: BacktestConfig, horizon: int, path: Path) -> None:
    """四張圖：總覽熱圖、觀察段 vs 驗證段、累積超額報酬、最大有利／不利幅度。圖表程式庫內嵌，離線也能看。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [f"{name}（{_SIDE_LABELS[result.rule_sides[rule_id]]}）" for rule_id, name in result.rule_names.items()]
    figures = [
        ("總覽：平均超額報酬（%），規則 × 持有天數", "去重、排除漲跌停鎖死。藍 = 優於同日觀察清單平均，紅 = 劣於。",
         _heatmap(result, backtest, names)),
        (f"觀察段 vs 驗證段：持有 {horizon} 日的平均超額報酬（%）",
         "驗證段明顯變差的規則，可能是參數貼合了過去。", _segment_bars(result, horizon, names)),
        (f"累積超額報酬：依訊號日期累加（持有 {horizon} 日，%）",
         "去重、排除漲跌停鎖死；點圖例可開關規則。看優勢是穩定累積，還是集中在某段行情。虛線為驗證段起點。",
         _cumulative(result, backtest, horizon)),
        (f"最大有利／不利幅度：持有 {horizon} 日內（%）",
         "每個訊號一點（去重、排除漲跌停鎖死），以還原後的最高、最低價相對進場價計算，供日後設定停損停利參考。",
         _excursions(result, horizon)),
    ]
    parts = []
    for i, (title, note, figure) in enumerate(figures):
        figure.update_layout(template="plotly_white", margin={"l": 40, "r": 20, "t": 30, "b": 40},
                             font={"family": "Microsoft JhengHei, PingFang TC, sans-serif"})
        html = figure.to_html(full_html=False, include_plotlyjs="inline" if i == 0 else False)
        parts.append(f"<h2>{title}</h2><p class='note'>{note}</p>{html}")
    kept = int(result.signals["kept"].sum()) if len(result.signals) else 0
    header = (f"{result.start} ~ {result.end}｜觀察段至 {backtest.validation_start - timedelta(days=1)}，"
              f"驗證段 {backtest.validation_start} 起｜訊號 {len(result.signals):,} 個（去重後 {kept:,}）｜"
              f"報酬以還原權息的收盤價計算，超額 = 減去同日觀察清單的平均報酬")
    path.write_text(_PAGE.format(header=header, body="\n".join(parts)), encoding="utf-8")


def _heatmap(result: "BacktestResult", backtest: BacktestConfig, names: list[str]) -> go.Figure:
    labels = [f"{n}日" for n in backtest.horizons]
    figure = make_subplots(rows=1, cols=2, subplot_titles=(OBSERVE, VALIDATE), shared_yaxes=True,
                           horizontal_spacing=0.03)
    values = {segment: [[_one(select(result.summary, rule_id=rule_id, segment=segment, horizon=n))["excess"]
                         for n in backtest.horizons] for rule_id in result.rule_names] for segment in (OBSERVE, VALIDATE)}
    limit = max(0.1, float(np.nanmax(np.abs(np.array(list(values.values()), dtype=float)))))
    for col, segment in enumerate((OBSERVE, VALIDATE), start=1):
        z = values[segment]
        figure.add_trace(go.Heatmap(
            z=z, x=labels, y=names, colorscale=_DIVERGING, zmid=0, zmin=-limit, zmax=limit, showscale=col == 2,
            text=[[("" if np.isnan(v) else f"{v:+.2f}") for v in row] for row in z], texttemplate="%{text}",
            hovertemplate="%{y}<br>持有 %{x}：%{z:+.2f}%<extra>" + segment + "</extra>",
        ), row=1, col=col)
    figure.update_yaxes(autorange="reversed")
    figure.update_layout(height=80 + 45 * len(names))
    return figure


def _segment_bars(result: "BacktestResult", horizon: int, names: list[str]) -> go.Figure:
    figure = go.Figure()
    for segment, style in ((OBSERVE, {"marker_color": _BLUE}),
                           (VALIDATE, {"marker_color": "rgba(42,120,214,0.15)",
                                       "marker_line": {"color": _BLUE, "width": 1.5}})):
        rows = [_one(select(result.summary, rule_id=rule_id, segment=segment, horizon=horizon))
                for rule_id in result.rule_names]
        figure.add_trace(go.Bar(name=segment, x=names, y=[r["excess"] for r in rows],
                                customdata=[r["count"] for r in rows],
                                hovertemplate="%{x}<br>%{y:+.2f}%（%{customdata:,} 次）<extra>" + segment + "</extra>",
                                **style))
    figure.update_layout(barmode="group", height=380, yaxis_title="平均超額報酬（%）", legend={"orientation": "h"})
    return figure


def _counted(result: "BacktestResult") -> pd.DataFrame:
    signals = result.signals
    return signals[signals["kept"] & ~signals["limit_locked"]]


def _cumulative(result: "BacktestResult", backtest: BacktestConfig, horizon: int) -> go.Figure:
    figure = go.Figure()
    signals = _counted(result)
    for rule_id, name in result.rule_names.items():
        rows = signals[signals["rule_id"] == rule_id].dropna(subset=[f"excess_{horizon}"]).sort_values("date")
        figure.add_trace(go.Scatter(x=rows["date"], y=rows[f"excess_{horizon}"].cumsum(), mode="lines",
                                    name=f"{name}（{_SIDE_LABELS[result.rule_sides[rule_id]]}）",
                                    hovertemplate="%{x}<br>累積 %{y:+.1f}%<extra>%{fullData.name}</extra>"))
    figure.add_vline(x=pd.Timestamp(backtest.validation_start).value / 1e6, line_dash="dash", line_color="#898781")
    figure.update_layout(height=420, yaxis_title="累積超額報酬（%）", hovermode="x unified")
    return figure


def _excursions(result: "BacktestResult", horizon: int) -> go.Figure:
    figure = go.Figure()
    signals = _counted(result)
    for rule_id, name in result.rule_names.items():
        rows = signals[signals["rule_id"] == rule_id].dropna(subset=[f"mfe_{horizon}"])
        figure.add_trace(go.Scattergl(
            x=rows[f"mae_{horizon}"], y=rows[f"mfe_{horizon}"], mode="markers", marker={"size": 4, "opacity": 0.5},
            name=f"{name}（{_SIDE_LABELS[result.rule_sides[rule_id]]}）",
            customdata=np.stack([rows["date"].astype(str), rows["code"] + " " + rows["name"],
                                 rows[f"ret_{horizon}"].round(2)], axis=-1) if len(rows) else None,
            hovertemplate="%{customdata[0]} %{customdata[1]}<br>最大不利 %{x:.1f}%、最大有利 %{y:.1f}%"
                          "<br>持有到期 %{customdata[2]}%<extra>%{fullData.name}</extra>",
        ))
    figure.update_layout(height=480, xaxis_title="最大不利幅度（%）", yaxis_title="最大有利幅度（%）")
    return figure


_PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>訊號回測報表</title>
<style>
body {{ font-family: "Microsoft JhengHei", "PingFang TC", sans-serif; max-width: 1100px; margin: 24px auto;
       padding: 0 16px; color: #1f1f1d; background: #ffffff; }}
h1 {{ font-size: 22px; font-weight: 500; }} h2 {{ font-size: 17px; font-weight: 500; margin-top: 36px; }}
.note, .header {{ color: #5f5e5a; font-size: 14px; }}
</style></head>
<body><h1>訊號回測報表</h1><p class="header">{header}</p>
{body}
<p class="note">本報表僅供技術研究，不構成投資建議。</p></body></html>
"""
