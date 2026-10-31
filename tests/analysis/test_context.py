"""StockContext 的建立：均線、計算區間、今日的處理（PROJECT_PLAN.md 4.1、4.2、第 6 節）。

使用 tests/fixtures/bull_zigzag.csv，預期結果的推導寫在該檔開頭。
"""

from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from conftest import load_fixture, make_bars, to_bars
from screener.analysis.context import BAR_COLUMNS, bars_to_frame, build_context
from screener.analysis.indicators import sma
from screener.config import TrendConfig
from screener.models import DailyBar, Market, Pivot, StockInfo, Trend

CONFIG = TrendConfig(pivot_ma=5, lookback_days=120, bull_break_to_range=True, bear_break_to_range=True)
INFO = StockInfo(code="2330", name="台積電", market=Market.TSE, group="半導體", subcategory="製造")

EXPECTED_PIVOTS = [
    Pivot(date(2026, 1, 12), 110.0, "peak"),
    Pivot(date(2026, 1, 15), 98.0, "trough"),
    Pivot(date(2026, 1, 21), 116.0, "peak"),
    Pivot(date(2026, 1, 26), 104.0, "trough"),
    Pivot(date(2026, 1, 30), 123.0, "peak"),
]


@pytest.fixture
def zigzag() -> pd.DataFrame:
    return load_fixture("bull_zigzag")


def with_today_close(frame: pd.DataFrame, close: float) -> pd.DataFrame:
    bars = to_bars(frame)
    bars.loc[bars.index[-1], ["open", "high", "low", "close"]] = close
    return bars


def test_fixture_ma5_matches_hand_calculation(zigzag: pd.DataFrame) -> None:
    pd.testing.assert_series_equal(sma(zigzag["close"].astype(float), 5), zigzag["ma5"], check_names=False)


def test_pivots_and_trend_from_fixture(zigzag: pd.DataFrame) -> None:
    ctx = build_context(INFO, to_bars(zigzag), CONFIG, is_tentative=False)

    assert ctx.pivots == EXPECTED_PIVOTS
    assert ctx.trend is Trend.BULL
    assert ctx.is_tentative is False


def test_todays_touch_confirms_pivot(zigzag: pd.DataFrame) -> None:
    # 今日 125 站上 MA5（117）→ 確認底：02-02～今日最低收盤 02-04 109（D22）
    ctx = build_context(INFO, with_today_close(zigzag, 125), CONFIG, is_tentative=True)

    assert ctx.pivots == [*EXPECTED_PIVOTS, Pivot(date(2026, 2, 4), 109.0, "trough")]
    assert ctx.trend is Trend.BULL


def test_pivot_confirmed_today_can_change_trend(zigzag: pd.DataFrame) -> None:
    # 01-28 確認底 104 之後：01-29 收 112（MA5 108.6，上方）、01-30「今日」收 108（MA5 109.2）跌破
    # → 今日確認頭：01-27～01-30 最高收盤 01-28 114。頭 116 → 114 變低、底 98 → 104 變高 → 盤整
    closes = [*zigzag["close"].loc[:date(2026, 1, 28)], 112, 108]
    ctx = build_context(INFO, make_bars(closes), CONFIG, is_tentative=True)

    assert ctx.pivots[-1] == Pivot(date(2026, 1, 28), 114.0, "peak")
    assert ctx.trend is Trend.RANGE


@pytest.mark.parametrize(("today_close", "expected"), [(103.0, Trend.RANGE), (104.0, Trend.BULL)])
def test_break_correction_includes_todays_close(zigzag: pd.DataFrame, today_close: float, expected: Trend) -> None:
    # 前底 104：今日收盤跌破才改判盤整，等於前底不算
    ctx = build_context(INFO, with_today_close(zigzag, today_close), CONFIG, is_tentative=True)

    assert ctx.trend is expected


def test_lookback_limits_window_but_ma_uses_full_history(zigzag: pd.DataFrame) -> None:
    # 計算區間為截至今日的 11 根（01-22～02-05）。01-22 的 MA5 來自區間外的歷史；
    # 收盤 110 = MA5，不是「上方」→ 初始狀態「下方」
    ctx = build_context(INFO, to_bars(zigzag), replace(CONFIG, lookback_days=11), is_tentative=False)

    assert ctx.pivots == [Pivot(date(2026, 1, 26), 104.0, "trough"), Pivot(date(2026, 1, 30), 123.0, "peak")]
    assert ctx.trend is Trend.UNKNOWN


def test_ma_includes_today_and_is_cached(zigzag: pd.DataFrame) -> None:
    ctx = build_context(INFO, to_bars(zigzag), CONFIG, is_tentative=False)

    assert ctx.ma(5).iloc[-1] == 114.8
    assert ctx.ma(5) is ctx.ma(5)


def test_single_bar_gives_unknown() -> None:
    ctx = build_context(INFO, make_bars([100]), CONFIG, is_tentative=False)

    assert (ctx.pivots, ctx.trend) == ([], Trend.UNKNOWN)


def test_rejects_empty_or_unsorted_bars() -> None:
    with pytest.raises(ValueError, match="沒有任何 K 棒"):
        build_context(INFO, make_bars([]), CONFIG, is_tentative=False)

    with pytest.raises(ValueError, match="由舊到新"):
        build_context(INFO, make_bars([1, 2, 3]).iloc[::-1], CONFIG, is_tentative=False)


def test_bars_to_frame() -> None:
    bars = [DailyBar("2330", date(2026, 10, 29), 2420.0, 2425.0, 2390.0, 2415.0, 18404),
            DailyBar("2330", date(2026, 10, 30), 2380.0, 2380.0, 2360.0, 2370.0, 36616)]

    frame = bars_to_frame(bars)

    assert tuple(frame.columns) == BAR_COLUMNS
    assert list(frame.index) == [date(2026, 10, 29), date(2026, 10, 30)]
    assert frame.loc[date(2026, 10, 30)].tolist() == [2380.0, 2380.0, 2360.0, 2370.0, 36616]
