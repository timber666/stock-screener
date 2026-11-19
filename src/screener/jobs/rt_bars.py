"""組 K 棒、算四條線與三方對照：python -m screener rt-bars（REALTIME_PLAN.md 7.1 第 6 點，L15）。

| 代號 | 來源 |
|---|---|
| A | 自己組的 1 分 K 與四條線：來自錄製檔（record）或 Shioaji 的歷史逐筆成交（ticks）；kbars 時直接用 B |
| B | Shioaji 的歷史 1 分 K（kbars），轉成開始時間標示、刪除量為 0 的 K 棒（history.kbars_to_bars） |
| C | 台股大學堂：使用者照對照清單讀數字 |

輸出到 realtime.yaml 的 bars.output_dir/<日期>/：每檔一份每分鐘的 CSV（A 的 K 棒、B 的 K 棒與差異、四條線），
以及一份對照清單 checklist.md（給使用者在台股大學堂讀數字）。均線延續前一交易日：前一交易日的 K 棒一律用 B。
"""

import csv
import gzip
import json
import logging
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, TextIO

from screener.config import AppConfig
from screener.jobs import JobFailed
from screener.realtime.bars import Bar, Trade, build_bars, resample, session_trades
from screener.realtime.config import DaytradeConfig, RealtimeConfig, ShioajiKeys
from screener.realtime.history import kbars_to_bars, parse_kbars, parse_ticks, split_days, ticks_to_trades
from screener.realtime.indicators import (LinePoint, MaPoint, drawn_value, live_moving_average_by_bar, moving_average,
                                          value_at, volume_profile_by_bar, vwap_by_bar)
from screener.realtime.recorder import read_records, recording_path
from screener.realtime.shioaji_source import ShioajiClient, ShioajiRequestError
from screener.realtime.ticks import trades_by_code

logger = logging.getLogger(__name__)

SOURCES = ("kbars", "ticks", "record")
CHECK_TIMES = (time(10, 0), time(11, 0), time(12, 0), time(13, 0))   # 盤中抽查的時點（取這個時間以後的第一根）
OPENING_BARS = 6                 # 對照清單列出開盤後的前幾根
CLOSING_FROM = time(13, 24)      # 對照清單列出這個時間以後的 K 棒（含 13:30 的收盤）
GAPS_LISTED = 2                  # 冷門股列出幾段沒有成交的時段

ClientFactory = Callable[[], ShioajiClient]


@dataclass
class StockResult:
    code: str
    name: str
    bars: list[Bar]                                   # A 的 1 分 K
    rows: list[dict[str, Any]] = field(default_factory=list)
    diff: dict[str, Any] = field(default_factory=dict)
    ignored: list[str] = field(default_factory=list)  # A 被排除的成交（試撮以外）
    trial: int = 0
    trades: list[Trade] = field(default_factory=list)  # A 的成交（session_trades 的 included）
    mis: dict[str, Any] | None = None                  # T11：和 13:10 MIS 快照的比對


def run(config: AppConfig, realtime: RealtimeConfig, daytrade: DaytradeConfig, keys: ShioajiKeys, *, day: date,
        codes: Sequence[str] | None = None, source: str | None = None, client_factory: ClientFactory | None = None,
        out: TextIO | None = None) -> Path:
    """回傳輸出目錄。source 為 None 時，有當天的錄製檔就用 record，否則用 kbars。"""
    out = out or sys.stdout
    names = {stock.code: stock.name for stock in config.watchlist.stocks}
    codes = list(codes or realtime.compare_codes)
    unknown = [code for code in codes if code not in names]
    if unknown:
        raise JobFailed(f"不在觀察清單的代號：{', '.join(unknown)}")
    record_file = recording_path(realtime.record.tick_dir, day)
    source = source or ("record" if record_file.exists() else "kbars")
    if source not in SOURCES:
        raise JobFailed(f"未知的來源：{source}（可用：{', '.join(SOURCES)}）")
    if source == "record" and not record_file.exists():
        raise JobFailed(f"找不到錄製檔 {record_file}")
    recorded = trades_by_code(read_records(record_file), codes) if source == "record" else {}

    client = (client_factory or (lambda: ShioajiClient(realtime.shioaji, keys)))()
    results: list[StockResult] = []
    try:
        client.connect()
        for code in codes:
            results.append(_stock(client, code, names[code], day, source, recorded.get(code), realtime, daytrade))
    except ShioajiRequestError as exc:
        raise JobFailed(str(exc)) from exc
    finally:
        client.logout()
    if source != "kbars":
        _compare_mis(results, day, config.settings.db_path)

    out_dir = realtime.bars.output_dir / day.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    for result in results:
        _write_rows(out_dir / f"minutes_{result.code}_{source}.csv", result.rows)
    text = _checklist(results, day, source, daytrade)
    (out_dir / f"checklist_{source}.md").write_text(text, encoding="utf-8")
    out.write(text + f"\n輸出：{out_dir}\n")
    logger.info("rt-bars 輸出到 %s", out_dir)
    return out_dir


