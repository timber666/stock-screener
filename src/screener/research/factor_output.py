"""單因子研究的輸出：終端摘要、CSV 與 Plotly 圖表報表（RESEARCH_PLAN.md 4.4）。"""

from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from screener.backtest.report import _PAGE, _table
from screener.config import FactorsConfig
from screener.research.factor_eval import CLOSE, NEXT_OPEN
from screener.research.factor_study import EXCLUDE, INCLUDE, INDUSTRY, OBSERVE, RAW, SEGMENTS, VALIDATE, StudyResult

_SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")   # 依序指派，不循環
_DIVERGING = [[0.0, "#E24B4A"], [0.5, "#f0efec"], [1.0, "#2a78d6"]]     # 紅 = 低於股票池平均、藍 = 高於
_BLUE = "#2a78d6"
_GRID = "#898781"


# --- 終端摘要 ---------------------------------------------------------------

def _fmt(value: float, digits: int = 2, sign: bool = True) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:+.{digits}f}" if sign else f"{value:.{digits}f}"


def _pick(summary: pd.DataFrame, **conditions: object) -> pd.DataFrame:
    selected = summary
    for column, value in conditions.items():
        selected = selected[selected[column] == value]
    return selected


def _row(table: pd.DataFrame, factor: str) -> pd.Series | None:
    rows = table[table["factor"] == factor]
    return rows.iloc[0] if len(rows) else None


def format_summary(result: StudyResult, config: FactorsConfig, validation_start, output_dir: Path) -> str:
    horizon = config.summary_horizon
    continuous = [f for f in result.factors if not f.is_categorical]
    lines = [
        f"單因子研究 {result.start} ~ {result.end}｜觀察段至 {validation_start - timedelta(days=1)}，"
        f"驗證段 {validation_start} 起｜因子 {len(result.factors)} 個",
        f"股票池：當月的流動性股票池（research.yaml 的 market.universe）｜報酬以還原權息的收盤價計算｜來回成本 {result.round_trip_pct:.3f}%",
        f"IC = 當天因子值與持有 {horizon} 日報酬的 Spearman 相關；t 值每 {horizon} 天取一次；分位數為 {config.quantiles} 組，"
        "排除當天收漲跌停的股票；超額 = 最高組 − 股票池平均；扣成本 = 超額 − 換手率 × 來回成本",
    ]
    if continuous:
        for segment in SEGMENTS:
            lines += ["", f"■ {segment}（持有 {horizon} 日，未中性）"]
            ic_rows = _pick(result.summary, horizon=horizon, segment=segment, neutral=RAW, entry=CLOSE, limit=INCLUDE)
            quantile_rows = _pick(result.summary, horizon=horizon, segment=segment, neutral=RAW, entry=CLOSE,
                                  limit=EXCLUDE)
            rows = []
            for factor in continuous:
                ic, quantile = _row(ic_rows, factor.id), _row(quantile_rows, factor.id)
                if ic is None or quantile is None:
                    continue
                rows.append([factor.id, factor.category, _fmt(ic["ic_mean"], 3), _fmt(ic["icir"], 2), _fmt(ic["ic_t"], 1),
                             _fmt(quantile["top_bottom"]), _fmt(quantile["top_excess"]),
                             _fmt(quantile["turnover"] * 100, 0, sign=False), _fmt(quantile["top_net"])])
            lines += _table(["因子", "類別", "IC", "ICIR", "t", "高減低(%)", "超額(%)", "換手(%)", "扣成本(%)"], rows)

        lines += ["", f"■ 產業中性前後的 IC（持有 {horizon} 日）；隔日開盤進場的 IC 為對照（觀察段）"]
        rows = []
        for factor in continuous:
            cells = [factor.id]
            for segment in SEGMENTS:
                for neutral in (RAW, INDUSTRY):
                    row = _row(_pick(result.summary, horizon=horizon, segment=segment, neutral=neutral, entry=CLOSE,
                                     limit=INCLUDE), factor.id)
                    cells.append(_fmt(row["ic_mean"], 3) if row is not None else "—")
            row = _row(_pick(result.summary, horizon=horizon, segment=OBSERVE, neutral=RAW, entry=NEXT_OPEN,
                             limit=INCLUDE), factor.id)
            cells.append(_fmt(row["ic_mean"], 3) if row is not None else "—")
            rows.append(cells)
        lines += _table(["因子", "觀察段未中性", "觀察段中性", "驗證段未中性", "驗證段中性", "觀察段隔日開盤"], rows)

    categorical = [f for f in result.factors if f.is_categorical]
    if categorical:
        lines += ["", f"■ 類別因子：持有 {horizon} 日的平均報酬／超額（%，排除收漲跌停）"]
        rows = []
        for factor in categorical:
            assert factor.groups is not None
            for group in factor.groups.values():
                cells = [factor.id, group]
                for segment in SEGMENTS:
                    selected = _pick(result.groups, factor=factor.id, group=group, horizon=horizon, segment=segment,
                                     limit=EXCLUDE)
                    if len(selected):
                        row = selected.iloc[0]
                        cells += [f"{row['stocks']:.0f}", _fmt(row["mean"]), _fmt(row["excess"])]
                    else:
                        cells += ["—", "—", "—"]
                rows.append(cells)
        lines += _table(["因子", "組", "觀察段檔數", "平均", "超額", "驗證段檔數", "平均", "超額"], rows)

    lines += ["", f"完整統計：{output_dir / 'summary.csv'}　每天的 IC：{output_dir / 'ic.csv'}　"
                  f"衰減：{output_dir / 'decay.csv'}　類別因子：{output_dir / 'groups.csv'}　"
                  f"圖表：{output_dir / 'report.html'}",
              "因子報告只描述統計，不構成投資建議。"]
    return "\n".join(lines)


