"""交易回測的進場訊號、出場、帳戶模擬與績效（RESEARCH_PLAN.md 4.2；PROJECT_PLAN.md 5.4、5.5）。"""

import math
import random
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from conftest import REPO_CONFIG_DIR, load_fixture, read_yaml, to_bars, write_yaml
from screener.analysis.adjustment import adjust_bars, event_factors
from screener.backtest.signals import StockHistory
from screener.backtest.trades import (END_OF_DATA, AccountDay, AccountResult, AccountSettings, Candidate, Trade,
                                      account_metrics, find_candidates, order_candidates, simulate_account)
from screener.config import ConfigError, CostConfig, load_backtest_config, load_config
from screener.engine import Engine
from screener.models import Market, StockInfo

CONFIG = load_config(REPO_CONFIG_DIR)
ENGINE = Engine.from_config(CONFIG.rules)
COST = CostConfig(commission_pct=0.1425, commission_discount=0.28, tax_pct=0.3)
INFO = StockInfo(code="1111", name="股1111", market=Market.TSE, group="PCB", subcategory="銅箔基板")


# --- 進場訊號與出場 -----------------------------------------------------------

# bull_zigzag 的最後一天（02-05）收盤 114 = 昨高，趨勢多頭；之後接上 closes。
# 第一天開盤 120、收盤 124 時符合進場：E1、124 > 昨高 114、MA5 115.0 > 昨日 114.8。以 114 計算的漲停價為 125。
ZIGZAG = to_bars(load_fixture("bull_zigzag"))


def zigzag_then(*closes: float, first_open: float = 120.0) -> StockHistory:
    days = pd.bdate_range(ZIGZAG.index[-1] + timedelta(days=1), periods=len(closes)).date
    extra = pd.DataFrame({"open": [first_open, *closes[1:]], "high": list(closes), "low": [min(first_open, closes[0]),
                          *closes[1:]], "close": list(closes), "volume": [1000] * len(closes)},
                         index=pd.Index(days, name="date"))
    bars = pd.concat([ZIGZAG, extra])
    return StockHistory(INFO, bars, adjust_bars(bars, [], []), event_factors(bars, [], []))


def candidates(history: StockHistory) -> list[Candidate]:
    return find_candidates(history, ENGINE, CONFIG.rules.trend, ZIGZAG.index[0], history.bars.index[-1], pick_ma=5)


def test_exit_when_close_falls_below_ma5() -> None:
    # 124 → 126（MA5 116.8）→ 118 < MA5 118.2
    history = zigzag_then(124.0, 126.0, 118.0, 130.0)
    (c,) = candidates(history)

    assert (c.entry_day, c.entry_price) == (history.bars.index[24], 124.0)
    assert (c.exit_day, c.exit_price, c.exit_reason, c.held_days, c.delayed) == (
        history.bars.index[26], 118.0, "跌破均線", 2, False)
    assert c.ma_gap_pct == pytest.approx((124 / 115 - 1) * 100) and c.volume_ratio == 1.0
    assert c.growth == ((history.bars.index[25], pytest.approx(126 / 124)), (history.bars.index[26], pytest.approx(118 / 124)))


def test_exit_on_the_fifth_trading_day() -> None:
    history = zigzag_then(124.0, 126.0, 128.0, 130.0, 132.0, 134.0, 136.0)
    (c,) = candidates(history)

    assert (c.exit_day, c.exit_price, c.exit_reason, c.held_days) == (history.bars.index[29], 134.0, "持有到期", 5)


def test_exit_is_delayed_while_locked_limit_down() -> None:
    # 112 為 124 的跌停價（111.6 進位到 112），賣不掉；隔天 105 沒有跌停，賣出
    history = zigzag_then(124.0, 112.0, 105.0)
    (c,) = candidates(history)

    assert (c.exit_day, c.exit_price, c.exit_reason, c.held_days, c.delayed) == (
        history.bars.index[26], 105.0, "跌破均線", 2, True)


def test_no_entry_when_closing_at_limit_up() -> None:
    assert candidates(zigzag_then(125.0, 126.0)) == []


def test_close_above_the_computed_limit_is_not_treated_as_locked() -> None:
    # 官方參考價有疑點的除權息日，收盤可能高於算出的漲停價（14.2）；與 14.1 相同，只有「等於」才算鎖死
    (c,) = candidates(zigzag_then(125.5, 126.0))

    assert c.entry_price == 125.5