def _stock(client: ShioajiClient, code: str, name: str, day: date, source: str, recorded: list[Trade] | None,
           realtime: RealtimeConfig, daytrade: DaytradeConfig) -> StockResult:
    session = daytrade.session
    contract = client.stock(code)
    if contract is None:
        raise JobFailed(f"合約查不到 {code}")
    start = day - timedelta(days=realtime.bars.prior_lookback_days)
    by_day = split_days(kbars_to_bars(parse_kbars(client.kbars(contract, start, day)), session))
    b_today = by_day.get(day, [])
    earlier = [d for d in by_day if d < day]
    prior = by_day[max(earlier)] if earlier else []

    trades: list[Trade] | None = None
    if source == "ticks":
        trades = ticks_to_trades(parse_ticks(client.ticks(contract, day)))
    elif source == "record":
        trades = recorded or []
    result = StockResult(code=code, name=name, bars=b_today)
    if trades is not None:
        selected = session_trades(trades, day, session)
        result.bars = build_bars(selected.included, 1, session)
        result.trades = selected.included
        result.trial = selected.trial
        result.ignored = [f"{trade.time:%H:%M:%S.%f} {trade.price} × {trade.volume}（{reason}）"
                          for trade, reason in selected.ignored]
        result.diff = _diff(result.bars, b_today)
    if not result.bars:
        raise JobFailed(f"{code} {day} 沒有 K 棒（{source}）；可能是休市日")
    result.rows = _minute_rows(result.bars, b_today if trades is not None else None, prior, daytrade)
    return result


def _minute_rows(bars: Sequence[Bar], reference: Sequence[Bar] | None, prior: Sequence[Bar],
                 daytrade: DaytradeConfig) -> list[dict[str, Any]]:
    session = daytrade.session
    averages: dict[str, list[MaPoint]] = {}
    known: dict[str, list[MaPoint]] = {}
    live: dict[str, list[LinePoint]] = {}
    for spec in daytrade.moving_averages:
        prior_bars = resample(prior, spec.bar_minutes, session)
        averages[spec.id] = moving_average(resample(bars, spec.bar_minutes, session), spec.period, prior=prior_bars)
        # 當天第一根走完之前，最後一根已走完的是前一交易日的最後一根（均線延續前一交易日，L5、L17）
        known[spec.id] = moving_average(prior_bars, spec.period)[-1:] + averages[spec.id]
        live[spec.id] = live_moving_average_by_bar(bars, spec.bar_minutes, spec.period, session, prior=prior_bars)
    vwap = vwap_by_bar(bars)
    vp = volume_profile_by_bar(bars)
    other = {bar.start: bar for bar in reference or ()}
    rows = []
    for index, (bar, vwap_point, vp_point) in enumerate(zip(bars, vwap, vp)):
        row: dict[str, Any] = {"start": bar.start.strftime("%H:%M"), "open": bar.open, "high": bar.high,
                               "low": bar.low, "close": bar.close, "volume": bar.volume, "amount": round(bar.amount)}
        if reference is not None:
            ref = other.get(bar.start)
            row.update(b_open=ref.open if ref else None, b_high=ref.high if ref else None,
                       b_low=ref.low if ref else None, b_close=ref.close if ref else None,
                       b_volume=ref.volume if ref else None, same_as_b=_same(bar, ref))
        for spec in daytrade.moving_averages:
            row[f"{spec.id}_drawn"] = drawn_value(averages[spec.id], bar.start)        # 歷史圖上畫的值
            row[f"{spec.id}_known"] = value_at(known[spec.id], bar.end)                # 這根走完時的「走完的值」
            row[f"{spec.id}_live"] = live[spec.id][index].value                        # 這根走完時的即時值（畫面上的線）
        row["vwap"] = vwap_point.value
        row["vp"] = vp_point.value
        rows.append(row)
    return rows