# --- CSV --------------------------------------------------------------------

_SUMMARY_COLUMNS = {"factor": "因子", "category": "類別", "horizon": "持有天數", "segment": "段", "neutral": "中性前後",
                    "entry": "進場", "limit": "漲跌停", "stocks": "平均檔數", "days": "IC 天數", "ic_mean": "IC 平均",
                    "ic_std": "IC 標準差", "icir": "ICIR", "ic_t": "IC t 值", "ic_t_days": "t 值樣本天數",
                    "ic_positive": "IC>0 比例"}
_GROUP_COLUMNS = {"factor": "因子", "category": "類別", "group": "組", "horizon": "持有天數", "segment": "段", "limit": "漲跌停",
                  "days": "天數", "stocks": "平均檔數", "mean": "平均報酬(%)", "excess": "超額(%)"}


def write_csv(result: StudyResult, config: FactorsConfig, output_dir: Path) -> None:
    """summary.csv、ic.csv、decay.csv、groups.csv；以 UTF-8 BOM 編碼，Excel 開啟中文不會亂碼。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    quantile_columns = {f"q{i}": f"第{i}組(%)" for i in range(1, config.quantiles + 1)}
    extra = {"top_bottom": "最高減最低(%)", "top_excess": "最高組超額(%)", "turnover": "換手率", "top_net": "扣成本後(%)"}
    columns = {**_SUMMARY_COLUMNS, **quantile_columns, **extra}
    summary = result.summary.reindex(columns=list(columns)).assign(entry=lambda t: t["entry"].map(
        {CLOSE: "收盤 t", NEXT_OPEN: "隔日開盤"}))
    summary.rename(columns=columns).round(4).to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    ic = result.ic.copy()
    ic.columns = [f"{factor}|{neutral}|{horizon}日" for factor, neutral, horizon in ic.columns]
    ic.round(4).to_csv(output_dir / "ic.csv", index_label="日期", encoding="utf-8-sig")
    result.decay.round(4).to_csv(output_dir / "decay.csv", index=False, encoding="utf-8-sig")
    groups = result.groups.reindex(columns=list(_GROUP_COLUMNS)).rename(columns=_GROUP_COLUMNS)
    groups.round(4).to_csv(output_dir / "groups.csv", index=False, encoding="utf-8-sig")


# --- 圖表報表 ---------------------------------------------------------------

def write_html(result: StudyResult, config: FactorsConfig, validation_start, path: Path) -> None:
    """累積 IC、分位數熱圖、高減低比較、衰減、中性前後、進場時點對照。圖表程式庫內嵌，離線也能看。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    horizon = config.summary_horizon
    continuous = [f for f in result.factors if not f.is_categorical]
    figures: list[tuple[str, str, go.Figure]] = []
    if continuous:
        figures += [
            (f"累積 Rank IC（持有 {horizon} 日，未中性）", "持續往同一方向累積的因子，預測力比較穩定；虛線為驗證段起點。"
             "各小圖依類別分開。IC 為負的因子方向相反（值小的股票表現較好）。",
             _cumulative_ic(result, config, validation_start)),
            (f"各組的平均報酬（持有 {horizon} 日，%，減去當天股票池平均，排除收漲跌停）",
             f"第 1 組因子值最小，第 {config.quantiles} 組最大。藍 = 高於股票池平均，紅 = 低於。"
             "兩端顏色相反、逐組漸變的因子，分組效果最明確。", _quantile_heatmap(result, config)),
            (f"觀察段 vs 驗證段：最高組減最低組（持有 {horizon} 日，%，排除收漲跌停）",
             "方向相反或大幅縮小的因子，可能是觀察段碰巧有效。", _segment_bars(result, config)),
            ("IC 衰減（觀察段，未中性）", "看因子適合多長的持有期：線越平，預測力維持越久。依類別分開。",
             _decay(result, config)),
            (f"產業中性前後的平均 IC（持有 {horizon} 日）", "中性後變小的因子，預測力大多來自選到特定產業。",
             _neutral_bars(result, config)),
            (f"進場時點：收盤 t 與隔日開盤（持有 {horizon} 日，觀察段）",
             "兩者差很多的因子，表示收盤到隔日開盤的這一段（跳空）影響很大（R23）。", _entry_bars(result, config)),
        ]
    parts = []
    for i, (title, note, figure) in enumerate(figures):
        figure.update_layout(template="plotly_white", margin={"l": 40, "r": 20, "t": 30, "b": 40},
                             font={"family": "Microsoft JhengHei, PingFang TC, sans-serif"})
        html = figure.to_html(full_html=False, include_plotlyjs="inline" if i == 0 else False)
        parts.append(f"<h2>{title}</h2><p class='note'>{note}</p>{html}")
    header = (f"{result.start} ~ {result.end}｜觀察段至 {validation_start - timedelta(days=1)}，驗證段 {validation_start} 起｜"
              f"來回成本 {result.round_trip_pct:.3f}%")
    path.write_text(_PAGE.format(title="單因子研究報表", header=header, body="\n".join(parts)), encoding="utf-8")


