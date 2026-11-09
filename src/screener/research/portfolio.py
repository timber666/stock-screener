"""組合回測與模擬組合：每月換股的帳戶模擬（RESEARCH_PLAN.md 4.6）。

分成三步：
1. rebalance_schedule：由交易日算出每月的可用日、訊號日與成交日（R31）。
2. build_rankings：訊號日收盤的因子值 → 依挑法排序的完整名單。
3. simulate_account：逐日模擬現金與持股。回測與模擬組合（portfolio-track）共用，規則不會分岔。

股數、成交金額與漲跌停用未還原價；持有期間的市值與損益以還原權息後的價格計算，等於股利再投入該檔（同 R6）。
"""

import math
import random
from bisect import bisect_right
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from screener.analysis.numeric import normalize
from screener.backtest.trades import (END_OF_DATA, TRADING_DAYS_PER_YEAR, AccountSettings, _affordable_shares,
                                      _annualized, _commission, drawdown)
from screener.config import TOP_FACTOR, TOP_VALUE
from screener.research.factor_eval import quantile_labels, restrict
from screener.research.factors.panel import Panel, cached_limits

OPEN = "open"                       # 下一個交易日開盤成交（主要結果）
CLOSE = "close"                     # 訊號日收盤成交（對照）
EXECUTIONS = (OPEN, CLOSE)
DROPPED = "掉出名單"
FINAL = "期末結算"


# --- 1. 換股日 ---------------------------------------------------------------

@dataclass(frozen=True)
class Rebalance:
    available: date                 # 可用日 D：當月 10 日之後的第一個交易日（R26）
    signal: date                    # 訊號日：D 那一週（週一～週日）的最後一個交易日
    trade: date | None              # 訊號日的下一個交易日；資料還沒到時為 None


def rebalance_schedule(days: Sequence[date], *, start: date, end: date, available_day: int,
                       covered_through: date) -> list[Rebalance]:
    """訊號日落在 start～end（含）的每月換股日。D 那一週要過完才決定得出訊號日：資料沒有涵蓋到該週五的月份不列入。"""
    schedule = []
    year, month = start.year, start.month
    while date(year, month, 1) <= end:
        rebalance = _month_rebalance(days, year, month, available_day, covered_through)
        if rebalance is not None and start <= rebalance.signal <= end:
            schedule.append(rebalance)
        year, month = year + month // 12, month % 12 + 1
    return schedule


def _month_rebalance(days: Sequence[date], year: int, month: int, available_day: int,
                     covered_through: date) -> Rebalance | None:
    position = bisect_right(days, date(year, month, available_day))
    if position == len(days):
        return None
    available = days[position]
    monday = available - timedelta(days=available.weekday())
    if covered_through < max(available, monday + timedelta(days=4)):
        return None
    last = bisect_right(days, monday + timedelta(days=6)) - 1
    return Rebalance(available=available, signal=days[last], trade=days[last + 1] if last + 1 < len(days) else None)


# --- 2. 排名 -----------------------------------------------------------------

@dataclass(frozen=True)
class RebalanceRanking:
    """一次換股的排名。有因子值的股票少於 min_stocks 時三個名單都是空的：當月不換股。"""

    rebalance: Rebalance
    by_factor: tuple[str, ...]      # 全部有因子值的股票，因子值由大到小，同分依代號
    by_value: tuple[str, ...]       # 最高組內，平均成交金額由大到小，同分依代號
    top_group: tuple[str, ...]      # 最高組，依代號

    def ranking(self, pick: str) -> tuple[str, ...]:
        if pick == TOP_FACTOR:
            return self.by_factor
        if pick == TOP_VALUE:
            return self.by_value
        raise ValueError(f"未知的挑法：{pick}")