def test_position_still_open_when_data_ends() -> None:
    history = zigzag_then(124.0, 126.0)
    (c,) = candidates(history)

    assert (c.exit_day, c.exit_price, c.exit_reason, c.held_days) == (history.bars.index[-1], 126.0, END_OF_DATA, 1)


# --- 帳戶 -------------------------------------------------------------------

DAYS = [date(2022, 6, 6) + timedelta(days=i) for i in range(10)]


def cand(code: str, entry: int, price: float, exit_: int, exit_price: float, *, gap: float = 0.0,
         ratio: float | None = 1.0, path: dict[int, float] | None = None) -> Candidate:
    """DAYS[entry] 收盤買進、DAYS[exit_] 收盤賣出；path 為中間各天的收盤（未給的日子沒有交易）。"""
    closes = {**(path or {}), exit_: exit_price}
    return Candidate(code=code, name=f"股{code}", entry_day=DAYS[entry], entry_price=price, ma_gap_pct=gap,
                     volume_ratio=ratio, exit_day=DAYS[exit_], exit_price=exit_price, exit_reason="跌破均線",
                     held_days=exit_ - entry, delayed=False,
                     growth=tuple((DAYS[i], close / price) for i, close in sorted(closes.items())))


def settings(cap: int = 5, capital: float = 300_000, pick: str = "nearest_ma") -> AccountSettings:
    return AccountSettings(initial_capital=capital, max_positions=cap, pick=pick, cost=COST, min_commission=20)


def simulate(cands, cap: int = 5, capital: float = 300_000, pick: str = "nearest_ma", end: int = 9, rng=None):
    return simulate_account(cands, DAYS, DAYS[end], settings(cap, capital, pick), rng=rng)


def test_hand_calculated_trade_matches() -> None:
    # 2303 聯電 2022-06-06 收 52.0 買進、06-10 收 51.2 賣出；權益 30 萬、上限 5 檔
    result = simulate([cand("2303", 0, 52.0, 4, 51.2)])

    (t,) = result.trades
    assert t.shares == 1153                                     # 60,000 / 52 = 1153.8
    assert t.buy_amount == pytest.approx(59_956)
    assert t.buy_fee == pytest.approx(59_956 * 0.001425 * 0.28)     # 23.92
    assert t.sell_amount == pytest.approx(1153 * 51.2)              # 59,033.6
    assert t.sell_fee == pytest.approx(59_033.6 * 0.001425 * 0.28)  # 23.55
    assert t.tax == pytest.approx(59_033.6 * 0.003)                 # 177.10
    assert t.pnl == pytest.approx(-1_146.97, abs=0.01)
    assert t.return_pct == pytest.approx(-1_146.97 / 59_956 * 100, abs=1e-4)


def test_minimum_commission_applies_to_small_trades() -> None:
    (t,) = simulate([cand("1111", 0, 10.0, 2, 10.0)], cap=10, capital=100_000).trades     # 單筆約 1 萬元

    assert t.buy_fee == 20 and t.sell_fee == 20


def test_cap_and_pick_order() -> None:
    cands = [cand("1111", 0, 10.0, 3, 10.0, gap=3.0, ratio=1.5), cand("2222", 0, 10.0, 3, 10.0, gap=1.0, ratio=1.2),
             cand("3333", 0, 10.0, 3, 10.0, gap=2.0, ratio=3.0)]

    nearest = simulate(cands, cap=2, pick="nearest_ma")
    volume = simulate(cands, cap=2, pick="volume_ratio")

    assert sorted(t.code for t in nearest.trades) == ["2222", "3333"]
    assert sorted(t.code for t in volume.trades) == ["1111", "3333"]
    assert (nearest.days[0].candidates, nearest.days[0].bought, nearest.days[0].rejected_full) == (3, 2, 1)
    assert max(d.positions for d in nearest.days) == 2


def test_ties_are_broken_by_code() -> None:
    cands = [cand("2222", 0, 10.0, 3, 10.0), cand("1111", 0, 10.0, 3, 10.0)]

    assert [c.code for c in order_candidates(cands, "nearest_ma", None)] == ["1111", "2222"]
    assert [c.code for c in order_candidates(cands, "volume_ratio", None)] == ["1111", "2222"]


