"""組合回測的換股日、排名與帳戶模擬（RESEARCH_PLAN.md 4.6）。全部以手工資料驗證，不讀真實資料庫。"""

import random
from datetime import date

import numpy as np
import pandas as pd
import pytest

from screener.backtest.trades import AccountSettings
from screener.config import TOP_FACTOR, TOP_VALUE, CostConfig
from screener.research.factors.panel import Panel
from screener.research.portfolio import (CLOSE, DROPPED, FINAL, OPEN, PriceTable, Rebalance, RebalanceRanking,
                                         account_metrics, build_rankings, index_series, new_list, random_plan,
                                         rebalance_schedule, series_metrics, simulate_account, yearly_returns)

COST = CostConfig(commission_pct=0.1425, commission_discount=0.28, tax_pct=0.3)
NO_COST = CostConfig(commission_pct=0.0, commission_discount=1.0, tax_pct=0.0)


def weekdays(start: date, count: int) -> list[date]:
    return [d.date() for d in pd.bdate_range(start, periods=count)]


# --- 換股日 -----------------------------------------------------------------

def schedule_of(days: list[date], covered: date | None = None) -> list[Rebalance]:
    return rebalance_schedule(days, start=days[0], end=days[-1], available_day=10, covered_through=covered or days[-1])


def test_signal_is_last_trading_day_of_the_available_week() -> None:
    # 2026-10-10 是週六：D = 10/12（週一），那一週的最後一個交易日 10/16，下一個交易日 10/19
    days = weekdays(date(2026, 10, 1), 20)
    assert schedule_of(days) == [Rebalance(date(2026, 10, 12), date(2026, 10, 16), date(2026, 10, 19))]


def test_available_day_is_strictly_after_the_tenth() -> None:
    # 2026-09-10 是週四、交易日：D 是 09/11（週五），訊號日同一天
    days = weekdays(date(2026, 9, 1), 15)
    assert schedule_of(days) == [Rebalance(date(2026, 9, 11), date(2026, 9, 11), date(2026, 9, 14))]


def test_signal_uses_saturday_makeup_trading_day() -> None:
    # 2017-02-18（週六）補班交易日：D = 02/13，那一週的最後一個交易日是週六
    days = sorted([*weekdays(date(2017, 2, 6), 15), date(2017, 2, 18)])
    assert schedule_of(days)[0] == Rebalance(date(2017, 2, 13), date(2017, 2, 18), date(2017, 2, 20))


def test_holiday_closes_the_week_early() -> None:
    # 2026-02-11（封關）之後休市到 02/23：訊號日 02/11，成交日 02/23
    days = [d for d in weekdays(date(2026, 2, 2), 20) if not date(2026, 2, 12) <= d <= date(2026, 2, 20)]
    assert schedule_of(days)[0] == Rebalance(date(2026, 2, 11), date(2026, 2, 11), date(2026, 2, 23))


def test_trade_day_is_none_when_next_trading_day_is_not_in_data() -> None:
    days = weekdays(date(2026, 11, 2), 10)      # 到 11/13（週五）
    assert schedule_of(days) == [Rebalance(date(2026, 11, 11), date(2026, 11, 13), None)]


def test_incomplete_week_is_not_scheduled() -> None:
    days = weekdays(date(2026, 11, 2), 8)       # 到 11/11（週三）：D 當天，那一週還沒過完
    assert schedule_of(days) == []
    # 週五休市：資料涵蓋到週五（含休市日）就算過完
    days = [d for d in weekdays(date(2026, 11, 2), 10) if d != date(2026, 11, 13)]
    assert schedule_of(days, covered=date(2026, 11, 12)) == []
    assert schedule_of(days, covered=date(2026, 11, 13)) == [Rebalance(date(2026, 11, 11), date(2026, 11, 12), None)]


