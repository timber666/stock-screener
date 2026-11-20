"""1 分 K 的品質檢查：python -m screener rt-check（REALTIME_PLAN.md 7.2 第 4 點，L24）。

intraday.db 的 1 分 K 每檔每天合起來，和 screener.db 的日 K 比對，範圍同 rt-backfill（backfill.start 或上市櫃日起，
L25）；兩個資料庫都以唯讀開啟，
不登入 Shioaji。輸出到 realtime.yaml 的 check.output_dir：

- report.md：回補的進度、開高低收、量的比例、缺漏、依年度的格式檢查、查詢紀錄。
- ohlc_mismatch.csv：開高低收不一致的股日。
- volume_outliers.csv：量的比例低於 check.volume_ratio_min 或高於 1 的股日。
- missing.csv：有日 K 沒有 1 分 K（查過但沒有資料）、有 1 分 K 沒有日 K、沒有 13:30 那根的股日。

回補還在進行時也可以執行：還沒回補的股日只計數，不算缺漏。
"""

import logging
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

import pandas as pd

from screener.config import AppConfig
from screener.jobs import JobFailed
from screener.realtime.backfill import first_day
from screener.realtime.config import DaytradeConfig, RealtimeConfig
from screener.realtime.intraday import EMPTY, FAILED, OK, FetchRecord, IntradayStore
from screener.realtime.quality import COMPARED, DAILY_COLUMNS, NO_DAILY, NO_DATA, NOT_FETCHED, check_stock

logger = logging.getLogger(__name__)

QUANTILES = (0.0, 0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99, 1.0)
_QUANTILE_NAMES = ("最小", "1%", "5%", "25%", "中位數", "75%", "95%", "99%", "最大")
_CSV_COLUMNS = ["code", "name", "date", "status", "before_listing", "d_open", "d_high", "d_low", "d_close", "d_volume",
                "b_open", "b_high", "b_low", "b_close", "b_volume", "volume_ratio", "bars", "first_minute",
                "last_minute"]


def run(config: AppConfig, realtime: RealtimeConfig, daytrade: DaytradeConfig, *, codes: Sequence[str] | None = None,
        out: TextIO | None = None, now: datetime | None = None) -> Path:
    """回傳輸出目錄。"""
    out = out or sys.stdout
    names = {stock.code: stock.name for stock in config.watchlist.stocks}
    selected = list(codes or names)
    unknown = [code for code in selected if code not in names]
    if unknown:
        raise JobFailed(f"不在觀察清單的代號：{', '.join(unknown)}")
    start = realtime.backfill.start
    try:
        store = IntradayStore(realtime.backfill.db_path, readonly=True)
    except FileNotFoundError as exc:
        raise JobFailed(str(exc)) from exc
    with store, closing(_open_readonly(config.settings.db_path)) as daily_db:
        listings = {code: date.fromisoformat(day) for code, day in daily_db.execute(
            "SELECT code, listed_on FROM listings")}
        fetched = store.fetched_days()
        frames = []
        for code in selected:
            # 同 rt-backfill 的範圍：start 或上市櫃日起（L25）；之前已回補的資料（例：改規則以前的興櫃期間）不檢查
            begin = first_day(code, start, listings.get(code), realtime.backfill.market_transfers)
            daily = pd.read_sql_query("SELECT date, open, high, low, close, volume FROM daily_prices "
                                      "WHERE code = ? AND date >= ? ORDER BY date", daily_db,
                                      params=(code, begin.isoformat()))[list(DAILY_COLUMNS)]
            bars = store.bars_frame(code)
            result = check_stock(daily, {day: n for day, n in fetched.get(code, {}).items() if day >= begin},
                                 bars[bars["date"] >= begin.isoformat()], daytrade.session, listings.get(code))
            frames.append(result.assign(code=code, name=names[code]))
        log = [record for record in store.fetch_log() if record.code in set(selected)]
    days = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    out_dir = realtime.check.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = _tables(days, realtime.check.volume_ratio_min)
    for name, frame in tables.items():
        frame.reindex(columns=_CSV_COLUMNS + (["problem"] if "problem" in frame else [])).to_csv(
            out_dir / f"{name}.csv", index=False, encoding="utf-8")
    text = _report(days, tables, log, selected, realtime, now or datetime.now())
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    out.write(_console_summary(days, tables) + f"輸出：{out_dir}\n")
    logger.info("rt-check 輸出到 %s", out_dir)
    return out_dir


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise JobFailed(f"找不到 {db_path}")
    return sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)


# --- 明細 -----------------------------------------------------------------------

