"""趨勢分類與破位修正（PROJECT_PLAN.md 4.2）。直接給定轉折點與收盤價。"""

from dataclasses import replace
from datetime import date, timedelta

import pandas as pd
import pytest

from screener.analysis.trend import basic_trend, classify_trend
from screener.config import TrendConfig
from screener.models import Pivot, Trend

CONFIG = TrendConfig(pivot_ma=5, lookback_days=120, bull_break_to_range=True, bear_break_to_range=True)
DAY0 = date(2026, 1, 5)


def pivots(*spec: tuple[str, float]) -> list[Pivot]:
    """依序建立轉折點，第 i 個在 DAY0 + 10i 天。spec 為 ("P" 頭 / "T" 底, 收盤價)。"""
    kinds = {"P": "peak", "T": "trough"}
    return [Pivot(date=DAY0 + timedelta(days=10 * i), close=c, kind=kinds[k]) for i, (k, c) in enumerate(spec)]


def closes_after(pivot_list: list[Pivot], *values: float) -> pd.Series:
    """最後一個轉折點之後逐日的收盤價（最後一個值為今日），前面補上轉折點當天的收盤價。"""
    last = pivot_list[-1]
    dates = [last.date] + [last.date + timedelta(days=i + 1) for i in range(len(values))]
    return pd.Series([last.close, *values], index=pd.Index(dates, name="date"))


# --- 步驟一：基本趨勢 -------------------------------------------------------

@pytest.mark.parametrize(
    "spec",
    [(), (("P", 10),), (("P", 10), ("T", 5), ("P", 12))],
    ids=["none", "one-peak", "one-trough"],
)
def test_fewer_than_two_peaks_or_troughs_is_unknown(spec: tuple) -> None:
    assert basic_trend(pivots(*spec)) is Trend.UNKNOWN


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ((("P", 10), ("T", 5), ("P", 12), ("T", 7)), Trend.BULL),
        ((("T", 5), ("P", 10), ("T", 7), ("P", 12)), Trend.BULL),
        ((("P", 12), ("T", 7), ("P", 10), ("T", 5)), Trend.BEAR),
        ((("P", 10), ("T", 7), ("P", 12), ("T", 5)), Trend.RANGE),     # 頭頭高、底底低
        ((("P", 12), ("T", 5), ("P", 10), ("T", 7)), Trend.RANGE),     # 頭頭低、底底高
        ((("P", 10), ("T", 5), ("P", 10), ("T", 7)), Trend.RANGE),     # 兩個頭相等
        ((("P", 10), ("T", 5), ("P", 12), ("T", 5)), Trend.RANGE),     # 兩個底相等
    ],
)
def test_basic_trend_compares_last_two_peaks_and_troughs(spec: tuple, expected: Trend) -> None:
    assert basic_trend(pivots(*spec)) is expected


def test_only_the_most_recent_two_pivots_of_each_kind_count() -> None:
    assert basic_trend(pivots(("P", 20), ("T", 5), ("P", 10), ("T", 6), ("P", 12), ("T", 7))) is Trend.BULL


# --- 步驟二：破位修正 -------------------------------------------------------

BULL = pivots(("T", 5), ("P", 10), ("T", 7), ("P", 12))      # 前底 7
BEAR = pivots(("P", 12), ("T", 7), ("P", 10), ("T", 5))      # 前頭 10


def test_bull_breaking_last_trough_becomes_range() -> None:
    assert classify_trend(BULL, closes_after(BULL, 9, 6.9, 8), CONFIG) is Trend.RANGE


def test_bull_break_by_todays_close_becomes_range() -> None:
    assert classify_trend(BULL, closes_after(BULL, 9, 8, 6.5), CONFIG) is Trend.RANGE


def test_bull_touching_last_trough_stays_bull() -> None:
    assert classify_trend(BULL, closes_after(BULL, 9, 7, 8), CONFIG) is Trend.BULL


def test_bull_break_correction_can_be_disabled() -> None:
    config = replace(CONFIG, bull_break_to_range=False)

    assert classify_trend(BULL, closes_after(BULL, 6), config) is Trend.BULL


def test_bull_break_compares_with_last_trough_when_last_pivot_is_trough() -> None:
    bull = pivots(("P", 10), ("T", 5), ("P", 12), ("T", 7))

    assert classify_trend(bull, closes_after(bull, 9, 6.9), CONFIG) is Trend.RANGE
    assert classify_trend(bull, closes_after(bull, 9, 7.1), CONFIG) is Trend.BULL


def test_pivot_on_today_leaves_nothing_to_check() -> None:
    # 最近一個轉折點就在今日：之後沒有收盤價，維持基本趨勢
    assert classify_trend(BULL, closes_after(BULL), CONFIG) is Trend.BULL


def test_closes_before_last_pivot_are_not_checked() -> None:
    closes = pd.concat([pd.Series([6.0], index=[BULL[-1].date - timedelta(days=1)]), closes_after(BULL, 9)])

    assert classify_trend(BULL, closes, CONFIG) is Trend.BULL


def test_bear_breaking_last_peak_becomes_range() -> None:
    assert classify_trend(BEAR, closes_after(BEAR, 8, 10.5), CONFIG) is Trend.RANGE
    assert classify_trend(BEAR, closes_after(BEAR, 8, 10), CONFIG) is Trend.BEAR


def test_bear_break_correction_can_be_disabled() -> None:
    config = replace(CONFIG, bear_break_to_range=False)

    assert classify_trend(BEAR, closes_after(BEAR, 11), config) is Trend.BEAR


@pytest.mark.parametrize(
    "spec",
    [(("P", 10), ("T", 7), ("P", 12), ("T", 5)), (("P", 10), ("T", 5), ("P", 12))],
    ids=["range", "unknown"],
)
def test_range_and_unknown_are_not_corrected(spec: tuple) -> None:
    pivot_list = pivots(*spec)
    expected = basic_trend(pivot_list)

    assert classify_trend(pivot_list, closes_after(pivot_list, 1, 100), CONFIG) is expected