def test_schedule_is_filtered_by_signal_day() -> None:
    days = weekdays(date(2026, 9, 1), 45)
    schedule = rebalance_schedule(days, start=date(2026, 10, 1), end=days[-1], available_day=10, covered_through=days[-1])
    assert [r.signal for r in schedule] == [date(2026, 10, 16)]


# --- 排名 -------------------------------------------------------------------

def rank_panel(values: dict[str, list[float]], days: list[date], eligible: dict[str, list[bool]] | None = None) -> Panel:
    frame = pd.DataFrame(values, index=pd.Index(days, name="date"), dtype=float)
    flags = pd.DataFrame(eligible, index=frame.index, dtype=bool) if eligible else frame.notna()
    empty = pd.DataFrame(False, index=frame.index, columns=frame.columns)
    return Panel(open=frame, high=frame, low=frame, close=frame, raw_close=frame, raw_open=frame, reference=frame,
                 volume=frame, value=frame, eligible=flags, limit_up=empty, limit_down=empty)


def test_rankings_use_signal_day_factor_and_top_quantile() -> None:
    days = [date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19)]
    codes = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    panel = rank_panel({c: [1.0, 1.0, 1.0] for c in codes}, days)
    # 訊號日的因子值：J 最大、A 最小；其他日子的值不應該被用到
    factor = pd.DataFrame({c: [100.0 - i, float(i), -5.0] for i, c in enumerate(codes)}, index=panel.days)
    liquidity = pd.DataFrame({c: [0.0, 10.0 - i, 0.0] for i, c in enumerate(codes)}, index=panel.days)
    rebalance = Rebalance(date(2026, 10, 12), date(2026, 10, 16), date(2026, 10, 19))
    [item] = build_rankings(panel, factor, liquidity, [rebalance], quantiles=5, min_stocks=5)
    assert item.by_factor == tuple(reversed(codes))
    assert item.top_group == ("I", "J")
    assert item.by_value == ("I", "J")                  # 成交金額 I 比 J 大
    assert item.ranking(TOP_FACTOR)[0] == "J"
    assert item.ranking(TOP_VALUE)[0] == "I"


def test_rankings_exclude_ineligible_stocks() -> None:
    days = [date(2026, 10, 16)]
    panel = rank_panel({"A": [1.0], "B": [1.0], "C": [1.0]}, days, {"A": [True], "B": [False], "C": [True]})
    factor = pd.DataFrame({"A": [1.0], "B": [9.0], "C": [2.0]}, index=panel.days)
    [item] = build_rankings(panel, factor, factor, [Rebalance(days[0], days[0], None)], quantiles=2, min_stocks=2)
    assert item.by_factor == ("C", "A")


def test_rankings_empty_when_too_few_stocks() -> None:
    days = [date(2026, 10, 16)]
    panel = rank_panel({"A": [1.0], "B": [1.0]}, days)
    factor = pd.DataFrame({"A": [1.0], "B": [2.0]}, index=panel.days)
    [item] = build_rankings(panel, factor, factor, [Rebalance(days[0], days[0], None)], quantiles=2, min_stocks=3)
    assert item.by_factor == item.by_value == item.top_group == ()


def test_factor_ties_are_broken_by_code() -> None:
    days = [date(2026, 10, 16)]
    panel = rank_panel({"A": [1.0], "B": [1.0], "C": [1.0]}, days)     # 面板的欄位依代號排序（build_panel）
    factor = pd.DataFrame({"A": [5.0], "B": [5.0], "C": [1.0]}, index=panel.days)
    [item] = build_rankings(panel, factor, factor, [Rebalance(days[0], days[0], None)], quantiles=2, min_stocks=2)
    assert item.by_factor == ("A", "B", "C")


def test_random_plan_is_reproducible_and_stays_in_top_group() -> None:
    rebalance = Rebalance(date(2026, 10, 12), date(2026, 10, 16), date(2026, 10, 19))
    item = RebalanceRanking(rebalance, ("A", "B", "C", "D"), ("B", "C"), ("B", "C", "D"))
    first = random_plan([item], random.Random(7))
    assert first == random_plan([item], random.Random(7))
    assert sorted(first[0][1]) == ["B", "C", "D"]


