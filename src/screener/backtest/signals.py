"""產生歷史訊號並計算訊號後的報酬（PROJECT_PLAN.md 14.1）。

訊號以回放模式逐日計算（收盤 K 棒），與每日推播用同一個規則引擎與 build_context；判斷訊號用未還原價格
（D8、D34），對照組（D34）改用還原價。報酬一律以還原權息後的價格計算。

掛牌（上市日／上櫃日）之前的興櫃交易沒有漲跌幅限制，掛牌後前五個交易日也沒有，這段期間不產生訊號、
也不列入基準（D38）；K 棒仍用於計算均線與轉折點，與每日推播相同。
"""

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from screener.analysis.adjustment import adjust_bars, event_factors, trading_day_index
from screener.analysis.context import bars_to_frame, build_context
from screener.analysis.market import limit_prices
from screener.analysis.numeric import normalize
from screener.config import TrendConfig
from screener.data.base import EventStore, PriceStore
from screener.data.isin import NO_PRICE_LIMIT_DAYS
from screener.engine import Engine
from screener.models import Side, StockInfo


@dataclass(frozen=True)
class StockHistory:
    info: StockInfo
    bars: pd.DataFrame              # 未還原的日 K，索引為日期
    adjusted: pd.DataFrame          # 還原權息後的日 K（成交量不調整）
    factors: pd.Series              # 每根 K 棒當天的調整因子，沒有事件為 1
    listed_on: date | None = None   # 上市日／上櫃日；None 表示不限制

    @property
    def first_position(self) -> int:
        """可以產生訊號的第一根 K 棒：掛牌後第六個交易日（前五日沒有漲跌幅限制）。"""
        if self.listed_on is None:
            return 0
        listed = trading_day_index(list(self.bars.index), self.listed_on)
        return len(self.bars) if listed is None else listed + NO_PRICE_LIMIT_DAYS


def load_history(store: PriceStore, events: EventStore, info: StockInfo) -> StockHistory | None:
    """資料庫沒有該檔日 K 時為 None。"""
    bars = store.get_bars(info.code)
    if not bars:
        return None
    frame = bars_to_frame(bars)
    events_of = (events.get_dividends(info.code), events.get_actions(info.code),
                 events.get_dividend_results(info.code))
    listing = events.get_listing(info.code)
    return StockHistory(info=info, bars=frame, adjusted=adjust_bars(frame, *events_of),
                        factors=event_factors(frame, *events_of), listed_on=listing.listed_on if listing else None)


def find_signals(
    history: StockHistory,
    engine: Engine,
    trend_config: TrendConfig,
    start: date,
    end: date,
    *,
    adjusted_rules: bool = False,
) -> list[dict]:
    """start～end（含）每一天以該日 K 棒當作「今日」評估規則，回傳每個訊號一列。

    adjusted_rules 為 True 時以還原價判斷（D34 的對照組）。每個訊號只用到當天（含）以前的 K 棒。
    掛牌後第六個交易日之前不產生訊號（D38）。
    """
    bars = history.adjusted if adjusted_rules else history.bars
    days = list(bars.index)
    rows = []
    for i in range(max(bisect_left(days, start), history.first_position), bisect_right(days, end)):
        ctx = build_context(history.info, bars.iloc[:i + 1], trend_config, is_tentative=False)
        for signal in engine.evaluate(ctx):
            rows.append({
                "date": days[i], "position": i, "code": history.info.code, "name": history.info.name,
                "group": history.info.group, "rule_id": signal.rule_id, "rule_name": signal.rule_name,
                "side": signal.side.value, "trend": ctx.trend.label,
            })
    return rows


def add_flags(rows: list[dict], history: StockHistory, *, window: int, gap_flag_pct: float) -> None:
    """標記漲跌停鎖死與跳空事件（就地加上 limit_locked、gap_event 欄位）。

    漲跌停鎖死：多方訊號當日收在漲停價、空方訊號收在跌停價；參考價 = 前一日收盤 × 當天的調整因子
    （除權息、減資日即為事件後參考價）。跳空事件：訊號日之前 window 根 K 棒（含當天）內，有單次
    價格調整幅度 ≥ gap_flag_pct（%）的事件。
    """
    closes = history.bars["close"].to_numpy()
    factors = history.factors.to_numpy()
    jumps = np.abs(1 - factors) >= gap_flag_pct / 100
    for row in rows:
        i = row["position"]
        row["gap_event"] = bool(jumps[max(0, i - window + 1):i + 1].any())
        row["limit_locked"] = False
        if i > 0:
            up, down = limit_prices(float(closes[i - 1] * factors[i]))
            limit = up if row["side"] == Side.LONG.value else down
            row["limit_locked"] = normalize(closes[i]) == normalize(limit)


def add_returns(rows: list[dict], history: StockHistory, horizons: Sequence[int]) -> None:
    """以還原權息後的價格計算持有 N 日的方向報酬與期間內最大有利／不利幅度（%，空方取負號）。

    進場價為訊號當日收盤；之後不足 N 根 K 棒時為 NaN。
    """
    adjusted = history.adjusted
    close, high, low = (adjusted[c].to_numpy() for c in ("close", "high", "low"))
    for row in rows:
        i = row["position"]
        sign = 1 if row["side"] == Side.LONG.value else -1
        row["entry_price"] = float(history.bars["close"].iloc[i])
        for n in horizons:
            if i + n >= len(close):
                row[f"ret_{n}"] = row[f"mfe_{n}"] = row[f"mae_{n}"] = np.nan
                continue
            up = high[i + 1:i + n + 1].max() / close[i] - 1
            down = low[i + 1:i + n + 1].min() / close[i] - 1
            row[f"ret_{n}"] = sign * (close[i + n] / close[i] - 1) * 100
            row[f"mfe_{n}"] = (up if sign > 0 else -down) * 100
            row[f"mae_{n}"] = (down if sign > 0 else -up) * 100


def forward_returns(history: StockHistory, horizons: Sequence[int]) -> pd.DataFrame:
    """每一天持有 N 日的報酬（%，還原權息，不分方向），索引為日期、欄位為 N；供計算基準。

    與訊號相同，掛牌後第六個交易日之前不列入。
    """
    close = history.adjusted["close"]
    returns = pd.DataFrame({n: (close.shift(-n) / close - 1) * 100 for n in horizons})
    return returns.iloc[history.first_position:]


def dedupe(signals: pd.DataFrame, days: int) -> pd.Series:
    """去重：同檔同規則，距離上次計入不到 days 個交易日的訊號不計。回傳每列是否計入。"""
    kept = pd.Series(False, index=signals.index)
    for _, group in signals.sort_values("position").groupby(["code", "rule_id"]):
        last: int | None = None
        for index, position in group["position"].items():
            if last is None or position - last >= days:
                kept[index] = True
                last = position
    return kept
