"""歷史訊號、標記與報酬（PROJECT_PLAN.md 14.1）。"""

from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from conftest import REPO_CONFIG_DIR, TEST_STOCK, make_bars, set_bar
from screener.analysis.adjustment import adjust_bars, event_factors
from screener.backtest.signals import (StockHistory, add_flags, add_returns, dedupe, find_signals,
                                       forward_returns)
from screener.config import load_config
from screener.engine import Engine
from screener.models import DividendEvent

CONFIG = load_config(REPO_CONFIG_DIR)
ENGINE = Engine.from_config(CONFIG.rules)


def history(bars: pd.DataFrame, dividends=(), actions=()) -> StockHistory:
    return StockHistory(TEST_STOCK, bars, adjust_bars(bars, dividends, actions), event_factors(bars, dividends, actions))


def dividend(bars: pd.DataFrame, position: int, cash: float) -> DividendEvent:
    return DividendEvent(TEST_STOCK.code, bars.index[position], cash, 0.0)


def row(position: int, side: str = "long", **extra) -> dict:
    return {"position": position, "side": side, **extra}


# --- 產生訊號 ---------------------------------------------------------------

# 20 根收盤 100，第 21 根 110：形成三線多排（與回放測試相同的構造），之後再走 10 根
FORMS = make_bars([100.0] * 20 + [110.0] + [111.0] * 10)
FORMATION_DAY = FORMS.index[20]


def test_signals_match_replay_of_each_day() -> None:
    rows = find_signals(history(FORMS), ENGINE, CONFIG.rules.trend, FORMS.index[0], FORMS.index[-1])

    assert [(r["date"], r["rule_name"], r["side"], r["position"]) for r in rows] == [
        (FORMATION_DAY, "三線多排", "long", 20)]


def test_signals_never_use_later_bars() -> None:
    # 把形成當天之後的 K 棒改成崩跌，形成當天的訊號不可以改變
    crashed = FORMS.copy()
    crashed.loc[FORMS.index[21]:, ["open", "high", "low", "close"]] = 50.0

    def signals(bars):
        rows = find_signals(history(bars), ENGINE, CONFIG.rules.trend, FORMS.index[0], FORMATION_DAY)
        return [(r["date"], r["rule_id"]) for r in rows]

    assert signals(crashed) == signals(FORMS)


def test_signals_are_limited_to_the_period() -> None:
    rows = find_signals(history(FORMS), ENGINE, CONFIG.rules.trend, FORMS.index[21], FORMS.index[-1])

    assert rows == []


def test_adjusted_rules_use_adjusted_prices() -> None:
    # 第 21 根除息 10 元，收盤 100 → 90 是除息造成的。未還原價看成下跌而形成三線空排；還原後沒有跳空
    bars = make_bars([100.0] * 20 + [90.0] * 5)
    h = history(bars, [dividend(bars, 20, 10.0)])

    raw = find_signals(h, ENGINE, CONFIG.rules.trend, bars.index[0], bars.index[-1])
    adjusted = find_signals(h, ENGINE, CONFIG.rules.trend, bars.index[0], bars.index[-1], adjusted_rules=True)

    assert [r["rule_name"] for r in raw] == ["三線空排"]
    assert adjusted == []


def test_no_signals_before_the_sixth_trading_day_after_listing() -> None:
    # 形成日是第 21 根（索引 20）；掛牌日在第 16 根時，第 21 根正是掛牌後第六個交易日
    def signals(listed_index: int) -> list[int]:
        h = replace(history(FORMS), listed_on=FORMS.index[listed_index])
        return [r["position"] for r in find_signals(h, ENGINE, CONFIG.rules.trend, FORMS.index[0], FORMS.index[-1])]

    assert signals(15) == [20]
    assert signals(16) == []


def test_baseline_excludes_days_before_the_sixth_trading_day_after_listing() -> None:
    h = replace(history(make_bars([100.0] * 10)), listed_on=make_bars([100.0] * 10).index[2])

    assert forward_returns(h, horizons=(1,)).index[0] == h.bars.index[7]


def test_listing_after_the_last_bar_allows_no_signals() -> None:
    h = replace(history(FORMS), listed_on=date(2030, 1, 1))

    assert find_signals(h, ENGINE, CONFIG.rules.trend, FORMS.index[0], FORMS.index[-1]) == []


# --- 標記 -------------------------------------------------------------------

def test_close_at_limit_up_or_down_is_flagged_by_side() -> None:
    bars = set_bar(set_bar(make_bars([100.0] * 3), 1, close=110.0), 2, close=99.0)       # 110 = 100 的漲停
    rows = [row(1, "long"), row(1, "short"), row(2, "short")]                             # 99 = 110 的跌停

    add_flags(rows, history(bars), window=120, gap_flag_pct=3.0)

    assert [r["limit_locked"] for r in rows] == [True, False, True]