def build_rankings(panel: Panel, factor: pd.DataFrame, liquidity: pd.DataFrame, schedule: Sequence[Rebalance], *,
                   quantiles: int, min_stocks: int) -> list[RebalanceRanking]:
    """以訊號日收盤的因子值排名；股票池與分組同 M9（Panel.eligible、quantile_labels）。"""
    values = restrict(factor, panel)
    average = liquidity.reindex(index=panel.days, columns=panel.codes).to_numpy(dtype=float)
    codes = list(panel.codes)
    row_of = {day: i for i, day in enumerate(panel.days)}
    rankings = []
    for rebalance in schedule:
        row = row_of[rebalance.signal]
        labels = quantile_labels(values[[row]], quantiles, min_stocks)[0]
        if not labels.any():
            rankings.append(RebalanceRanking(rebalance, (), (), ()))
            continue
        columns = np.flatnonzero(labels > 0)                # 依代號排序，sorted 穩定，同分時依代號
        top = columns[labels[columns] == quantiles]
        by_factor = sorted(columns, key=lambda c: -values[row, c])
        by_value = sorted(top, key=lambda c: -average[row, c] if not np.isnan(average[row, c]) else math.inf)
        rankings.append(RebalanceRanking(rebalance, tuple(codes[c] for c in by_factor),
                                         tuple(codes[c] for c in by_value), tuple(codes[c] for c in top)))
    return rankings


def random_plan(rankings: Sequence[RebalanceRanking], rng: random.Random) -> list[tuple[Rebalance, tuple[str, ...]]]:
    """基準③：最高組的隨機順序（R35）。"""
    plan = []
    for item in rankings:
        group = list(item.top_group)
        rng.shuffle(group)
        plan.append((item.rebalance, tuple(group)))
    return plan


# --- 3. 帳戶 -----------------------------------------------------------------

@dataclass(frozen=True)
class PriceTable:
    """帳戶模擬用的價格：列為交易日、欄為股票；沒有成交的日子為 NaN。"""

    days: tuple[date, ...]
    codes: tuple[str, ...]
    raw_open: np.ndarray
    raw_close: np.ndarray
    adj_open: np.ndarray
    adj_close: np.ndarray
    adj_held: np.ndarray            # 還原收盤，沒有成交的日子沿用最後一個收盤：持股計值用
    reference: np.ndarray           # 參考價 = 前一根 K 棒的收盤 × 當天的調整因子
    last_bar: np.ndarray            # 每檔最後一根 K 棒的列；沒有 K 棒時為 −1

    @classmethod
    def from_panel(cls, panel: Panel) -> "PriceTable":
        return cls.from_arrays(tuple(panel.days), tuple(panel.codes),
                               raw_open=panel.raw_open.to_numpy(dtype=float), raw_close=panel.raw_close.to_numpy(dtype=float),
                               adj_open=panel.open.to_numpy(dtype=float), adj_close=panel.close.to_numpy(dtype=float),
                               reference=panel.reference.to_numpy(dtype=float))

    @classmethod
    def from_arrays(cls, days: tuple[date, ...], codes: tuple[str, ...], *, raw_open: np.ndarray, raw_close: np.ndarray,
                    adj_open: np.ndarray, adj_close: np.ndarray, reference: np.ndarray) -> "PriceTable":
        traded = ~np.isnan(adj_close)
        last_bar = np.where(traded.any(axis=0), len(days) - 1 - np.argmax(traded[::-1], axis=0), -1)
        return cls(days, codes, raw_open, raw_close, adj_open, adj_close,
                   pd.DataFrame(adj_close).ffill().to_numpy(), reference, last_bar)


@dataclass(frozen=True)
class PortfolioTrade:
    """一筆已賣出的部位。金額單位為元。"""

    code: str
    buy_day: date
    buy_price: float                # 未還原
    shares: int
    buy_amount: float               # 股數 × 買進價
    buy_fee: float
    sell_day: date
    sell_price: float               # 未還原；當天沒有成交（期末結算）時為 NaN
    sell_amount: float              # 買進金額 × 還原後的漲跌：沒有除權息時等於股數 × 賣出價，否則含股利
    sell_fee: float
    tax: float
    reason: str
    deferred_days: int              # 賣不掉順延的交易日數

    @property
    def pnl(self) -> float:
        return self.sell_amount - self.sell_fee - self.tax - self.buy_amount - self.buy_fee


@dataclass(frozen=True)
class OpenPosition:
    """模擬結束時還持有的部位（模擬組合用）。"""

    code: str
    buy_day: date
    buy_price: float
    shares: int
    buy_amount: float
    value: float
    selling: bool                   # 等待賣出（順延中）