def _categories(result: StudyResult) -> dict[str, list[str]]:
    categories: dict[str, list[str]] = {}
    for factor in result.factors:
        if not factor.is_categorical:
            categories.setdefault(factor.category, []).append(factor.id)
    return categories


def _small_multiples(categories: dict[str, list[str]], *, row_height: int) -> tuple[go.Figure, list[tuple[int, int]]]:
    names = list(categories)
    cols = 2
    rows = (len(names) + 1) // 2
    figure = make_subplots(rows=rows, cols=cols, subplot_titles=names, horizontal_spacing=0.08,
                           vertical_spacing=0.3 / max(rows, 1))
    figure.update_layout(height=row_height * rows, hovermode="x unified", showlegend=True)
    return figure, [((i // cols) + 1, (i % cols) + 1) for i in range(len(names))]


def _cumulative_ic(result: StudyResult, config: FactorsConfig, validation_start) -> go.Figure:
    categories = _categories(result)
    figure, positions = _small_multiples(categories, row_height=280)
    for (category, factor_ids), (row, col) in zip(categories.items(), positions, strict=True):
        for i, factor in enumerate(factor_ids):
            column = (factor, RAW, config.summary_horizon)
            if column not in result.ic.columns:
                continue
            series = result.ic[column].fillna(0).cumsum()
            figure.add_trace(go.Scatter(x=result.ic.index, y=series, mode="lines", name=factor, line={"color": _SERIES[i], "width": 2},
                                        legendgroup=category, legendgrouptitle_text=category,
                                        hovertemplate="%{x}<br>累積 IC %{y:+.1f}<extra>" + factor + "</extra>"),
                             row=row, col=col)
        figure.add_vline(x=pd.Timestamp(validation_start).value / 1e6, line_dash="dash", line_color=_GRID, row=row, col=col)
    return figure


def _quantile_heatmap(result: StudyResult, config: FactorsConfig) -> go.Figure:
    continuous = [f.id for f in result.factors if not f.is_categorical]
    labels = [f"第{i}組" for i in range(1, config.quantiles + 1)]
    figure = make_subplots(rows=1, cols=2, subplot_titles=SEGMENTS, shared_yaxes=True, horizontal_spacing=0.03)
    rows = {segment: _pick(result.summary, horizon=config.summary_horizon, segment=segment, neutral=RAW, entry=CLOSE,
                           limit=EXCLUDE) for segment in SEGMENTS}
    values = {segment: [[_group_excess(_row(rows[segment], factor), config, i) for i in range(1, config.quantiles + 1)]
                        for factor in continuous] for segment in SEGMENTS}
    limit = max(0.1, float(np.nanmax(np.abs(np.array(list(values.values()), dtype=float)))))
    for col, segment in enumerate(SEGMENTS, start=1):
        z = values[segment]
        figure.add_trace(go.Heatmap(z=z, x=labels, y=continuous, colorscale=_DIVERGING, zmid=0, zmin=-limit, zmax=limit,
                                    showscale=col == 2,
                                    text=[[("" if np.isnan(v) else f"{v:+.2f}") for v in row] for row in z],
                                    texttemplate="%{text}",
                                    hovertemplate="%{y}<br>%{x}：%{z:+.2f}%<extra>" + segment + "</extra>"),
                         row=1, col=col)
    figure.update_yaxes(autorange="reversed")
    figure.update_layout(height=80 + 36 * len(continuous))
    return figure


def _group_excess(row: pd.Series | None, config: FactorsConfig, group: int) -> float:
    if row is None:
        return np.nan
    average = np.mean([row[f"q{i}"] for i in range(1, config.quantiles + 1)])
    return float(row[f"q{group}"] - average)


def _segment_bars(result: StudyResult, config: FactorsConfig) -> go.Figure:
    continuous = [f.id for f in result.factors if not f.is_categorical]
    figure = go.Figure()
    for segment, style in ((OBSERVE, {"marker_color": _BLUE}),
                           (VALIDATE, {"marker_color": "rgba(42,120,214,0.15)",
                                       "marker_line": {"color": _BLUE, "width": 1.5}})):
        rows = _pick(result.summary, horizon=config.summary_horizon, segment=segment, neutral=RAW, entry=CLOSE,
                     limit=EXCLUDE)
        y = [_value(_row(rows, factor), "top_bottom") for factor in continuous]
        figure.add_trace(go.Bar(name=segment, x=continuous, y=y,
                                hovertemplate="%{x}<br>%{y:+.2f}%<extra>" + segment + "</extra>", **style))
    figure.update_layout(barmode="group", height=380, yaxis_title="最高組減最低組（%）", legend={"orientation": "h"})
    return figure


def _value(row: pd.Series | None, column: str) -> float:
    return float(row[column]) if row is not None and not pd.isna(row[column]) else np.nan


def _decay(result: StudyResult, config: FactorsConfig) -> go.Figure:
    categories = _categories(result)
    figure, positions = _small_multiples(categories, row_height=260)
    table = result.decay[result.decay["segment"] == OBSERVE] if len(result.decay) else result.decay
    for (category, factor_ids), (row, col) in zip(categories.items(), positions, strict=True):
        for i, factor in enumerate(factor_ids):
            rows = table[table["factor"] == factor]
            figure.add_trace(go.Scatter(x=rows["horizon"], y=rows["ic_mean"], mode="lines+markers", name=factor,
                                        line={"color": _SERIES[i], "width": 2}, marker={"size": 8},
                                        legendgroup=category, legendgrouptitle_text=category,
                                        hovertemplate="持有 %{x} 日<br>平均 IC %{y:+.3f}<extra>" + factor + "</extra>"),
                             row=row, col=col)
    return figure


def _neutral_bars(result: StudyResult, config: FactorsConfig) -> go.Figure:
    continuous = [f.id for f in result.factors if not f.is_categorical]
    figure = go.Figure()
    for neutral, color in ((RAW, _BLUE), (INDUSTRY, _SERIES[1])):
        rows = _pick(result.summary, horizon=config.summary_horizon, segment=OBSERVE, neutral=neutral, entry=CLOSE,
                     limit=INCLUDE)
        figure.add_trace(go.Bar(name=neutral, x=continuous, y=[_value(_row(rows, f), "ic_mean") for f in continuous],
                                marker_color=color,
                                hovertemplate="%{x}<br>%{y:+.3f}<extra>" + neutral + "</extra>"))
    figure.update_layout(barmode="group", height=360, yaxis_title="平均 IC（觀察段）", legend={"orientation": "h"})
    return figure


def _entry_bars(result: StudyResult, config: FactorsConfig) -> go.Figure:
    continuous = [f.id for f in result.factors if not f.is_categorical]
    figure = go.Figure()
    for entry, label, color in ((CLOSE, "收盤 t", _BLUE), (NEXT_OPEN, "隔日開盤", _SERIES[1])):
        rows = _pick(result.summary, horizon=config.summary_horizon, segment=OBSERVE, neutral=RAW, entry=entry,
                     limit=INCLUDE)
        figure.add_trace(go.Bar(name=label, x=continuous, y=[_value(_row(rows, f), "ic_mean") for f in continuous],
                                marker_color=color,
                                hovertemplate="%{x}<br>%{y:+.3f}<extra>" + label + "</extra>"))
    figure.update_layout(barmode="group", height=360, yaxis_title="平均 IC（觀察段）", legend={"orientation": "h"})
    return figure
