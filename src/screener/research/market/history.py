"""由 market.db 讀出與訊號回測相同格式的歷史資料（StockHistory，RESEARCH_PLAN.md 4.3）。

- 日 K：有成交的日子；成交量由股換算成張（四捨五入，同 FinMind，PROJECT_PLAN.md 8.1）。
- 掛牌日：第一次出現在行情的日子，之後五個交易日不產生訊號、不列入基準（D38）；第一次出現就是資料的
  第一天時，視為之前就已掛牌。上櫃轉上市是同一個代號，接續計算，轉上市不重新排除。
- 股票池：每月更換（R7），不在當月股票池的日子不產生訊號、不列入基準，K 棒照常用於計算。
- 還原權息：優先使用官方的前後價格（除權息、減資、面額變更，D33、D37）。沒有官方事件的日子，若漲跌欄位
  推得出參考價（收盤 − 漲跌價差）而且不等於前一日收盤，以推出的參考價補上（實例：事件日遇休市順延時，
  上市沒有標 X，推出的參考價與官方相同或差一檔）。只比前一個交易日有成交的日子：上櫃股票沒有成交的隔天，
  櫃買中心會調整參考價（例：9960 2024-09-27 收 26.55，09-30 沒有成交，10-01 參考價 26.35），那不是事件。
  標 X、「除息」等卻沒有官方事件的日子推不出參考價，不還原，由品質報告列出。
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from screener.analysis.adjustment import adjust_bars, event_factors, trading_day_index
from screener.analysis.context import BAR_COLUMNS
from screener.analysis.numeric import normalize
from screener.backtest.signals import StockHistory
from screener.config import UniverseConfig
from screener.models import CorporateAction, DividendResult, Market, StockInfo
from screener.research.market.models import SHARES_PER_LOT, Security
from screener.research.market.store import SqliteMarketStore
from screener.research.universe import Universe, build_universe

MARKET_LABELS = {Market.TSE: "上市", Market.OTC: "上櫃"}     # 全市場的股票沒有族群，以市場代替
_PRICE_COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "change"]
_QUOTE_COLUMNS = [*_PRICE_COLUMNS, "market", "name"]


@dataclass(frozen=True)
class MarketHistories:
    histories: list[StockHistory]   # 曾經入選股票池的股票
    universe: Universe
    derived_events: int             # 以行情推出的參考價補上的事件數
    first_day: date                 # market.db 第一個與最後一個交易日
    last_day: date


def value_table(store: SqliteMarketStore) -> pd.DataFrame:
    """成交金額：索引為交易日、欄位為代號；不在行情表的日子為 NaN。"""
    frame = store.quotes_frame(["code", "date", "value"])
    table = frame.pivot(index="date", columns="code", values="value").sort_index()
    table.index = pd.Index([date.fromisoformat(d) for d in table.index], name="date")
    return table


def load_market_histories(store: SqliteMarketStore, config: UniverseConfig) -> MarketHistories:
    values = value_table(store)
    if values.empty:
        raise ValueError("market.db 沒有任何行情")
    universe = build_universe(values, size=config.size, lookback_days=config.lookback_days)
    codes = universe.codes
    first_day = values.index[0]
    results: dict[str, list[DividendResult]] = defaultdict(list)
    for result, _ in store.dividend_results():
        results[result.code].append(result)
    actions: dict[str, list[CorporateAction]] = defaultdict(list)
    for action, _ in store.actions():
        actions[action.code].append(action)

    quotes = store.quotes_frame(_QUOTE_COLUMNS)
    quotes = quotes[quotes["code"].isin(codes)]
    calendar = {day.isoformat(): i for i, day in enumerate(values.index)}
    histories, derived_total = [], 0
    for code, group in quotes.groupby("code", sort=True):
        group = group.sort_values("date")
        history, derived = build_history(group, _security(group), results[code], actions[code], universe,
                                         listed_before=first_day, calendar=calendar)
        histories.append(history)
        derived_total += len(derived)
    return MarketHistories(histories=histories, universe=universe, derived_events=derived_total,
                           first_day=first_day, last_day=values.index[-1])


def _security(quotes: pd.DataFrame) -> Security:
    """由一檔股票的行情（含沒有成交的日子，依日期排序）取得名稱、市場（最後一天）與第一次出現的日子。
    不依賴 securities 表：market-update 中斷時，那張表還沒有重建。"""
    last = quotes.iloc[-1]
    return Security(code=last["code"], name=last["name"], market=Market(last["market"]),
                    first_date=date.fromisoformat(quotes["date"].iloc[0]), last_date=date.fromisoformat(last["date"]),
                    delisted_on=None)


def build_history(quotes: pd.DataFrame, security: Security, results: Sequence[DividendResult],
                  actions: Sequence[CorporateAction], universe: Universe, *, listed_before: date,
                  calendar: Mapping[str, int]) -> tuple[StockHistory, list[DividendResult]]:
    """quotes：一檔股票的行情，含沒有成交的日子（欄位同 _PRICE_COLUMNS）；calendar：交易日 → 第幾個交易日。
    回傳 (歷史資料, 以行情推出的事件)。"""
    quotes = quotes.sort_values("date")
    position = quotes["date"].map(calendar)
    comparable = ((position.diff() == 1) & quotes["close"].shift(1).notna()).to_numpy()   # 前一個交易日有成交
    traded = quotes["close"].notna().to_numpy()
    quotes, comparable = quotes[traded], comparable[traded]
    days = [date.fromisoformat(d) for d in quotes["date"]]
    bars = pd.DataFrame({column: quotes[column].to_numpy() for column in BAR_COLUMNS},
                        index=pd.Index(days, name="date"))
    bars["volume"] = (quotes["volume"].to_numpy(dtype=np.int64) + SHARES_PER_LOT // 2) // SHARES_PER_LOT
    derived = derived_events(security.code, bars, quotes["change"].to_numpy(dtype=float), comparable, results,
                             actions)
    events = ((), actions, [*results, *derived])
    info = StockInfo(code=security.code, name=security.name, market=security.market,
                     group=MARKET_LABELS[security.market], subcategory=None)
    history = StockHistory(info=info, bars=bars, adjusted=adjust_bars(bars, *events),
                           factors=event_factors(bars, *events),
                           listed_on=security.first_date if security.first_date > listed_before else None,
                           in_universe=universe.mask(security.code, days))
    return history, derived


def derived_events(code: str, bars: pd.DataFrame, changes: np.ndarray, comparable: np.ndarray,
                   results: Sequence[DividendResult], actions: Sequence[CorporateAction]) -> list[DividendResult]:
    """沒有官方事件、推出的參考價（收盤 − 漲跌價差）卻不等於前一日收盤的日子，以推出的參考價當作事件。
    comparable：前一個交易日有成交（前一根 K 棒就是前一個交易日）的 K 棒。官方事件遇休市時順延到下一根
    K 棒，與還原的計算相同。"""
    days = list(bars.index)
    official = {i for event in (*results, *actions) if (i := trading_day_index(days, event.date)) is not None}
    closes = bars["close"].to_numpy(dtype=float)
    events = []
    for i in range(1, len(days)):
        if i in official or not comparable[i] or np.isnan(changes[i]):
            continue
        reference = normalize(closes[i] - changes[i])
        if reference != normalize(closes[i - 1]) and reference > 0:
            events.append(DividendResult(code, days[i], float(closes[i - 1]), float(reference)))
    return events