@dataclass(frozen=True)
class RebalanceLog:
    """一次換股實際發生的事。"""

    signal: date
    trade: date
    kept: tuple[str, ...]
    bought: tuple[str, ...]
    sold: tuple[str, ...]
    unbuyable: tuple[str, ...]      # 買不到而遞補的股票
    deferred: tuple[str, ...]       # 當天賣不掉、順延的股票
    skipped_cash: tuple[str, ...]   # 資金不足、買不起 1 股而略過的股票


@dataclass(frozen=True)
class AccountResult:
    settings: AccountSettings
    execution: str
    days: tuple[date, ...]
    cash: np.ndarray
    holdings: np.ndarray            # 每天收盤的持股市值
    positions: np.ndarray           # 每天收盤的持股檔數
    costs: np.ndarray               # 每天的手續費與證交稅
    bought_amount: float            # 買進金額合計
    trades: tuple[PortfolioTrade, ...]
    logs: tuple[RebalanceLog, ...]
    open_positions: tuple[OpenPosition, ...]
    deferred_positions: int         # 賣不掉順延過的部位數
    unbuyable_count: int
    skipped_cash_count: int

    @property
    def equity(self) -> pd.Series:
        return pd.Series(self.cash + self.holdings, index=pd.Index(self.days, name="date"))


@dataclass
class _Holding:
    code: str
    buy_day: date
    buy_price: float
    shares: int
    buy_amount: float
    buy_fee: float
    entry_adjusted: float           # 買進當下的還原價

    def value(self, adjusted: float) -> float:
        return self.buy_amount * adjusted / self.entry_adjusted


def new_list(ranking: Sequence[str], held: Collection[str], top_n: int,
             can_buy: Callable[[str], bool]) -> tuple[list[str], list[str]]:
    """(新名單, 買不到的股票)。依排名由上往下：已持有的直接列入（留任），沒持有的要買得到才列入，買不到就看下一名。

    名單確定後才賣出沒列入的持股，所以遞補不會碰到賣了又買回（R34、第 7 節第 1 點）。
    """
    chosen, unbuyable = [], []
    for code in ranking:
        if len(chosen) == top_n:
            break
        if code in held or can_buy(code):
            chosen.append(code)
        else:
            unbuyable.append(code)
    return chosen, unbuyable


def _at_limit(prices: PriceTable, raw: np.ndarray, row: int, column: int, *, up: bool) -> bool:
    """以「等於」判斷（PROJECT_PLAN.md 14.1、D56）。"""
    reference = prices.reference[row, column]
    if np.isnan(reference):
        return False
    limit_up, limit_down = cached_limits(float(reference))
    return normalize(raw[row, column]) == normalize(limit_up if up else limit_down)