def _tables(days: pd.DataFrame, ratio_min: float) -> dict[str, pd.DataFrame]:
    if days.empty:
        empty = pd.DataFrame(columns=_CSV_COLUMNS)
        return {"ohlc_mismatch": empty, "volume_outliers": empty.assign(problem=[]), "missing": empty.assign(problem=[])}
    compared = days[days["status"] == COMPARED]
    ratio = compared["volume_ratio"].astype(float)
    outliers = compared[(ratio < ratio_min) | (ratio > 1)]
    outliers = outliers.assign(problem=["低於下限" if value < ratio_min else "高於 1" for value in outliers["volume_ratio"]])
    missing = pd.concat([
        days[days["status"] == NO_DATA].assign(problem="有日 K、沒有 1 分 K"),
        days[days["status"] == NO_DAILY].assign(problem="有 1 分 K、沒有日 K"),
        compared[~compared["has_close_bar"].astype(bool)].assign(problem="沒有 13:30 那根"),
    ], ignore_index=True)
    return {"ohlc_mismatch": compared[~compared["same_ohlc"].astype(bool)], "volume_outliers": outliers,
            "missing": missing}


# --- 報告 -----------------------------------------------------------------------

def _count(days: pd.DataFrame, status: str) -> int:
    return int((days["status"] == status).sum()) if not days.empty else 0


def _console_summary(days: pd.DataFrame, tables: dict[str, pd.DataFrame]) -> str:
    compared = _count(days, COMPARED)
    return (f"rt-check：比對 {compared:,} 個股日，開高低收不一致 {len(tables['ohlc_mismatch']):,}、"
            f"量的比例超出範圍 {len(tables['volume_outliers']):,}、缺漏 {len(tables['missing']):,}；"
            f"還沒回補 {_count(days, NOT_FETCHED):,} 個股日\n")


