"""逐筆成交 → 1／3／5 分 K（REALTIME_PLAN.md 2.1，L4）。純函式：不碰網路、不匯入 shioaji。

- 1 分 K 涵蓋該分鐘的 00～59 秒，以開始時間標示；N 分 K 從 session.open 對齊。
- 試撮（開盤前、緩漲、緩跌、收盤前）不畫 K 棒，也不計入成交量與均價線。
- 沒有成交的分鐘不畫 K 棒（不補量為 0 的 K 棒）。
- session.close（13:30）那一分鐘的成交是收盤成交，在每一種分 K 都自成一根，標示為 13:30；之後的成交（例：盤後定價）不計入。
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from screener.realtime.config import Session

SHARES_PER_LOT = 1000            # 一張 = 1,000 股；成交金額以元計


@dataclass(frozen=True)
class Trade:
    time: datetime               # 交易所的成交時間
    price: float
    volume: int                  # 張
    is_trial: bool = False       # 試撮


@dataclass(frozen=True)
class Bar:
    start: datetime              # 開始時間（程式內部一律存開始時間，2.1）
    minutes: int
    open: float
    high: float
    low: float
    close: float
    volume: int                  # 張
    amount: float                # 元

    @property
    def end(self) -> datetime:
        """走完的時間：這根 K 棒的值從這一刻起才確定（2.4）。"""
        return self.start + timedelta(minutes=self.minutes)


@dataclass(frozen=True)
class SessionTrades:
    """一個交易日中要組成 K 棒的成交，以及被排除的成交（供對照與檢查）。"""

    included: list[Trade]
    trial: int                                   # 試撮的筆數
    ignored: list[tuple[Trade, str]] = field(default_factory=list)   # (成交, 原因)


def session_trades(trades: Iterable[Trade], day: date, session: Session) -> SessionTrades:
    """依時間排序（同時間維持原本順序），排除試撮、開盤前與收盤之後的成交，以及不是這一天的成交。"""
    opening = datetime.combine(day, session.open)
    closing_end = datetime.combine(day, session.close) + timedelta(minutes=1)
    included: list[Trade] = []
    ignored: list[tuple[Trade, str]] = []
    trial = 0
    for trade in sorted(trades, key=lambda t: t.time):
        if trade.is_trial:
            trial += 1
        elif trade.time.date() != day:
            ignored.append((trade, "不是這個交易日"))
        elif trade.time < opening:
            ignored.append((trade, "開盤前"))
        elif trade.time >= closing_end:
            ignored.append((trade, "收盤之後"))
        else:
            included.append(trade)
    return SessionTrades(included=included, trial=trial, ignored=ignored)


def bucket_start(moment: datetime, minutes: int, session: Session) -> datetime:
    """moment 所屬的 N 分 K 的開始時間：從 session.open 對齊；session.close 以後自成一根。"""
    closing = datetime.combine(moment.date(), session.close)
    if moment >= closing:
        return closing
    opening = datetime.combine(moment.date(), session.open)
    elapsed = int((moment - opening).total_seconds() // 60)
    return opening + timedelta(minutes=elapsed - elapsed % minutes)


def build_bars(trades: Sequence[Trade], minutes: int, session: Session) -> list[Bar]:
    """由 session_trades 的 included（已排序、已排除試撮）組成 N 分 K；沒有成交的時段沒有 K 棒。"""
    bars: list[Bar] = []
    for trade in trades:
        start = bucket_start(trade.time, minutes, session)
        amount = trade.price * trade.volume * SHARES_PER_LOT
        if bars and bars[-1].start == start:
            last = bars[-1]
            bars[-1] = Bar(start=start, minutes=minutes, open=last.open, high=max(last.high, trade.price),
                           low=min(last.low, trade.price), close=trade.price, volume=last.volume + trade.volume,
                           amount=last.amount + amount)
        else:
            if bars and start < bars[-1].start:
                raise ValueError(f"成交沒有依時間排序：{trade.time} 在 {bars[-1].start} 之後")
            bars.append(Bar(start=start, minutes=minutes, open=trade.price, high=trade.price, low=trade.price,
                            close=trade.price, volume=trade.volume, amount=amount))
    return bars


def resample(bars: Sequence[Bar], minutes: int, session: Session) -> list[Bar]:
    """把 1 分 K 合成 N 分 K（同 build_bars 的切法）。用於只有分 K 的歷史資料與前一交易日的 K 棒。"""
    merged: list[Bar] = []
    for bar in bars:
        start = bucket_start(bar.start, minutes, session)
        if merged and merged[-1].start == start:
            last = merged[-1]
            merged[-1] = Bar(start=start, minutes=minutes, open=last.open, high=max(last.high, bar.high),
                             low=min(last.low, bar.low), close=bar.close, volume=last.volume + bar.volume,
                             amount=last.amount + bar.amount)
        else:
            merged.append(Bar(start=start, minutes=minutes, open=bar.open, high=bar.high, low=bar.low,
                              close=bar.close, volume=bar.volume, amount=bar.amount))
    return merged