# --- 新名單 -----------------------------------------------------------------

def test_new_list_keeps_held_stock_when_substituting() -> None:
    # 第 2 名買不到，往下遞補時碰到本來要賣掉的持股 D：直接留任，不會賣了又買回
    chosen, unbuyable = new_list(["A", "B", "C", "D"], {"D"}, 3, lambda code: code != "B")
    assert chosen == ["A", "C", "D"]
    assert unbuyable == ["B"]


def test_new_list_stops_at_n() -> None:
    chosen, unbuyable = new_list(["A", "B", "C"], set(), 2, lambda code: True)
    assert chosen == ["A", "B"]
    assert unbuyable == []


def test_new_list_held_counts_even_if_not_buyable() -> None:
    chosen, unbuyable = new_list(["A", "B"], {"A"}, 2, lambda code: False)
    assert chosen == ["A"]
    assert unbuyable == ["B"]


# --- 帳戶 -------------------------------------------------------------------

DAYS = weekdays(date(2026, 10, 12), 12)         # 10/12（一）～10/27（二）


def table(prices: dict[str, list[float | None]], *, raw_open: dict[str, list[float | None]] | None = None,
          adj_open: dict[str, list[float | None]] | None = None, adj_close: dict[str, list[float | None]] | None = None,
          reference: dict[str, list[float | None]] | None = None, days: list[date] = DAYS) -> PriceTable:
    """prices：未還原收盤。沒有另外指定時開盤 = 收盤、還原價 = 未還原、參考價 = 前一日收盤。"""
    def array(data: dict[str, list[float | None]]) -> np.ndarray:
        return np.array([data[code] for code in prices], dtype=float).T

    close = array(prices)
    opens = array(raw_open) if raw_open else close.copy()
    references = array(reference) if reference else np.vstack([np.full(close.shape[1], np.nan),
                                                               pd.DataFrame(close).ffill().to_numpy()[:-1]])
    return PriceTable.from_arrays(tuple(days), tuple(prices), raw_open=opens, raw_close=close,
                                  adj_open=array(adj_open) if adj_open else opens.copy(),
                                  adj_close=array(adj_close) if adj_close else close.copy(), reference=references)


def settings(top_n: int = 2, capital: float = 300000.0, cost: CostConfig = COST, min_commission: float = 20.0) -> AccountSettings:
    return AccountSettings(initial_capital=capital, max_positions=top_n, pick=TOP_FACTOR, cost=cost, min_commission=min_commission)


REB1 = Rebalance(DAYS[0], DAYS[0], DAYS[1])     # 訊號日 10/12，10/13 開盤成交
REB2 = Rebalance(DAYS[5], DAYS[5], DAYS[6])     # 訊號日 10/19，10/20 開盤成交


def flat(value: float) -> list[float | None]:
    return [value] * len(DAYS)


def simulate(prices: PriceTable, plan, config: AccountSettings | None = None, execution: str = OPEN, *, end: date = DAYS[-1],
             delisted=(), close_out: bool = True):
    return simulate_account(prices, plan, config or settings(), execution, start=DAYS[0], end=end, delisted=delisted,
                            close_out=close_out)


def test_buy_at_next_open_with_shares_rounded_down_and_min_commission() -> None:
    prices = table({"A": flat(100.0), "B": flat(30.0)})
    result = simulate(prices, [(REB1, ("A", "B"))], settings(capital=30000.0))
    # 單筆 = 30000 / 2 = 15000：A 149 股（14,900 + 手續費 20 ≤ 15,000）
    [first, second] = sorted(result.trades, key=lambda t: t.code)
    assert (first.code, first.shares, first.buy_amount, first.buy_fee, first.buy_day) == ("A", 149, 14900.0, 20.0, DAYS[1])
    # 買完 A 剩現金 15,080，單筆仍 15,000：B 499 股（14,970 + 手續費 20 ≤ 15,000）
    assert (second.code, second.shares, second.buy_amount) == ("B", 499, 14970.0)