def simulate_account(prices: PriceTable, plan: Sequence[tuple[Rebalance, Sequence[str]]], settings: AccountSettings,
                     execution: str, *, start: date, end: date, delisted: Collection[str] = (),
                     close_out: bool = True) -> AccountResult:
    """從 start 收盤（全部現金）模擬到 end 收盤。

    成交日先決定新名單，賣出不在名單的持股，再依排名買進新進的；賣不掉的順延到之後每個交易日，收回的現金留到下一次
    換股。delisted 的股票在最後一根 K 棒以收盤結算。close_out 時 end 當天以收盤結算全部持股（回測）；否則持股留在
    open_positions（模擬組合）。
    """
    row_of = {day: i for i, day in enumerate(prices.days)}
    first, last = row_of[start], row_of[end]
    column_of = {code: i for i, code in enumerate(prices.codes)}
    delisted_columns = {column_of[code] for code in delisted if code in column_of}
    exec_raw, exec_adj = ((prices.raw_close, prices.adj_close) if execution == CLOSE
                          else (prices.raw_open, prices.adj_open))
    by_row: dict[int, tuple[Rebalance, Sequence[str]]] = {}
    for rebalance, ranking in plan:
        day = rebalance.signal if execution == CLOSE else rebalance.trade
        if day is not None and ranking and first <= row_of.get(day, -1) <= last:
            by_row[row_of[day]] = (rebalance, ranking)

    size = last - first + 1
    cash_path, holdings_path, costs_path = np.zeros(size), np.zeros(size), np.zeros(size)
    positions_path = np.zeros(size, dtype=int)
    cash = settings.initial_capital
    holdings: dict[str, _Holding] = {}
    pending: dict[str, int] = {}            # 等待賣出的持股 → 已順延的交易日數
    trades: list[PortfolioTrade] = []
    logs: list[RebalanceLog] = []
    bought_amount = 0.0
    deferred_positions = unbuyable_count = skipped_cash_count = 0

    def can_buy(code: str, row: int) -> bool:
        raw = exec_raw[row, column_of[code]]
        return not np.isnan(raw) and not _at_limit(prices, exec_raw, row, column_of[code], up=True)

    def settle(code: str, row: int, adjusted: float, reason: str) -> None:
        nonlocal cash
        holding = holdings.pop(code)
        trade = _close_position(holding, prices.days[row], float(prices.raw_close[row, column_of[code]]), adjusted, reason,
                                pending.pop(code, 0), settings)
        trades.append(trade)
        cash += trade.sell_amount - trade.sell_fee - trade.tax
        costs_path[row - first] += trade.sell_fee + trade.tax

    for row in range(first, last + 1):
        day = prices.days[row]
        planned = by_row.get(row)
        chosen, unbuyable = [], []
        if planned is not None:
            chosen, unbuyable = new_list(planned[1], holdings, settings.max_positions, lambda code: can_buy(code, row))
            unbuyable_count += len(unbuyable)
            wanted = set(chosen)
            for code in holdings:
                if code in wanted:
                    pending.pop(code, None)         # 順延中又回到名單：取消賣出，視為留任
                else:
                    pending.setdefault(code, 0)

        sold, deferred = [], []
        for code in sorted(pending):
            column = column_of[code]
            raw = exec_raw[row, column]
            if np.isnan(raw) or _at_limit(prices, exec_raw, row, column, up=False):
                if pending[code] == 0:
                    deferred_positions += 1
                pending[code] += 1
                deferred.append(code)
                continue
            holding = holdings.pop(code)
            trade = _close_position(holding, day, float(raw), float(exec_adj[row, column]), DROPPED, pending.pop(code),
                                    settings)
            trades.append(trade)
            cash += trade.sell_amount - trade.sell_fee - trade.tax
            costs_path[row - first] += trade.sell_fee + trade.tax
            sold.append(code)

        if planned is not None:
            kept = tuple(code for code in chosen if code in holdings)
            equity = cash + sum(h.value(_marked(prices, exec_adj, row, column_of[h.code])) for h in holdings.values())
            target = equity / settings.max_positions
            bought, skipped = [], []
            for code in chosen:
                if code in holdings:
                    continue
                column = column_of[code]
                price = float(exec_raw[row, column])
                shares = _affordable_shares(min(target, cash), price, settings)
                if shares < 1:
                    skipped.append(code)
                    continue
                amount = shares * price
                fee = _commission(amount, settings)
                cash -= amount + fee
                costs_path[row - first] += fee
                bought_amount += amount
                holdings[code] = _Holding(code, day, price, shares, amount, fee, float(exec_adj[row, column]))
                bought.append(code)
            skipped_cash_count += len(skipped)
            logs.append(RebalanceLog(signal=planned[0].signal, trade=day, kept=kept, bought=tuple(bought), sold=tuple(sold),
                                     unbuyable=tuple(unbuyable), deferred=tuple(deferred), skipped_cash=tuple(skipped)))

        for code in list(holdings):
            column = column_of[code]
            if column in delisted_columns and prices.last_bar[column] == row:
                settle(code, row, float(prices.adj_close[row, column]), END_OF_DATA)
        if row == last and close_out:
            for code in sorted(holdings):
                settle(code, row, float(prices.adj_held[row, column_of[code]]), FINAL)

        cash_path[row - first] = cash
        holdings_path[row - first] = sum(h.value(prices.adj_held[row, column_of[h.code]]) for h in holdings.values())
        positions_path[row - first] = len(holdings)

    open_positions = tuple(OpenPosition(h.code, h.buy_day, h.buy_price, h.shares, h.buy_amount,
                                        h.value(float(prices.adj_held[last, column_of[h.code]])), h.code in pending)
                           for h in sorted(holdings.values(), key=lambda h: h.code))
    return AccountResult(settings=settings, execution=execution, days=prices.days[first:last + 1], cash=cash_path,
                         holdings=holdings_path, positions=positions_path, costs=costs_path, bought_amount=bought_amount,
                         trades=tuple(trades), logs=tuple(logs), open_positions=open_positions,
                         deferred_positions=deferred_positions, unbuyable_count=unbuyable_count,
                         skipped_cash_count=skipped_cash_count)