def test_limit_on_ex_dividend_day_uses_reference_price() -> None:
    # 除息 10 元：參考價 90，漲停 99
    bars = set_bar(make_bars([100.0, 100.0]), 1, close=99.0)

    rows = [row(1)]
    add_flags(rows, history(bars, [dividend(bars, 1, 10.0)]), window=120, gap_flag_pct=3.0)

    assert rows[0]["limit_locked"]


def test_gap_event_within_the_window_is_flagged() -> None:
    bars = make_bars([100.0] * 6)
    h = history(bars, [dividend(bars, 2, 10.0)])            # 因子 0.9，調整幅度 10%
    rows = [row(1), row(2), row(4), row(5)]

    add_flags(rows, h, window=3, gap_flag_pct=3.0)

    assert [r["gap_event"] for r in rows] == [False, True, True, False]     # 第 5 根的區間為第 3～5 根


def test_small_dividend_is_not_a_gap_event() -> None:
    bars = make_bars([100.0] * 3)
    rows = [row(2)]

    add_flags(rows, history(bars, [dividend(bars, 1, 2.0)]), window=120, gap_flag_pct=3.0)

    assert not rows[0]["gap_event"]


# --- 報酬 -------------------------------------------------------------------

def test_returns_and_excursions_for_long_and_short() -> None:
    bars = make_bars([100.0, 110.0, 99.0, 120.0])
    bars = set_bar(set_bar(bars, 1, high=112.0, low=105.0), 2, high=101.0, low=95.0)
    rows = [row(0, "long"), row(0, "short")]

    add_returns(rows, history(bars), horizons=(1, 2, 3, 4))

    long, short = rows
    assert [long[f"ret_{n}"] for n in (1, 2, 3)] == pytest.approx([10.0, -1.0, 20.0])
    assert (long["mfe_2"], long["mae_2"]) == pytest.approx((12.0, -5.0))
    assert [short[f"ret_{n}"] for n in (1, 2, 3)] == pytest.approx([-10.0, 1.0, -20.0])
    assert (short["mfe_2"], short["mae_2"]) == pytest.approx((5.0, -12.0))
    assert np.isnan(long["ret_4"]) and np.isnan(long["mfe_4"])          # 之後不足 4 根
    assert long["entry_price"] == 100.0


def test_returns_add_back_dividends() -> None:
    # 收盤 100 → 除息 10 元後 90：還原後沒有漲跌
    bars = make_bars([100.0, 100.0, 90.0])
    rows = [row(0)]

    add_returns(rows, history(bars, [dividend(bars, 2, 10.0)]), horizons=(2,))

    assert rows[0]["ret_2"] == pytest.approx(0.0)


def test_forward_returns_for_every_day() -> None:
    returns = forward_returns(history(make_bars([100.0, 110.0, 121.0])), horizons=(1, 2))

    assert returns[1].tolist()[:2] == pytest.approx([10.0, 10.0])
    assert returns[2].iloc[0] == pytest.approx(21.0)
    assert np.isnan(returns[2].iloc[1])


# --- 去重 -------------------------------------------------------------------

def test_dedupe_counts_from_the_last_kept_signal() -> None:
    signals = pd.DataFrame({
        "code": ["2330"] * 5 + ["2330", "2454"],
        "rule_id": ["e2"] * 5 + ["e3", "e2"],
        "position": [0, 2, 5, 6, 11, 2, 2],
    })

    kept = dedupe(signals, days=5)

    # 2 距 0 不到 5 天不計；5 距上次計入的 0 剛好 5 天計入；不同規則、不同股票各自計算
    assert kept.tolist() == [True, False, True, False, True, True, True]


def test_dedupe_of_zero_days_keeps_everything() -> None:
    signals = pd.DataFrame({"code": ["2330"] * 2, "rule_id": ["e2"] * 2, "position": [3, 3]})

    assert dedupe(signals, days=0).tolist() == [True, True]


def test_find_signals_reports_trend_and_identity() -> None:
    (signal,) = find_signals(history(FORMS), ENGINE, CONFIG.rules.trend, FORMS.index[0], FORMS.index[-1])

    assert (signal["code"], signal["name"], signal["group"], signal["rule_id"]) == (
        "2330", "台積電", "半導體", "ma_bull_alignment")
    assert signal["trend"] in {"多頭", "盤整", "空頭", "資料不足"}
    assert isinstance(signal["date"], date)