def test_equity_identity_and_pnl_sum() -> None:
    prices = table({"A": [100, 100, 104, 106, 103, 101, 99, 100, 102, 104, 106, 108],
                    "B": [50, 50, 49, 48, 47, 46, 45, 46, 47, 48, 49, 50],
                    "C": [20, 20, 20.5, 21, 21.5, 22, 22.5, 23, 23.5, 24, 24.5, 25]})
    result = simulate(prices, [(REB1, ("A", "B")), (REB2, ("C", "A"))])
    assert result.cash[-1] + result.holdings[-1] == pytest.approx(result.equity.iloc[-1])
    assert result.holdings[-1] == 0                     # 期末全部結算
    assert result.equity.iloc[-1] - 300000 == pytest.approx(sum(t.pnl for t in result.trades))
    assert result.positions[0] == 0                     # 訊號日收盤還沒有持股
    assert result.equity.iloc[0] == 300000


def test_final_day_settles_at_close_with_costs() -> None:
    prices = table({"A": flat(100.0)})
    result = simulate(prices, [(REB1, ("A",))], settings(top_n=1))
    [trade] = result.trades
    assert trade.reason == FINAL
    assert trade.sell_day == DAYS[-1]
    assert trade.sell_fee == pytest.approx(max(trade.sell_amount * 0.001425 * 0.28, 20.0))
    assert trade.tax == pytest.approx(trade.sell_amount * 0.003)


def test_retained_stock_is_not_traded() -> None:
    prices = table({"A": flat(100.0), "B": flat(50.0), "C": flat(25.0)})
    result = simulate(prices, [(REB1, ("A", "B")), (REB2, ("A", "C"))])
    log = result.logs[1]
    assert (log.kept, log.sold, log.bought) == (("A",), ("B",), ("C",))
    assert [t.code for t in result.trades if t.reason == DROPPED] == ["B"]
    assert [t.buy_day for t in result.trades if t.code == "A"] == [DAYS[1]]


def test_limit_up_open_is_unbuyable_and_substituted() -> None:
    # A 在 10/13 開盤等於漲停（前一日收 100，漲停 110）：買不到，遞補 C
    opens = {"A": [100, 110, *([110.0] * 10)], "B": flat(50.0), "C": flat(25.0)}
    prices = table({"A": [100, *([110.0] * 11)], "B": flat(50.0), "C": flat(25.0)}, raw_open=opens,
                   reference={"A": [None, 100, *([110.0] * 10)], "B": flat(50.0), "C": flat(25.0)})
    result = simulate(prices, [(REB1, ("A", "B", "C"))])
    log = result.logs[0]
    assert log.unbuyable == ("A",)
    assert log.bought == ("B", "C")
    assert result.unbuyable_count == 1


def test_open_above_limit_is_not_treated_as_limit() -> None:
    # 以「等於」判斷（14.1、D56）：開盤 111 不等於漲停價 110，照常買進
    opens = {"A": [100, 111, *([111.0] * 10)]}
    prices = table({"A": flat(100.0)}, raw_open=opens, reference={"A": [None, 100, *([100.0] * 10)]})
    result = simulate(prices, [(REB1, ("A",))], settings(top_n=1))
    assert result.logs[0].bought == ("A",)


def test_no_trade_on_trade_day_is_unbuyable() -> None:
    closes = {"A": [100, None, *([100.0] * 10)], "B": flat(50.0)}
    prices = table(closes)
    result = simulate(prices, [(REB1, ("A", "B"))], settings(top_n=1))
    assert result.logs[0].unbuyable == ("A",)
    assert result.logs[0].bought == ("B",)


