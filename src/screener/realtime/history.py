"""Shioaji 的歷史分 K（kbars）與逐筆成交（ticks）：解析與摘要（REALTIME_PLAN.md 7.1 的 T5、T6）。

Shioaji 回傳「欄位 → 清單」的結構（例：kbars 的 ts、Open、High、Low、Close、Volume、Amount）。
這裡只處理已經轉成 dict 的資料，不匯入 shioaji。
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from screener.realtime.bars import Bar, Trade
from screener.realtime.config import Session
from screener.realtime.timestamps import from_shioaji_ts


@dataclass(frozen=True)
class KBarRow:
    """Shioaji 的一根 1 分 K。label 是 Shioaji 標示的時間（T5：以結束時間標示，見 REALTIME_PLAN.md 第 3 節）。"""

    label: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int                 # 張
    amount: float               # 元


@dataclass(frozen=True)
class TickRow:
    time: datetime
    close: float
    volume: int                 # 張
    bid_price: float
    bid_volume: int
    ask_price: float
    ask_volume: int
    tick_type: int              # 1 外盤、2 內盤、0 無法判斷


def parse_kbars(columns: Mapping[str, Sequence[Any]]) -> list[KBarRow]:
    return [KBarRow(label=from_shioaji_ts(int(ts)), open=float(o), high=float(h), low=float(lo), close=float(c),
                    volume=int(v), amount=float(a))
            for ts, o, h, lo, c, v, a in zip(columns["ts"], columns["Open"], columns["High"], columns["Low"],
                                             columns["Close"], columns["Volume"], columns["Amount"])]


def parse_ticks(columns: Mapping[str, Sequence[Any]]) -> list[TickRow]:
    return [TickRow(time=from_shioaji_ts(int(ts)), close=float(c), volume=int(v), bid_price=float(bp),
                    bid_volume=int(bv), ask_price=float(ap), ask_volume=int(av), tick_type=int(tt))
            for ts, c, v, bp, bv, ap, av, tt in zip(columns["ts"], columns["close"], columns["volume"],
                                                    columns["bid_price"], columns["bid_volume"], columns["ask_price"],
                                                    columns["ask_volume"], columns["tick_type"])]


def summarize_kbars(rows: Sequence[KBarRow], closing_call: tuple[time, time]) -> dict[date, dict[str, Any]]:
    """每天一筆：根數、第一根與最後一根的標示、量為 0 的 K 棒（收盤集合競價期間與盤中分開）、量與金額、開高低收。

    closing_call 是收盤集合競價期間的標示範圍（含兩端），例：(13:26, 13:29)。
    """
    days: dict[date, list[KBarRow]] = {}
    for row in rows:
        days.setdefault(row.label.date(), []).append(row)
    summary = {}
    for day, bars in sorted(days.items()):
        zero = [bar for bar in bars if bar.volume == 0]
        in_call = [bar for bar in zero if closing_call[0] <= bar.label.time() <= closing_call[1]]
        summary[day] = {
            "count": len(bars),
            "first": bars[0].label.time().isoformat(),
            "last": bars[-1].label.time().isoformat(),
            "zero_volume": len(zero),
            "zero_volume_closing_call": len(in_call),
            "zero_volume_intraday": [bar.label.time().isoformat() for bar in zero if bar not in in_call],
            "volume": sum(bar.volume for bar in bars),
            "amount": sum(bar.amount for bar in bars),
            "open": bars[0].open,
            "high": max(bar.high for bar in bars),
            "low": min(bar.low for bar in bars),
            "close": bars[-1].close,
            "last_bar_volume": bars[-1].volume,
        }
    return summary


def summarize_ticks(rows: Sequence[TickRow], session_open: time, closing_call_start: time,
                    session_close: time) -> dict[str, Any]:
    """一個交易日的逐筆成交：筆數、時間範圍與精度、各時段的筆數、總量、內外盤。"""
    if not rows:
        return {"count": 0}
    times = [row.time.time() for row in rows]
    return {
        "count": len(rows),
        "first": times[0].isoformat(),
        "last": times[-1].isoformat(),
        "with_microseconds": sum(1 for t in times if t.microsecond),
        "before_open": sum(1 for t in times if t < session_open),
        "at_open_second": sum(1 for t in times if session_open <= t < _plus_seconds(session_open, 1)),
        "closing_call": sum(1 for t in times if closing_call_start <= t < session_close),
        "at_or_after_close": sum(1 for t in times if t >= session_close),
        "zero_volume": sum(1 for row in rows if row.volume == 0),
        "volume": sum(row.volume for row in rows),
        "tick_type": dict(sorted(Counter(row.tick_type for row in rows).items())),
        "last_ticks": [{"time": row.time.time().isoformat(), "close": row.close, "volume": row.volume}
                       for row in rows[-3:]],
        "first_ticks": [{"time": row.time.time().isoformat(), "close": row.close, "volume": row.volume}
                        for row in rows[:3]],
    }


def _plus_seconds(value: time, seconds: int) -> time:
    return (datetime.combine(date.min, value) + timedelta(seconds=seconds)).time()


def kbars_to_bars(rows: Sequence[KBarRow], session: Session) -> list[Bar]:
    """Shioaji 的 1 分 K 轉成本系統的 1 分 K（第 2 節的定義）。

    - Shioaji 以結束時間標示（09:01 = 09:00:00～09:00:59），改成開始時間；收盤那根標示為 session.close（13:30），
      就是 13:30 的收盤成交，維持 13:30。
    - Shioaji 補上的量為 0 的 K 棒（收盤集合競價期間，以及當天的資料在沒有成交的分鐘）不是成交，刪除。
    """
    bars = []
    for row in rows:
        if row.volume == 0:
            continue
        start = row.label if row.label.time() == session.close else row.label - timedelta(minutes=1)
        bars.append(Bar(start=start, minutes=1, open=row.open, high=row.high, low=row.low, close=row.close,
                        volume=row.volume, amount=row.amount))
    return bars


def ticks_to_trades(rows: Sequence[TickRow]) -> list[Trade]:
    """歷史逐筆成交轉成 Trade。歷史資料沒有試撮的標示，也不含試撮（T6），全部視為一般成交。"""
    return [Trade(time=row.time, price=row.close, volume=row.volume) for row in rows]


def split_days(bars: Sequence[Bar]) -> dict[date, list[Bar]]:
    days: dict[date, list[Bar]] = {}
    for bar in bars:
        days.setdefault(bar.start.date(), []).append(bar)
    return days