def _same(a: Bar, b: Bar | None) -> bool:
    return b is not None and (a.open, a.high, a.low, a.close, a.volume) == (b.open, b.high, b.low, b.close, b.volume)


def _diff(a_bars: Sequence[Bar], b_bars: Sequence[Bar]) -> dict[str, Any]:
    a = {bar.start: bar for bar in a_bars}
    b = {bar.start: bar for bar in b_bars}
    mismatched = [f"{start:%H:%M} A {_ohlcv(a[start])}／B {_ohlcv(b[start])}" for start in sorted(a.keys() & b.keys())
                  if not _same(a[start], b[start])]
    return {"a": len(a), "b": len(b), "only_a": [f"{s:%H:%M}" for s in sorted(a.keys() - b.keys())],
            "only_b": [f"{s:%H:%M}" for s in sorted(b.keys() - a.keys())], "mismatched": mismatched,
            "volume_a": sum(bar.volume for bar in a_bars), "volume_b": sum(bar.volume for bar in b_bars)}


def _ohlcv(bar: Bar) -> str:
    return f"{bar.open}/{bar.high}/{bar.low}/{bar.close} {bar.volume}"


# --- T11：和 13:10 的 MIS 快照比對 --------------------------------------------------

def _compare_mis(results: Sequence[StockResult], day: date, db_path: Path) -> None:
    """用 scan 已保存的 MIS 原始資料（當天第一次推播），比對 A 在 MIS 資料時間（t，秒）結束時的狀態。不另外查詢 MIS。"""
    raw = _mis_records(db_path, day)
    if raw is None:
        return
    for result in results:
        record = raw.get(result.code)
        if record is None or not result.trades:
            continue
        moment = datetime.combine(day, time.fromisoformat(record["t"])) + timedelta(seconds=1)
        upto = [trade for trade in result.trades if trade.time < moment]
        if not upto:
            continue
        mine = {"last": upto[-1].price, "open": upto[0].price, "high": max(t.price for t in upto),
                "low": min(t.price for t in upto), "volume": sum(t.volume for t in upto)}
        trade = record.get("trade") or {}
        use_z = record.get("z") not in (None, "-")
        last = record.get("z") if use_z else trade.get("z")          # 同 scan 的現價（D47）
        theirs = {"last": _mis_float(last), "open": _mis_float(record.get("o")), "high": _mis_float(record.get("h")),
                  "low": _mis_float(record.get("l")), "volume": _mis_int(record.get("v"))}
        different = [f"{name} {mine[name]}／{theirs[name]}" for name in mine if mine[name] != theirs[name]]
        price_source = "z" if use_z else f"trade.z（{trade.get('t')}）"
        result.mis = {"time": record["t"], "price_source": price_source, "mine": mine, "mis": theirs,
                      "different": different,
                      "text": f"MIS 資料時間 {record['t']}，現價取 {price_source}：" + (
                          "現價、開高低、累積量都相同" if not different else "不同（A／MIS）：" + "、".join(different))}


def _mis_records(db_path: Path, day: date) -> dict[str, dict[str, Any]] | None:
    if not db_path.is_file():
        return None
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute("SELECT raw_file FROM scan_runs WHERE date = ? ORDER BY id LIMIT 1",
                           (day.isoformat(),)).fetchone()
    if row is None or not row[0] or not Path(row[0]).is_file():
        return None
    with gzip.open(row[0], "rt", encoding="utf-8") as f:
        payload = json.load(f)
    return {record.get("c"): record for record in payload["records"]}


def _mis_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mis_int(value: Any) -> int | None:
    number = _mis_float(value)
    return int(number) if number is not None else None


# --- 對照清單 -------------------------------------------------------------------