def test_limit_down_open_defers_sale_to_next_open() -> None:
    # B 在 10/20 開盤跌停（前一日收 50，跌停 45）：順延；10/21 開盤 44.5 賣出
    closes = {"A": flat(100.0), "B": [50, 50, 50, 50, 50, 50, 45, 44.5, 44.5, 44.5, 44.5, 44.5], "C": flat(25.0)}
    opens = {"A": flat(100.0), "B": [50, 50, 50, 50, 50, 50, 45, 44.5, 44.5, 44.5, 44.5, 44.5], "C": flat(25.0)}
    refs = {"A": flat(100.0), "B": [None, 50, 50, 50, 50, 50, 50, 45, 44.5, 44.5, 44.5, 44.5], "C": flat(25.0)}
    prices = table(closes, raw_open=opens, reference=refs)
    result = simulate(prices, [(REB1, ("A", "B")), (REB2, ("A", "C"))])
    assert result.logs[1].deferred == ("B",)
    trade = next(t for t in result.trades if t.code == "B")
    assert (trade.sell_day, trade.sell_price, trade.deferred_days, trade.reason) == (DAYS[7], 44.5, 1, DROPPED)
    assert result.deferred_positions == 1


def test_deferred_cash_waits_for_next_rebalance() -> None:
    closes = {"A": flat(100.0), "B": [50, 50, 50, 50, 50, 50, 45, 44.5, 44.5, 44.5, 44.5, 44.5], "C": flat(25.0)}
    refs = {"A": flat(100.0), "B": [None, 50, 50, 50, 50, 50, 50, 45, 44.5, 44.5, 44.5, 44.5], "C": flat(25.0)}
    prices = table(closes, reference=refs)
    result = simulate(prices, [(REB1, ("A", "B")), (REB2, ("A", "C"))], close_out=False)
    assert [p.code for p in result.open_positions] == ["A", "C"]    # B 賣出後沒有買新的
    assert result.cash[7] > result.cash[6]                          # 10/21 收回 B 的錢，留著


def test_deferred_stock_back_on_the_list_is_retained() -> None:
    # B 10/20 跌停賣不掉，第三次換股（10/23 開盤）B 回到名單：取消賣出，視為留任
    closes = {"A": flat(100.0), "B": [50, 50, 50, 50, 50, 50, *([45.0] * 6)], "C": flat(25.0)}
    # 跌停一路鎖死：開盤都等於當天的跌停價，順延到第三次換股
    refs = {"A": flat(100.0), "B": [None, 50, 50, 50, 50, 50, 50, *([50.0] * 5)], "C": flat(25.0)}
    prices = table(closes, reference=refs)
    reb3 = Rebalance(DAYS[8], DAYS[8], DAYS[9])
    result = simulate(prices, [(REB1, ("A", "B")), (REB2, ("A", "C")), (reb3, ("B", "A"))], close_out=False)
    assert result.logs[2].kept == ("B", "A")
    assert result.logs[2].sold == ("C",)
    assert all(t.code != "B" for t in result.trades)
    position = next(p for p in result.open_positions if p.code == "B")
    assert position.selling is False


def test_adjusted_prices_reinvest_dividends_in_position_value() -> None:
    # 10/15 除息：未還原 100 → 95，因子 0.95：還原價 10/15 之前都打 0.95 折
    raw = [100, 100, 100, 95, 95, 95, 95, 95, 95, 95, 95, 95]
    adj = [95.0, 95.0, 95.0, 95.0, 95.0, 95.0, 95.0, 95.0, 95.0, 95.0, 95.0, 95.0]
    prices = table({"A": raw}, adj_open={"A": adj}, adj_close={"A": adj}, reference={"A": [None, 100, 100, 95, 95, 95, 95, 95, 95, 95, 95, 95]})
    result = simulate(prices, [(REB1, ("A",))], settings(top_n=1, cost=NO_COST, min_commission=0.0))
    [trade] = result.trades
    assert trade.sell_amount == pytest.approx(trade.buy_amount)     # 還原後沒有漲跌：股利再投入
    assert trade.shares * 95 < trade.sell_amount                    # 帳面價 95 × 股數 < 含股利的賣出金額


