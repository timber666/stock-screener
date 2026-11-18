"""四條線：3MA、5MA、均價線、VP（REALTIME_PLAN.md 2.2、2.4，L5–L7、L13）。純函式：不碰網路、不匯入 shioaji。

- 3MA、5MA：N 分 K 最近 period 根收盤價的簡單平均，包含該根自己，延續前一交易日的 K 棒。K 棒走完才有值：
  每個值都帶著走完的時間（available_at），時間 t 只能用 available_at ≤ t 的值（2.4）。不足 period 根時為 None（L13）。
- 均價線：當天 09:00 起累積成交金額 ÷ 累積成交量，每筆成交都更新。
- VP：當天成交量最大的那根 1 分 K 的收盤價，開盤那根也算；量相同時維持原本那根。正在形成的 K 棒量超過目前的最大量時，
  VP 跟著現價移動，這根走完才固定。第一根 1 分 K 走完時第一次出現。

數值以 analysis/numeric.py 的 normalize 消除浮點誤差（PROJECT_PLAN.md 10.3）。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from screener.analysis.numeric import normalize
from screener.realtime.bars import SHARES_PER_LOT, Bar, Trade, bucket_start
from screener.realtime.config import Session


@dataclass(frozen=True)
class MaPoint:
    """一根 N 分 K 的均線值。圖上畫在這根涵蓋的 1 分 K 上（階梯線），但要到 available_at 才知道。"""

    bar_start: datetime
    available_at: datetime
    value: float | None          # 不足 period 根時為 None：不畫，用到的規則不成立（L13）


@dataclass(frozen=True)
class LinePoint:
    time: datetime               # 這個值成立的時刻
    value: float | None


def moving_average(bars: Sequence[Bar], period: int, prior: Sequence[Bar] = ()) -> list[MaPoint]:
    """今天每根 N 分 K 的均線。prior 是前一交易日的 N 分 K（依時間排序；用到最後 period − 1 根）。"""
    if period <= 0:
        raise ValueError("period 必須大於 0")
    closes = [bar.close for bar in prior[-(period - 1):]] if period > 1 else []
    points = []
    for bar in bars:
        closes.append(bar.close)
        window = closes[-period:]
        value = normalize(sum(window) / period) if len(window) == period else None
        points.append(MaPoint(bar_start=bar.start, available_at=bar.end, value=value))
    return points


def value_at(points: Sequence[MaPoint], moment: datetime) -> float | None:
    """時間 moment 可以用的均線值：最後一根已走完（available_at ≤ moment）的值（2.4）。"""
    value = None
    for point in points:
        if point.available_at > moment:
            break
        value = point.value
    return value


def drawn_value(points: Sequence[MaPoint], minute: datetime) -> float | None:
    """歷史圖上這一分鐘畫的值：涵蓋這一分鐘的那根 N 分 K 的值（和看盤軟體的歷史畫面對照用，不能用於回測，2.4）。"""
    value = None
    for point in points:
        if point.bar_start > minute:
            break
        value = point.value
    return value


def vwap_by_trade(trades: Sequence[Trade]) -> list[LinePoint]:
    """每筆成交後的均價線。trades 是 session_trades 的 included。"""
    amount = 0.0
    volume = 0
    points = []
    for trade in trades:
        amount += trade.price * trade.volume
        volume += trade.volume
        points.append(LinePoint(time=trade.time, value=normalize(amount / volume) if volume else None))
    return points


def vwap_by_bar(bars: Sequence[Bar]) -> list[LinePoint]:
    """每根 1 分 K 走完時的均價線（等於那一分鐘最後一筆成交後的值）。"""
    amount = 0.0
    volume = 0
    points = []
    for bar in bars:
        amount += bar.amount
        volume += bar.volume
        value = normalize(amount / (volume * SHARES_PER_LOT)) if volume else None
        points.append(LinePoint(time=bar.end, value=value))
    return points


def volume_profile_by_bar(bars: Sequence[Bar]) -> list[LinePoint]:
    """每根 1 分 K 走完時的 VP：到這根為止量最大的 K 棒的收盤價，量相同時維持較早的那根。"""
    best: Bar | None = None
    points = []
    for bar in bars:
        if best is None or bar.volume > best.volume:
            best = bar
        points.append(LinePoint(time=bar.end, value=best.close))
    return points


def volume_profile_by_trade(trades: Sequence[Trade], session: Session) -> list[LinePoint]:
    """每筆成交後的 VP（盤中的畫法）。trades 是 session_trades 的 included。

    正在形成的 K 棒量超過已走完的 K 棒中最大的量時，VP 為現價；否則為那根最大量 K 棒的收盤價。
    第一根 K 棒還沒走完時沒有 VP。
    """
    best_volume = 0
    best_close: float | None = None
    forming_start: datetime | None = None
    forming_volume = 0
    forming_close = 0.0
    points = []
    for trade in trades:
        start = bucket_start(trade.time, 1, session)
        if forming_start is not None and start != forming_start:   # 上一根走完
            if best_close is None or forming_volume > best_volume:
                best_volume, best_close = forming_volume, forming_close
            forming_volume = 0
        forming_start = start
        forming_volume += trade.volume
        forming_close = trade.price
        if best_close is None:
            value = None
        elif forming_volume > best_volume:
            value = trade.price
        else:
            value = best_close
        points.append(LinePoint(time=trade.time, value=value))
    return points