def test_size_is_current_equity_divided_by_cap() -> None:
    # 1111 先買進並上漲 50%；2222 隔天買進時，單筆金額以當天權益（含 1111 的市值）÷ 2 計算
    cands = [cand("1111", 0, 100.0, 5, 150.0, path={1: 150.0}), cand("2222", 1, 50.0, 5, 50.0)]
    result = simulate(cands, cap=2, capital=100_000)

    first = next(t for t in result.trades if t.code == "1111")
    second = next(t for t in result.trades if t.code == "2222")
    cash_after_first = 100_000 - first.buy_amount - first.buy_fee
    target = (cash_after_first + first.buy_amount * 1.5) / 2
    assert second.buy_amount + second.buy_fee <= min(target, cash_after_first)
    assert second.buy_amount + 50.0 + second.buy_fee > min(target, cash_after_first)     # 已是買得起的最大股數
    assert target > 50_000                                                             # 複利：比初始資金 ÷ 2 大


def test_held_and_just_sold_stocks_are_not_bought_again() -> None:
    cands = [cand("1111", 0, 10.0, 2, 10.0), cand("1111", 1, 10.0, 4, 10.0), cand("1111", 2, 10.0, 5, 10.0),
             cand("1111", 3, 10.0, 6, 10.0)]
    result = simulate(cands)

    assert [(t.entry_day, t.exit_day) for t in result.trades] == [(DAYS[0], DAYS[2]), (DAYS[3], DAYS[6])]


def test_unaffordable_stock_is_skipped_for_a_cheaper_one() -> None:
    result = simulate([cand("1111", 0, 2_000.0, 2, 2_000.0, gap=0.0), cand("2222", 0, 50.0, 2, 50.0, gap=1.0)],
                      cap=1, capital=1_000)

    assert [t.code for t in result.trades] == ["2222"]
    assert result.days[0].skipped_cash == 1 and result.trades[0].shares == 19      # 19 × 50 + 20 = 970


def test_no_buying_after_the_end_but_positions_are_closed() -> None:
    result = simulate([cand("1111", 2, 10.0, 6, 11.0), cand("2222", 4, 10.0, 6, 10.0)], end=3)

    assert [t.code for t in result.trades] == ["1111"]
    assert result.days[-1].day == DAYS[6] and result.days[-1].positions == 0


def test_cash_and_holdings_add_up() -> None:
    cands = [cand("1111", 0, 20.0, 3, 22.0, path={1: 21.0}), cand("2222", 1, 30.0, 4, 27.0, path={2: 29.0, 3: 28.0}),
             cand("3333", 3, 40.0, 6, 41.0)]
    result = simulate(cands, cap=2)

    final = result.days[-1]
    assert final.positions == 0 and final.holdings_value == 0
    assert final.equity - 300_000 == pytest.approx(sum(t.pnl for t in result.trades))
    day1 = result.days[1]                                       # 1111 收 21，2222 當天買進
    t1 = next(t for t in result.trades if t.code == "1111")
    t2 = next(t for t in result.trades if t.code == "2222")
    assert day1.holdings_value == pytest.approx(t1.shares * 21.0 + t2.shares * 30.0)
    assert day1.cash == pytest.approx(300_000 - t1.buy_amount - t1.buy_fee - t2.buy_amount - t2.buy_fee)


def test_holding_value_carries_forward_on_days_without_trading() -> None:
    result = simulate([cand("1111", 0, 10.0, 4, 12.0, path={1: 11.0})], cap=1, capital=10_000)
    (t,) = result.trades

    assert [round(d.holdings_value, 6) for d in result.days[:4]] == [round(t.shares * p, 6) for p in (10, 11, 11, 11)]


def test_random_pick_is_reproducible_with_the_same_seed() -> None:
    cands = [cand(str(1000 + i), 0, 10.0, 3, 10.0) for i in range(8)]

    first = simulate(cands, cap=3, pick="random", rng=random.Random(42))
    again = simulate(cands, cap=3, pick="random", rng=random.Random(42))

    assert [t.code for t in first.trades] == [t.code for t in again.trades]
    assert len(first.trades) == 3


def test_random_pick_requires_rng() -> None:
    with pytest.raises(ValueError, match="需要 rng"):
        order_candidates([cand("1111", 0, 10.0, 3, 10.0)], "random", None)


# --- 績效 -------------------------------------------------------------------

def trade(pnl: float, exit_: int) -> Trade:
    return Trade(code="1111", name="股1111", entry_day=DAYS[0], entry_price=10.0, shares=100, buy_amount=1_000.0,
                 buy_fee=0.0, exit_day=DAYS[exit_], exit_price=10.0, sell_amount=1_000.0 + pnl, sell_fee=0.0, tax=0.0,
                 exit_reason="跌破均線", held_days=exit_, delayed=False)