def test_close_execution_trades_on_signal_day() -> None:
    prices = table({"A": flat(100.0)})
    result = simulate(prices, [(REB1, ("A",))], settings(top_n=1), CLOSE)
    assert result.logs[0].trade == DAYS[0]
    assert result.positions[0] == 1
    assert result.equity.iloc[0] < 300000                           # 當天付了手續費


def test_close_execution_limit_up_on_signal_close_is_unbuyable() -> None:
    closes = {"A": [110.0, *([110.0] * 11)], "B": flat(50.0)}
    prices = table(closes, reference={"A": flat(100.0), "B": flat(50.0)})
    result = simulate(prices, [(REB1, ("A", "B"))], settings(top_n=1), CLOSE)
    assert result.logs[0].unbuyable == ("A",)
    assert result.logs[0].bought == ("B",)


def test_delisted_stock_settles_at_last_close() -> None:
    closes = {"A": [100, 100, 100, 90, None, None, None, None, None, None, None, None], "B": flat(50.0)}
    prices = table(closes)
    result = simulate(prices, [(REB1, ("A", "B"))], delisted={"A"})
    trade = next(t for t in result.trades if t.code == "A")
    assert (trade.sell_day, trade.sell_price, trade.reason) == (DAYS[3], 90.0, "資料結束")


def test_suspended_stock_without_delisting_is_carried_at_last_close() -> None:
    closes = {"A": [100, 100, 100, 90, None, None, None, None, None, None, None, None]}
    prices = table(closes)
    result = simulate(prices, [(REB1, ("A",))], settings(top_n=1, cost=NO_COST, min_commission=0.0), end=DAYS[8], close_out=False)
    [position] = result.open_positions
    assert position.value == pytest.approx(position.shares * 90)


def test_suspended_stock_sold_after_resuming() -> None:
    closes = {"A": [100, 100, 100, 100, 100, 100, None, None, 100, 100, 100, 100], "B": flat(50.0)}
    prices = table(closes, reference={"A": [None, *([100.0] * 11)], "B": flat(50.0)})
    result = simulate(prices, [(REB1, ("A",)), (REB2, ("B",))], settings(top_n=1), close_out=False)
    trade = next(t for t in result.trades if t.code == "A")
    assert trade.sell_day == DAYS[8]
    assert trade.deferred_days == 2


def test_cannot_afford_one_share_is_skipped_without_substitution() -> None:
    prices = table({"A": flat(40000.0), "B": flat(10.0)})
    result = simulate(prices, [(REB1, ("A", "B"))], settings(top_n=2, capital=60000.0), close_out=False)
    assert result.logs[0].skipped_cash == ("A",)
    assert result.logs[0].bought == ("B",)
    assert result.skipped_cash_count == 1


def test_no_rebalance_when_ranking_is_empty() -> None:
    prices = table({"A": flat(100.0), "B": flat(50.0)})
    result = simulate(prices, [(REB1, ("A",)), (REB2, ())], settings(top_n=1), close_out=False)
    assert [p.code for p in result.open_positions] == ["A"]
    assert len(result.logs) == 1


def test_trade_day_after_end_is_not_executed() -> None:
    prices = table({"A": flat(100.0)})
    result = simulate(prices, [(REB1, ("A",))], settings(top_n=1), end=DAYS[0], close_out=False)
    assert result.positions[0] == 0
    assert result.logs == ()