def _report(days: pd.DataFrame, tables: dict[str, pd.DataFrame], log: Sequence[FetchRecord], codes: Sequence[str],
            realtime: RealtimeConfig, now: datetime) -> str:
    listed = realtime.check.listed
    ratio_min = realtime.check.volume_ratio_min
    with_daily = days[days["status"] != NO_DAILY] if not days.empty else days
    first = with_daily["date"].min() if not with_daily.empty else "-"
    last = with_daily["date"].max() if not with_daily.empty else "-"
    lines = [f"# 1 分 K 的品質檢查（{now:%Y-%m-%d %H:%M}）", "",
             f"範圍：{len(codes)} 檔，{first}～{last}（screener.db 的日 K，每檔從 {realtime.backfill.start} 或上市櫃日起，"
             f"轉市場的 {'、'.join(realtime.backfill.market_transfers) or '（無）'} 從 {realtime.backfill.start} 起，L25）。"
             "1 分 K 合起來：開 = 第一根的開盤、高低 = 最高與最低、收 = 最後一根的收盤、量 = 合計（REALTIME_PLAN.md 7.2 第 4 點）。",
             "", "## 回補的進度", "", "| 項目 | 股日 |", "|---|---:|",
             f"| screener.db 的股日 | {len(with_daily):,} |",
             f"| 已回補，有 1 分 K | {_count(days, COMPARED):,} |",
             f"| 已回補，沒有資料 | {_count(days, NO_DATA):,} |",
             f"| 還沒回補 | {_count(days, NOT_FETCHED):,} |",
             f"| 有 1 分 K、沒有日 K | {_count(days, NO_DAILY):,} |", ""]
    statuses = pd.Series([record.status for record in log]).value_counts()
    lines.append(f"查詢紀錄：{len(log):,} 次（有 K 棒 {int(statuses.get(OK, 0)):,}、沒有資料 "
                 f"{int(statuses.get(EMPTY, 0)):,}、失敗 {int(statuses.get(FAILED, 0)):,}）。")

    compared = days[days["status"] == COMPARED] if not days.empty else days
    mismatch = tables["ohlc_mismatch"]
    lines += ["", "## 開高低收", "",
              f"比對 {len(compared):,} 個股日：完全相同 {len(compared) - len(mismatch):,}、不同 {len(mismatch):,}"
              "（明細：ohlc_mismatch.csv）。"]
    if len(mismatch):
        lines += ["", "| 代號 | 日期 | 日 K 開／高／低／收 | 1 分 K 開／高／低／收 | K 棒數 |", "|---|---|---|---|---:|"]
        for row in mismatch.head(listed).itertuples():
            lines.append(f"| {row.code} {row.name} | {row.date} | {_ohlc(row, 'd')} | {_ohlc(row, 'b')} | {int(row.bars)} |")
        if len(mismatch) > listed:
            lines.append(f"\n只列出前 {listed} 筆。")

    lines += ["", "## 量（1 分 K ÷ 日 K）", "",
              "1 分 K 只含一般交易，日 K 含零股、盤後定價、鉅額交易（D21），比例應該略低於 1（M12：97～99%）。", ""]
    ratio = compared["volume_ratio"].dropna().astype(float) if len(compared) else pd.Series(dtype=float)
    if len(ratio):
        values = ratio.quantile(list(QUANTILES))
        lines += ["| " + " | ".join(_QUANTILE_NAMES) + " |", "|" + "---:|" * len(QUANTILES),
                  "| " + " | ".join(f"{value:.3f}" for value in values) + " |", ""]
    outliers = tables["volume_outliers"]
    low = int((outliers["problem"] == "低於下限").sum()) if len(outliers) else 0
    lines.append(f"低於 {ratio_min:g}：{low:,} 個股日；高於 1：{len(outliers) - low:,} 個股日；日 K 量為 0："
                 f"{int(compared['volume_ratio'].isna().sum()) if len(compared) else 0:,} 個股日（明細：volume_outliers.csv）。")
    if len(outliers):
        lines += ["", "| 代號 | 日期 | 1 分 K 量 | 日 K 量 | 比例 |", "|---|---|---:|---:|---:|"]
        for row in outliers.sort_values("volume_ratio").head(listed).itertuples():
            lines.append(f"| {row.code} {row.name} | {row.date} | {row.b_volume:,.0f} | {row.d_volume:,.0f} | "
                         f"{row.volume_ratio:.3f} |")

    lines += ["", "## 缺漏", "", "明細：missing.csv。", ""]
    missing = tables["missing"]
    for problem in ("有日 K、沒有 1 分 K", "有 1 分 K、沒有日 K", "沒有 13:30 那根"):
        part = missing[missing["problem"] == problem] if len(missing) else missing
        extra = ""
        if problem == "有日 K、沒有 1 分 K" and len(part):
            extra = f"，其中上市櫃日之前（興櫃，D38）{int(part['before_listing'].sum()):,}"
        lines.append(f"- {problem}：{len(part):,} 個股日{extra}")
        if len(part):
            by_code = part.groupby(["code", "name"])["date"].agg(["count", "min", "max"]).reset_index()
            by_code = by_code.sort_values("count", ascending=False)
            for row in by_code.head(listed).itertuples():
                span = row.min if row.min == row.max else f"{row.min}～{row.max}"
                lines.append(f"  - {row.code} {row.name}：{row.count:,} 個股日（{span}）")
            if len(by_code) > listed:
                lines.append(f"  - 另有 {len(by_code) - listed} 檔")

    lines += ["", "## 格式（依年度）", "",
              "M12 驗證過的近期資料：K 棒都在 09:00～13:24 與 13:30 之間、之前的日子沒有量為 0 的 K 棒、有 13:30 的收盤那根。"
              "「交易時段以外」應為 0；若舊資料的時間標示不同（例：以開始時間標示），轉換後會出現 08:59 的 K 棒。", "",
              "| 年度 | 比對的股日 | 平均 K 棒數 | 有 09:00 那根 | 有 13:30 那根 | 交易時段以外的 K 棒 | 刪除的量為 0 的 K 棒 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    zero_by_year: dict[int, int] = {}
    for record in log:
        zero_by_year[record.end.year] = zero_by_year.get(record.end.year, 0) + record.zero_bars
    if len(compared):
        for year, part in compared.groupby("year"):
            lines.append(f"| {year} | {len(part):,} | {part['bars'].mean():.1f} | "
                         f"{part['has_open_bar'].astype(bool).mean():.1%} | {part['has_close_bar'].astype(bool).mean():.1%} | "
                         f"{int(part['outside_session'].sum()):,} | {zero_by_year.get(int(year), 0):,} |")
    lines.append("")
    lines.append("刪除的量為 0 的 K 棒依查詢的迄日歸到年度（查詢紀錄的 zero_bars）。")

    failed = [record for record in log if record.status == FAILED]
    notes = [record for record in log if record.status != FAILED and record.note]
    lines += ["", "## 查詢紀錄", ""]
    lines.append(f"- 失敗：{len(failed):,} 次" + ("（之後的執行會重查）" if failed else ""))
    for record in failed[-listed:]:
        lines.append(f"  - {record.fetched_at:%Y-%m-%d %H:%M} {record.code} {record.start}～{record.end}：{record.note}")
    lines.append(f"- 回傳了不在日 K 的日子（沒有寫入）：{len(notes):,} 次")
    for record in notes[:listed]:
        lines.append(f"  - {record.code} {record.start}～{record.end}：{record.note}")
    return "\n".join(lines) + "\n"


def _ohlc(row: object, prefix: str) -> str:
    return "／".join(f"{getattr(row, f'{prefix}_{name}'):g}" for name in ("open", "high", "low", "close"))