def _marked(prices: PriceTable, exec_adj: np.ndarray, row: int, column: int) -> float:
    """成交時點的還原價；當天沒有成交時沿用最後一個收盤。"""
    value = exec_adj[row, column]
    return float(prices.adj_held[row, column] if np.isnan(value) else value)


def _close_position(holding: _Holding, day: date, price: float, adjusted: float, reason: str, deferred_days: int,
                    settings: AccountSettings) -> PortfolioTrade:
    amount = holding.value(adjusted)
    return PortfolioTrade(code=holding.code, buy_day=holding.buy_day, buy_price=holding.buy_price, shares=holding.shares,
                          buy_amount=holding.buy_amount, buy_fee=holding.buy_fee, sell_day=day, sell_price=price,
                          sell_amount=amount, sell_fee=_commission(amount, settings),
                          tax=amount * settings.cost.tax_pct / 100, reason=reason, deferred_days=deferred_days)


# --- 4. 績效與基準 -----------------------------------------------------------

def index_series(values: dict[date, float], calendar: Sequence[date], initial_capital: float) -> pd.Series:
    """基準①：報酬指數累積成的帳戶，從 calendar 第一天收盤開始。"""
    series = pd.Series(values, dtype=float).reindex(calendar).ffill()
    series.index = pd.Index(calendar, name="date")
    return initial_capital * series / series.iloc[0]


def _years(equity: pd.Series) -> float:
    return max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)


def series_metrics(equity: pd.Series) -> dict[str, float]:
    """總報酬、年化、最大回撤（%）。"""
    return {"return": (equity.iloc[-1] / equity.iloc[0] - 1) * 100,
            "cagr": _annualized(equity.iloc[-1] / equity.iloc[0], _years(equity)),
            "max_drawdown": float(drawdown(equity).min())}


def account_metrics(result: AccountResult, index: pd.Series, universe: pd.Series) -> dict[str, float]:
    """帳戶、換股與基準的統計。報酬與比例都以 % 表示；換手為一年買進金額 ÷ 平均權益（倍）。"""
    equity = result.equity
    initial = result.settings.initial_capital
    years = _years(equity)
    bench_index = series_metrics(index.reindex(equity.index).ffill())
    bench_universe = series_metrics(universe.reindex(equity.index).ffill())
    return {
        "total_return": (equity.iloc[-1] / initial - 1) * 100,
        "cagr": _annualized(equity.iloc[-1] / initial, years),
        "max_drawdown": float(drawdown(equity).min()),
        "volatility": float(equity.pct_change().std() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100),
        "turnover": result.bought_amount / float(equity.mean()) / years,
        "trades": len(result.trades),
        "win_rate": float(np.mean([t.pnl > 0 for t in result.trades]) * 100) if result.trades else np.nan,
        "costs": float(result.costs.sum()),
        "unbuyable": result.unbuyable_count,
        "deferred": result.deferred_positions,
        "skipped_cash": result.skipped_cash_count,
        "avg_positions": float(result.positions.mean()),
        "final_equity": float(equity.iloc[-1]),
        "index_return": bench_index["return"],
        "index_cagr": bench_index["cagr"],
        "index_max_drawdown": bench_index["max_drawdown"],
        "universe_return": bench_universe["return"],
        "universe_cagr": bench_universe["cagr"],
        "universe_max_drawdown": bench_universe["max_drawdown"],
    }


def yearly_returns(equity: pd.Series) -> pd.Series:
    """各年報酬（%）：該年最後一天相對前一年最後一天（第一年相對第一天）。"""
    years = pd.Series([day.year for day in equity.index], index=equity.index)
    closes = equity.groupby(years).last()
    previous = closes.shift(1).fillna(equity.iloc[0])
    return (closes / previous - 1) * 100