def test_hand_computed_rebalance() -> None:
    """手算：30 萬、2 檔，10/20 換股賣 B 買 C。"""
    closes = {"A": [100, 100, 102, 104, 106, 108, 110, 112, 112, 112, 112, 112],
              "B": [50, 50, 51, 52, 53, 54, 55, 55, 55, 55, 55, 55],
              "C": [25, 25, 25, 25, 25, 25, 25, 26, 26, 26, 26, 26]}
    opens = {"A": [100, 100, 102, 104, 106, 108, 109, 112, 112, 112, 112, 112],
             "B": [50, 50, 51, 52, 53, 54, 56, 55, 55, 55, 55, 55],
             "C": [25, 25, 25, 25, 25, 25, 25, 26, 26, 26, 26, 26]}
    prices = table(closes, raw_open=opens)
    result = simulate(prices, [(REB1, ("A", "B")), (REB2, ("A", "C"))], close_out=False)
    fee = 0.001425 * 0.28
    # 10/13 開盤：單筆 150,000。A 100 元 → 1,499 股 149,900、手續費 59.81；B 50 元 → 2,998 股 149,900、手續費 59.81
    a_shares, b_shares = 1499, 2998
    buy_fee = 149900 * fee
    assert buy_fee == pytest.approx(59.8101)
    cash = 300000 - 2 * (149900 + buy_fee)
    assert cash == pytest.approx(80.3798)
    # 10/20 開盤：B 56 元賣出，金額 167,888、手續費 66.99、證交稅 503.66
    sell = b_shares * 56
    sell_fee, tax = sell * fee, sell * 0.003
    assert (sell, sell_fee, tax) == (167888, pytest.approx(66.9873), pytest.approx(503.664))
    cash += sell - sell_fee - tax
    # 權益 = 現金 167,397.73 + A 1,499 股 × 109 = 330,788.73；單筆 165,394.37；C 25 元 → 6,613 股
    equity = cash + a_shares * 109
    assert equity == pytest.approx(330788.7285)
    c_shares = 6613
    assert result.logs[1].sold == ("B",)
    c_trade = next(p for p in result.open_positions if p.code == "C")
    assert c_trade.shares == c_shares
    cash -= c_shares * 25 * (1 + fee)
    assert result.cash[6] == pytest.approx(cash)
    assert result.holdings[6] == pytest.approx(a_shares * 110 + c_shares * 25)


# --- 績效 -------------------------------------------------------------------

def test_index_series_starts_at_capital() -> None:
    days = DAYS[:3]
    series = index_series({days[0]: 100.0, days[1]: 110.0, days[2]: 121.0}, days, 300000.0)
    assert series.tolist() == pytest.approx([300000.0, 330000.0, 363000.0])


def test_series_metrics() -> None:
    days = [date(2024, 1, 1), date(2025, 1, 1)]
    metrics = series_metrics(pd.Series([100.0, 120.0], index=days))
    assert metrics["return"] == pytest.approx(20.0)
    assert metrics["max_drawdown"] == 0


def test_yearly_returns() -> None:
    days = [date(2024, 1, 2), date(2024, 12, 31), date(2025, 12, 31)]
    result = yearly_returns(pd.Series([100.0, 110.0, 99.0], index=days))
    assert result.loc[2024] == pytest.approx(10.0)
    assert result.loc[2025] == pytest.approx(-10.0)


def test_account_metrics_turnover_and_costs() -> None:
    prices = table({"A": flat(100.0)})
    result = simulate(prices, [(REB1, ("A",))], settings(top_n=1))
    days = list(result.days)
    bench = pd.Series(np.linspace(100, 110, len(days)), index=days)
    metrics = account_metrics(result, bench, bench)
    years = (days[-1] - days[0]).days / 365.25
    assert metrics["turnover"] == pytest.approx(result.bought_amount / result.equity.mean() / years)
    assert metrics["costs"] == pytest.approx(sum(t.buy_fee + t.sell_fee + t.tax for t in result.trades))
    assert metrics["index_return"] == pytest.approx(10.0)
    assert metrics["final_equity"] == pytest.approx(result.equity.iloc[-1])