def test_metrics_on_a_hand_made_account() -> None:
    # 權益 100 → 120 → 90 → 130：最大回撤 (90 / 120 − 1) = −25%，第 2 天創高、第 4 天回復（2 個交易日）
    days = tuple(AccountDay(day=DAYS[i], cash=v, holdings_value=0.0, positions=i % 2, candidates=0, bought=0,
                            rejected_full=0, skipped_cash=0) for i, v in enumerate([100.0, 120.0, 90.0, 130.0]))
    result = AccountResult(settings=settings(cap=1, capital=100.0), trades=(trade(-1, 1), trade(-2, 2), trade(5, 3)),
                           days=days)
    benchmark = pd.Series(100.0, index=DAYS[:4])

    m = account_metrics(result, benchmark)
    assert m["total_return"] == pytest.approx(30.0)
    assert m["max_drawdown"] == pytest.approx(-25.0)
    assert m["drawdown_days"] == 2 and m["drawdown_recovered"]
    assert (m["trades"], m["win_rate"], m["max_losing_streak"]) == (3, pytest.approx(100 / 3), 2)
    assert m["payoff"] == pytest.approx(5 / 1.5)
    assert m["flat_days"] == pytest.approx(50.0) and m["avg_positions"] == pytest.approx(0.5)
    assert m["benchmark_return"] == pytest.approx(0.0)


def test_ruined_account_has_minus_100_percent_cagr() -> None:
    # 虧光之後最低手續費仍照收，現金可能變成負數：年化記為 −100%，不是無效值
    days = tuple(AccountDay(day=DAYS[i], cash=v, holdings_value=0.0, positions=0, candidates=0, bought=0,
                            rejected_full=0, skipped_cash=0) for i, v in enumerate([100.0, 1.0, -5.0]))
    result = AccountResult(settings=settings(cap=1, capital=100.0), trades=(), days=days)

    m = account_metrics(result, pd.Series(100.0, index=DAYS[:3]))
    assert m["cagr"] == -100.0 and m["total_return"] == pytest.approx(-105.0)


# --- 設定 -------------------------------------------------------------------

def test_repo_trade_config() -> None:
    trade = load_backtest_config(REPO_CONFIG_DIR / "backtest.yaml").trade

    assert (trade.initial_capital, trade.max_positions, trade.picks) == (300_000, (3, 5, 10), ("nearest_ma", "volume_ratio"))
    assert (trade.pick_ma, trade.random_runs, trade.min_commission) == (5, 100, 20)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [("picks", ["nearest_ma", "momentum"], "picks：應為 nearest_ma、volume_ratio 其中之一"),
     ("max_positions", [5, 3], "max_positions：應由小到大且不重複"),
     ("max_positions", [0, 3], "max_positions：應為正整數陣列"),
     ("initial_capital", 0, "initial_capital：必須大於 0"),
     ("min_commission", -1, "min_commission：不可為負數")],
)
def test_invalid_trade_config_is_rejected(tmp_path: Path, key: str, value, message: str) -> None:
    data = read_yaml(REPO_CONFIG_DIR / "backtest.yaml")
    data["trade"][key] = value
    write_yaml(tmp_path / "backtest.yaml", data)

    with pytest.raises(ConfigError, match=message):
        load_backtest_config(tmp_path / "backtest.yaml")


def test_factor_pick_prefers_high_values_and_puts_missing_last() -> None:
    def with_factor(code: str, value: float) -> Candidate:
        return replace(cand(code, 0, 10.0, 3, 10.0), factors={"rev_yoy": value})

    cands = [with_factor("4444", math.nan), with_factor("3333", 0.1), with_factor("2222", 0.5), with_factor("1111", 0.1),
             with_factor("0050", math.nan)]

    assert [c.code for c in order_candidates(cands, "rev_yoy", None)] == ["2222", "1111", "3333", "0050", "4444"]
    assert sorted(t.code for t in simulate(cands, cap=2, pick="rev_yoy").trades) == ["1111", "2222"]


def test_factor_pick_needs_values_on_every_candidate() -> None:
    with pytest.raises(ValueError, match="未知的挑選方式：rev_yoy"):
        order_candidates([cand("1111", 0, 10.0, 3, 10.0)], "rev_yoy", None)