def _checklist(results: Sequence[StockResult], day: date, source: str, daytrade: DaytradeConfig) -> str:
    ma_ids = [spec.id for spec in daytrade.moving_averages]
    lines = [f"# 對照清單 {day}（A 的來源：{source}）", "",
             "時間為 K 棒的開始時間；台股大學堂「技術分析」畫面以結束時間標示，同一根顯示為下一分鐘（括號內）。",
             f"{'、'.join(ma_ids)} 是歷史圖上畫在這一分鐘的值（該根 3 分、5 分 K 的均線）；均價線、VP 是這根 1 分 K 走完時的值。"
             "比較時依台股大學堂顯示的小數位數四捨五入。"]
    for result in results:
        lines += ["", f"## {result.code} {result.name}", ""]
        if result.diff:
            diff = result.diff
            lines.append(f"- A 對 B：A {diff['a']} 根、B {diff['b']} 根；只有 A：{', '.join(diff['only_a']) or '無'}；"
                         f"只有 B：{', '.join(diff['only_b']) or '無'}；開高低收量不同：{len(diff['mismatched'])} 根"
                         f"；總量 A {diff['volume_a']:,}／B {diff['volume_b']:,} 張")
            lines += [f"  - {item}" for item in diff["mismatched"][:10]]
            lines.append(f"- 排除的試撮 {result.trial} 筆；其他排除的成交：{'; '.join(result.ignored) or '無'}")
        if result.mis:
            lines.append(f"- 和 13:10 MIS 快照比對（T11）：{result.mis['text']}")
        header = ["時間", "開", "高", "低", "收", "量", *ma_ids, "均價線", "VP"]
        lines += ["", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        for row, note in _picked_rows(result.rows):
            lines.append("| " + " | ".join([f"{row['start']}（{_next_minute(row['start'])}）{note}", _num(row["open"]),
                                             _num(row["high"]), _num(row["low"]), _num(row["close"]),
                                             f"{row['volume']:,}", *(_num(row[f'{i}_drawn']) for i in ma_ids),
                                             _num(row["vwap"]), _num(row["vp"])]) + " |")
        gaps = _gaps(result.rows)
        if gaps:
            lines.append("")
            lines.append(f"- 沒有成交的時段（不畫 K 棒）：{', '.join(gaps[:GAPS_LISTED])}"
                         + (f" 等 {len(gaps)} 段" if len(gaps) > GAPS_LISTED else ""))
    return "\n".join(lines) + "\n"


def _picked_rows(rows: Sequence[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    picked: dict[str, str] = {row["start"]: "" for row in rows[:OPENING_BARS]}
    for check in CHECK_TIMES:
        row = next((row for row in rows if row["start"] >= check.strftime("%H:%M")), None)
        if row is not None:
            picked.setdefault(row["start"], "")
    for row in rows:
        if row["start"] >= CLOSING_FROM.strftime("%H:%M"):
            picked.setdefault(row["start"], "")
    gap_rows = _gap_edges(rows)[:2 * GAPS_LISTED]
    for start in gap_rows:
        picked.setdefault(start, "＊")
    return [(row, picked[row["start"]]) for row in rows if row["start"] in picked]


def _gaps(rows: Sequence[dict[str, Any]]) -> list[str]:
    """相鄰兩根 K 棒之間沒有成交的分鐘，例：09:02～09:04。13:25～13:29 的收盤集合競價不算。"""
    gaps = []
    for before, after in zip(rows, rows[1:]):
        first = _add(before["start"], 1)
        last = _add(after["start"], -1)
        if first <= last and not (first >= "13:25" and after["start"] == "13:30"):
            gaps.append(first if first == last else f"{first}～{last}")
    return gaps


def _gap_edges(rows: Sequence[dict[str, Any]]) -> list[str]:
    """沒有成交的時段前後的 K 棒（冷門股的對照重點），以 ＊ 標示。"""
    edges = []
    for before, after in zip(rows, rows[1:]):
        if _add(before["start"], 1) < after["start"] and after["start"] != "13:30":
            edges += [before["start"], after["start"]]
    return edges


def _add(hhmm: str, minutes: int) -> str:
    moment = datetime.strptime(hhmm, "%H:%M") + timedelta(minutes=minutes)
    return moment.strftime("%H:%M")


def _next_minute(hhmm: str) -> str:
    return _add(hhmm, 1)


def _num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def _write_rows(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
