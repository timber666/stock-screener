"""訊號成績單的輸出：Discord 訊息與明細 CSV（RESEARCH_PLAN.md 4.1）。

訊息超過單則上限時依段落拆成多則，沿用每日推播的拆分方式（notify.message.pack_sections）。
"""

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from screener.models import Side
from screener.notify.message import Section, pack_sections
from screener.research.signal_report import (APPEARED, NO_CLOSE_BAR, NOT_EVALUATED, VANISHED, WEEK, SignalReport,
                                             consistency, extremes, performance)

DISCLAIMER = "僅為統計紀錄，不構成投資建議"


def format_messages(report: SignalReport, *, m6: Mapping[tuple[str, int], float], top_n: int, csv_path: Path,
                    limit: int) -> list[str]:
    """m6：M6 驗證段的平均超額報酬，key 為（規則名稱, 持有天數）；沒有時不顯示。"""
    sections = [_header(report), _consistency(report), _week(report, top_n),
                *(_cumulative(report, n, m6) for n in report.horizons),
                Section(head=(f"明細：{csv_path.as_posix()}", DISCLAIMER))]
    return pack_sections(sections, limit)


def _header(report: SignalReport) -> Section:
    period = report.period
    lines = [f"【訊號成績單】{period.start} ～ {period.end:%m-%d}（{len(report.trading_days)} 個交易日）"]
    rows = report.week_rows
    pushed = rows[rows["pushed"]]
    if report.scan_days:
        sides = "、".join(f"{side.label} {int((pushed['side'] == side.value).sum())}" for side in Side)
        substituted = sum(d.run.substituted for d in report.scan_days)
        not_evaluated = sum(d.run.not_evaluated for d in report.scan_days)
        lines.append(f"推播 {len(report.scan_days)} 天｜訊號 {len(pushed)} 個（{sides}）｜替代價 {substituted} 次｜"
                     f"未評估 {not_evaluated} 次")
    else:
        lines.append("推播 0 天：期間內沒有推播紀錄")
    if report.missing_days:
        lines.append(f"⚠️ 沒有推播紀錄的交易日：{_days(report.missing_days)}")
    if report.changed_rule_days:
        lines.append(f"⚠️ 期間內規則有變更：{_days(report.changed_rule_days)} 推播時的規則檔與目前不同，"
                     f"收盤訊號以目前的規則重算")
    if unadjusted := int((pushed["unadjusted"]).sum()):
        lines.append(f"⚠️ {unadjusted} 個訊號的持有期間有日子沒有快照、事件資料也沒有涵蓋，除權息未調整（見明細）")
    return Section(head=(*lines, ""))


def _consistency(report: SignalReport) -> Section:
    title = "━━ 盤中 vs 收盤 ━━"
    table = consistency(report)
    items = [(f"{report.rule_names[rule_id]}  一致 {_rate(row['rate'])}"
              f"（{VANISHED} {int(row[VANISHED])}、{APPEARED} {int(row[APPEARED])}）",)
             for rule_id, row in table.iterrows()]
    statuses = report.week_rows["status"]
    for status, label in ((NOT_EVALUATED, "13:10 未評估、收盤才觸發"), (NO_CLOSE_BAR, "資料庫沒有當日日 K")):
        if count := int((statuses == status).sum()):
            items.append((f"{label}：{count} 個（不列入）",))
    if not items:
        items = [("（沒有訊號）",)]
    return Section(head=(title,), items=tuple(items), tail=("",), resume_inside=(f"{title}（續）",))


def _week(report: SignalReport, top_n: int) -> Section:
    last = report.last_trading_day
    title = f"━━ 本週表現（13:10 進場，持有到 {last:%m/%d} 收盤）━━" if last else "━━ 本週表現 ━━"
    items = [(_stats_line(report.rule_names[rule_id], row),)
             for rule_id, row in performance(report.week_rows, WEEK, list(report.rule_names)).iterrows()]
    best, worst = extremes(report, top_n)
    for label, picks in (("最好", best), ("最差", worst)):
        if picks:
            items.append((f"{label}：" + "、".join(
                f"{e.code} {e.name} {_pct(e.ret, 1)}（{e.day:%m/%d} {'、'.join(e.rule_names)}）" for e in picks),))
    if not items:
        items = [("（沒有訊號）",)]
    return Section(head=(title,), items=tuple(items), tail=("",), resume_inside=(f"{title}（續）",))


def _cumulative(report: SignalReport, n: int, m6: Mapping[tuple[str, int], float]) -> Section:
    since = f"{report.first_day} 起，" if report.first_day else ""
    title = f"━━ 累積（M7 上線以來，{since}持有 {n} 日）━━"
    items = []
    for rule_id, row in performance(report.pushed_rows, n, list(report.rule_names)).iterrows():
        name = report.rule_names[rule_id]
        reference = f"（M6 驗證段 {_pct(m6[(name, n)])}）" if (name, n) in m6 else ""
        items.append((_stats_line(name, row) + reference,))
    if not items:
        items = [(f"（尚無持有滿 {n} 日的訊號）",)]
    return Section(head=(title,), items=tuple(items), tail=("",), resume_inside=(f"{title}（續）",))


def _stats_line(name: str, row: pd.Series) -> str:
    return (f"{name}  {int(row['count'])} 次｜平均 {_pct(row['mean'])}｜勝率 {_rate(row['win_rate'])}｜"
            f"超額 {_pct(row['excess'])}")


def _pct(value: float, digits: int = 2) -> str:
    if value is None or np.isnan(value):
        return "-"
    return f"{value:+.{digits}f}%"


def _rate(value: float) -> str:
    return "-" if value is None or np.isnan(value) else f"{value:.0f}%"


def _days(days: Sequence[date]) -> str:
    return "、".join(f"{day:%m/%d}" for day in days)


# --- 明細 CSV ---------------------------------------------------------------

_SIDE_LABELS = {side.value: side.label for side in Side}


def write_csv(report: SignalReport, path: Path) -> None:
    """期間內每個訊號一列（含收盤才出現的訊號）。報酬為方向報酬（空方取負號），單位 %。
    以 UTF-8 BOM 編碼，Excel 開啟中文不會亂碼。"""
    rows = report.week_rows
    table = pd.DataFrame({
        "日期": rows["date"], "代號": rows["code"], "名稱": rows["name"], "規則": rows["rule_name"],
        "方向": rows["side"].map(_SIDE_LABELS), "趨勢": rows["trend"], "盤中 vs 收盤": rows["status"],
        "13:10 現價": rows["price"], "現價替代來源": rows["substitute"], "13:10 漲跌停鎖死": rows["limit_locked"],
        "當日收盤": rows["close"], "13:10→收盤(%)": rows["ret_close"].round(4),
        "本週報酬(%)": rows["ret_week"].round(4), "本週基準(%)": rows["base_week"].round(4),
        "本週超額(%)": rows["excess_week"].round(4),
    })
    for n in report.horizons:
        for key, label in (("ret", "報酬"), ("base", "基準"), ("excess", "超額")):
            table[f"{label}{n}日(%)"] = rows[f"{key}_{n}"].round(4)
    table["除權息調整"] = rows["adjustment"]
    table["規則有變更"] = rows["rule_changed"]
    path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(path, index=False, encoding="utf-8-sig")
