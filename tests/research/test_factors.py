"""因子的計算（RESEARCH_PLAN.md 4.4、4.5）：以手工的小面板驗證定義，以及只用當天（含）以前的資料。"""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import REPO_CONFIG_DIR
from screener.analysis.indicators import sma
from screener.analysis.pivots import find_pivots
from screener.analysis.trend import classify_trend
from screener.backtest.signals import StockHistory
from screener.config import ConfigError, FactorDefinition, TrendConfig, load_research_config
from screener.jobs import JobFailed
from screener.models import Market, StockInfo
from screener.research.factors import FactorContext, build_factors
from screener.research.factors import fundamental as fund
from screener.research.factors.panel import Panel, build_panel, per_stock
from screener.research.factors.trend import TREND_CODES

NAN = float("nan")
MARKET = load_research_config(REPO_CONFIG_DIR / "research.yaml").market
TREND = TrendConfig(pivot_ma=5, lookback_days=120, bull_break_to_range=True, bear_break_to_range=True)


def days(n: int, start: date = date(2026, 1, 1)) -> pd.Index:
    return pd.Index([start + timedelta(days=i) for i in range(n)], name="date")


def make_panel(closes: dict[str, list[float]], *, index: pd.Index | None = None, opens: list[float] | None = None,
               highs: list[float] | None = None, lows: list[float] | None = None, volumes: list[float] | None = None,
               values: list[float] | None = None, raw: list[float] | None = None) -> Panel:
    index = days(len(next(iter(closes.values())))) if index is None else index
    close = pd.DataFrame(closes, index=index, dtype=float)

    def frame(column: list[float] | None) -> pd.DataFrame:
        if column is None:
            return close.copy()
        return pd.DataFrame({code: column for code in close.columns}, index=index, dtype=float)

    flags = pd.DataFrame(False, index=index, columns=close.columns)
    return Panel(open=frame(opens), high=frame(highs), low=frame(lows), close=close, raw_close=frame(raw), raw_open=frame(raw), reference=frame(raw),
                 volume=frame(volumes), value=frame(values), eligible=close.notna(), limit_up=flags, limit_down=flags)


def context(panel: Panel, *, revenue: pd.DataFrame | None = None, valuations: pd.DataFrame | None = None,
            shares: pd.DataFrame | None = None) -> FactorContext:
    return FactorContext(panel=panel, trend=TREND, market=MARKET,
                         revenue=pd.DataFrame() if revenue is None else revenue,
                         valuations=pd.DataFrame() if valuations is None else valuations,
                         shares=pd.DataFrame() if shares is None else shares)


def compute(kind: str, ctx: FactorContext, **params) -> pd.DataFrame:
    factor = build_factors([FactorDefinition("x", kind, params)])[0]
    return factor.compute(ctx)


# --- 註冊表 ---------------------------------------------------------------------

def test_repo_definitions_all_build() -> None:
    factors = build_factors(load_research_config(REPO_CONFIG_DIR / "research.yaml").factors.definitions)

    assert len(factors) == 26
    assert {f.id for f in factors if f.is_categorical} == {"trend", "profitable"}


def test_unknown_kind_and_bad_params_are_rejected() -> None:
    with pytest.raises(ConfigError, match="未知的 kind `nope`"):
        build_factors([FactorDefinition("a", "nope", {})])
    with pytest.raises(ConfigError, match="參數應為"):
        build_factors([FactorDefinition("a", "momentum", {"days": 5})])


# --- 面板 -----------------------------------------------------------------------

def history(code: str, index: pd.Index, closes: list[float], *, in_universe: list[bool] | None = None,
            factors: list[float] | None = None) -> StockHistory:
    bars = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes,
                         "volume": [1000] * len(closes)}, index=index, dtype=float)
    return StockHistory(info=StockInfo(code=code, name=code, market=Market.TSE, group="上市", subcategory=None),
                        bars=bars, adjusted=bars / 2, factors=pd.Series(factors or [1.0] * len(closes), index=index),
                        in_universe=pd.Series(in_universe or [True] * len(closes), index=index))


def test_build_panel_aligns_stocks_and_flags_limits() -> None:
    all_days = days(4)
    # A 第 1 天收 11（昨收 10 的漲停價）；B 第 1 天沒有成交；C 第 1 天跌停 9
    a = history("A", all_days, [10, 11, 11, 11])
    b = history("B", all_days[[0, 2, 3]], [10, 10, 10], in_universe=[True, False, True])
    c = history("C", all_days, [10, 9, 9, 9])
    values = pd.DataFrame({"A": [1e8] * 4, "B": [1e8, np.nan, 2e8, 0.0], "C": [np.nan, 5e7, 5e7, 5e7]}, index=all_days)

    panel = build_panel([a, b, c], values)

    assert list(panel.codes) == ["A", "B", "C"]
    assert panel.close.loc[all_days[1], "A"] == 5.5            # 還原價（測試用 ÷ 2）
    assert np.isnan(panel.close.loc[all_days[1], "B"])
    assert panel.raw_close.loc[all_days[1], "A"] == 11
    assert list(panel.eligible["B"]) == [True, False, False, True]
    assert panel.limit_up.loc[all_days[1], "A"] and not panel.limit_up.loc[all_days[2], "A"]
    assert panel.limit_down.loc[all_days[1], "C"]
    assert panel.value.loc[all_days[1], "B"] == 0              # 出現過後，沒有成交算 0
    assert np.isnan(panel.value.loc[all_days[0], "C"])         # 第一次出現之前


def test_per_stock_counts_only_traded_bars() -> None:
    frame = pd.DataFrame({"A": [1.0, NAN, 2.0, 3.0]}, index=days(4))

    result = per_stock(frame, lambda s: s.shift(1))

    assert result["A"].iloc[2] == 1.0                           # 上一根 K 棒是第 0 天，不是沒有成交的第 1 天


# --- 價量因子 --------------------------------------------------------------------

def test_momentum_skips_the_most_recent_period() -> None:
    panel = make_panel({"A": [1, 2, 4, 8, 16, 32]})

    result = compute("momentum", context(panel), lookback=4, skip=1)

    assert result["A"].iloc[4] == 8 / 1 - 1                     # t−4 → t−1
    assert np.isnan(result["A"].iloc[3])


def test_past_return_and_volatility_and_max_return() -> None:
    panel = make_panel({"A": [10, 11, 12.1, 9.68]})
    ctx = context(panel)

    assert compute("past_return", ctx, days=2)["A"].iloc[3] == pytest.approx(9.68 / 11 - 1)
    returns = [0.1, 0.1, -0.2]
    assert compute("volatility", ctx, days=3)["A"].iloc[3] == pytest.approx(np.std(returns, ddof=1))
    assert compute("max_daily_return", ctx, days=2)["A"].iloc[3] == pytest.approx(0.1)


def test_average_value_counts_untraded_days_as_zero() -> None:
    panel = make_panel({"A": [1, 1, 1, 1]}, values=[100, 0, 300, 200])

    result = compute("average_value", context(panel), days=3)

    assert result["A"].iloc[2] == pytest.approx(np.log(400 / 3))
    assert result["A"].iloc[3] == pytest.approx(np.log(500 / 3))
    assert np.isnan(result["A"].iloc[1])


def test_value_surge() -> None:
    panel = make_panel({"A": [1] * 5}, values=[10, 10, 10, 30, 30])

    result = compute("value_surge", context(panel), short=2, long=4)

    assert result["A"].iloc[3] == pytest.approx((10 + 30) / 2 / 15)


# --- 現有規則的分數 ----------------------------------------------------------------

def test_ma_gap_and_spread() -> None:
    panel = make_panel({"A": [1, 2, 3, 4, 5, 6]})
    ctx = context(panel)

    assert compute("ma_gap", ctx, ma=3)["A"].iloc[3] == pytest.approx(4 / 3 - 1)
    assert compute("ma_spread", ctx, short=2, long=4)["A"].iloc[5] == pytest.approx(5.5 / 4.5 - 1)


def test_volume_ratio_zero_yesterday_is_missing() -> None:
    panel = make_panel({"A": [1, 1, 1, 1]}, volumes=[10, 0, 5, 20])

    result = compute("volume_ratio", context(panel))

    assert np.isnan(result["A"].iloc[2])                        # 昨日 0 張算不出量比（D39）
    assert result["A"].iloc[3] == 4.0


def test_candle_body_uses_previous_close() -> None:
    panel = make_panel({"A": [10, 11, 12]}, opens=[10, 10.5, 12.5])

    result = compute("candle_body", context(panel))

    assert result["A"].iloc[1] == pytest.approx(0.05)           # (11 − 10.5) / 10
    assert result["A"].iloc[2] == pytest.approx(-0.5 / 11)


def test_high_and_low_gap_exclude_today() -> None:
    panel = make_panel({"A": [5, 6, 7, 8]}, highs=[9, 9, 7, 99], lows=[5, 4, 6, 0.1])

    high = compute("high_gap", context(panel), days=2)
    low = compute("low_gap", context(panel), days=2)

    assert high["A"].iloc[3] == pytest.approx(8 / 9 - 1)        # 前 2 日最高 9，不含今日的 99
    assert low["A"].iloc[3] == pytest.approx(8 / 4 - 1)         # 前 2 日最低 4，不含今日的 0.1


# --- 趨勢 -----------------------------------------------------------------------

def test_trend_matches_the_rule_engine_functions_day_by_day() -> None:
    closes = [10 + 3 * np.sin(i / 3) + i * 0.05 for i in range(60)]
    index = days(60)
    panel = make_panel({"A": closes}, index=index)

    labels = compute("trend", context(panel))["A"]

    series = pd.Series(closes, index=index)
    averages = sma(series, 5)
    for i in (5, 30, 59):
        expected = classify_trend(find_pivots(series.iloc[:i + 1], averages.iloc[:i + 1]), series.iloc[:i + 1], TREND)
        assert labels.iloc[i] == TREND_CODES[expected]


# --- 基本面 -----------------------------------------------------------------------

def test_revenue_available_day_is_the_first_trading_day_after_the_deadline() -> None:
    trading = pd.Index([date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 12), date(2026, 10, 13)])

    assert fund.revenue_available_on(date(2026, 9, 1), trading, MARKET) == date(2026, 10, 12)      # 10/09～10/11 休市
    assert fund.revenue_available_on(date(2026, 12, 1), trading, MARKET) is None                   # 還沒有到


def revenue_rows() -> pd.DataFrame:
    # 8 月營收 9/11 起可用，9 月營收 10/12 起可用；A 的 9 月營收年增 100%，B 的去年營收為 0 算不出來
    months = ["2026-08-01", "2026-09-01"]
    return pd.DataFrame([
        {"code": "A", "month": months[0], "market": "tse", "name": "A", "industry": "半導體業", "revenue": 150.0,
         "previous": 100.0, "last_year": 100.0, "cumulative": 500.0, "cumulative_last_year": 400.0},
        {"code": "A", "month": months[1], "market": "tse", "name": "A", "industry": "半導體業", "revenue": 200.0,
         "previous": 150.0, "last_year": 100.0, "cumulative": 700.0, "cumulative_last_year": 500.0},
        {"code": "B", "month": months[1], "market": "otc", "name": "B", "industry": "航運業", "revenue": 50.0,
         "previous": 0.0, "last_year": 0.0, "cumulative": 50.0, "cumulative_last_year": 0.0},
    ])


REVENUE_DAYS = pd.Index([date(2026, 9, 10), date(2026, 9, 11), date(2026, 10, 8), date(2026, 10, 12)], name="date")


def test_revenue_factors_use_only_published_months() -> None:
    panel = make_panel({"A": [1.0] * 4, "B": [1.0] * 4}, index=REVENUE_DAYS)
    ctx = context(panel, revenue=revenue_rows())

    yoy = compute("revenue_yoy", ctx)

    assert np.isnan(yoy["A"].iloc[0])                           # 9/10：8 月營收還不能用
    assert yoy["A"].iloc[1] == pytest.approx(0.5)               # 9/11：8 月營收
    assert yoy["A"].iloc[2] == pytest.approx(0.5)               # 10/08：9 月營收還不能用，沿用 8 月
    assert yoy["A"].iloc[3] == pytest.approx(1.0)               # 10/12：9 月營收
    assert np.isnan(yoy["B"].iloc[3])
    assert compute("revenue_mom", ctx)["A"].iloc[3] == pytest.approx(200 / 150 - 1)
    assert compute("revenue_cum_yoy", ctx)["A"].iloc[3] == pytest.approx(700 / 500 - 1)


def test_revenue_acceleration_needs_the_earlier_month() -> None:
    rows = revenue_rows()
    earlier = rows.iloc[[0]].assign(month="2026-06-01", revenue=120.0, last_year=100.0)
    panel = make_panel({"A": [1.0] * 4, "B": [1.0] * 4}, index=REVENUE_DAYS)

    result = compute("revenue_acceleration", context(panel, revenue=pd.concat([rows, earlier], ignore_index=True)), months=3)

    assert result["A"].iloc[3] == pytest.approx(1.0 - 0.2)      # 9 月年增 100%，6 月年增 20%
    assert np.isnan(result["A"].iloc[1])                        # 8 月的三個月前（5 月）沒有資料


def test_industry_codes_are_point_in_time() -> None:
    panel = make_panel({"A": [1.0] * 4, "B": [1.0] * 4}, index=REVENUE_DAYS)

    codes, names = fund.industry_codes(revenue_rows(), panel, MARKET)

    assert names == ["半導體業", "航運業"]
    assert list(codes[:, 0]) == [-1, 0, 0, 0]                   # A：8 月的資料 9/11 起可用
    assert list(codes[:, 1]) == [-1, -1, -1, 1]                 # B：只有 9 月的資料，10/12 起可用


def test_financial_industry_names_are_merged() -> None:
    rows = revenue_rows()
    rows.loc[0, "industry"], rows.loc[1, "industry"], rows.loc[2, "industry"] = "金融保險業", "金融業", "金融業"
    panel = make_panel({"A": [1.0] * 4, "B": [1.0] * 4}, index=REVENUE_DAYS)

    codes, names = fund.industry_codes(rows, panel, MARKET)

    assert names == ["金融保險業"]
    assert list(codes[-1]) == [0, 0]


def valuation_rows() -> pd.DataFrame:
    return pd.DataFrame([{"code": "A", "date": "2026-09-01", "market": "tse", "pe": 20.0, "pb": 2.0, "dividend_yield": 4.0},
                         {"code": "B", "date": "2026-09-01", "market": "otc", "pe": None, "pb": 1.0, "dividend_yield": None}])


VALUATION_DAYS = pd.Index([date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2)], name="date")


def test_valuation_updates_with_adjusted_price_after_the_sample_day() -> None:
    panel = make_panel({"A": [8.0, 10.0, 12.5], "B": [5.0, 5.0, 5.0]}, index=VALUATION_DAYS)
    ctx = context(panel, valuations=valuation_rows())

    ep = compute("earnings_yield", ctx)
    assert np.isnan(ep["A"].iloc[0])                            # 取樣日之前沒有資料
    assert ep["A"].iloc[1] == pytest.approx(1 / 20)
    assert ep["A"].iloc[2] == pytest.approx(1 / 20 * 10 / 12.5)  # E/P 隨股價更新
    assert np.isnan(ep["B"].iloc[2])                            # 虧損沒有 E/P
    assert compute("book_yield", ctx)["A"].iloc[2] == pytest.approx(1 / 2 * 10 / 12.5)
    assert compute("dividend_yield", ctx)["A"].iloc[2] == pytest.approx(4 * 10 / 12.5)


def test_profitable_is_categorical_from_the_sample_day() -> None:
    panel = make_panel({"A": [8.0, 10.0, 12.5], "B": [5.0, 5.0, 5.0]}, index=VALUATION_DAYS)

    result = compute("profitable", context(panel, valuations=valuation_rows()))

    assert np.isnan(result["A"].iloc[0])
    assert (result.loc[VALUATION_DAYS[1], "A"], result.loc[VALUATION_DAYS[2], "A"]) == (1.0, 1.0)
    assert result.loc[VALUATION_DAYS[1], "B"] == 2.0            # 本益比沒有值 = 虧損


def test_size_uses_shares_at_the_sample_and_adjusted_price_after() -> None:
    index = pd.Index([date(2026, 9, 1), date(2026, 9, 2)], name="date")
    # 取樣日未還原收盤 100、還原收盤 50；隔天還原收盤 60 → 市值 1,000 股 × 100 × 60 / 50
    panel = make_panel({"A": [50.0, 60.0]}, index=index, raw=[100.0, 120.0])
    shares = pd.DataFrame([{"code": "A", "date": "2026-09-01", "market": "tse", "shares": 1000}])

    result = compute("size", context(panel, shares=shares))

    assert result["A"].iloc[0] == pytest.approx(np.log(100_000.0))
    assert result["A"].iloc[1] == pytest.approx(np.log(120_000.0))


def test_sample_without_a_trade_keeps_the_previous_sample() -> None:
    # 取樣日沒有成交，沿用上一次的樣本與它的價格
    index = pd.Index([date(2026, 8, 3), date(2026, 9, 1), date(2026, 9, 2)], name="date")
    panel = make_panel({"A": [10.0, NAN, 20.0]}, index=index)
    values = pd.DataFrame([{"code": "A", "date": "2026-08-03", "market": "tse", "pe": 10.0, "pb": 1.0, "dividend_yield": 1.0},
                           {"code": "A", "date": "2026-09-01", "market": "tse", "pe": 99.0, "pb": 1.0, "dividend_yield": 1.0}])

    ep = compute("earnings_yield", context(panel, valuations=values))

    assert ep["A"].iloc[2] == pytest.approx(1 / 10 * 10 / 20)


SAMPLE_DAYS = pd.Index([date(2026, 8, 3), date(2026, 8, 4), date(2026, 9, 1), date(2026, 9, 2), date(2026, 10, 1)],
                       name="date")


def test_sample_without_a_positive_value_ends_the_previous_sample() -> None:
    # 9/01 的取樣明確沒有配息、虧損（R44）：之後沒有值，不沿用 8/03 的樣本；10/01 有新的正數樣本才恢復
    panel = make_panel({"A": [10.0, 11.0, 12.0, 15.0, 20.0]}, index=SAMPLE_DAYS)
    values = pd.DataFrame([{"code": "A", "date": "2026-08-03", "market": "tse", "pe": 10.0, "pb": 1.0, "dividend_yield": 5.0},
                           {"code": "A", "date": "2026-09-01", "market": "tse", "pe": None, "pb": 1.0, "dividend_yield": 0.0},
                           {"code": "A", "date": "2026-10-01", "market": "tse", "pe": 8.0, "pb": 1.0, "dividend_yield": 2.0}])
    ctx = context(panel, valuations=values)

    dy = compute("dividend_yield", ctx)["A"]
    ep = compute("earnings_yield", ctx)["A"]

    assert dy.iloc[1] == pytest.approx(5.0 * 10 / 11)
    assert np.isnan(dy.iloc[2]) and np.isnan(dy.iloc[3])
    assert dy.iloc[4] == pytest.approx(2.0)
    assert ep.iloc[1] == pytest.approx(1 / 10 * 10 / 11)
    assert np.isnan(ep.iloc[2]) and np.isnan(ep.iloc[3])
    assert ep.iloc[4] == pytest.approx(1 / 8)
    assert compute("book_yield", ctx)["A"].iloc[3] == pytest.approx(1 / 1 * 12 / 15)     # 淨值比有值，照常更新


def test_stock_missing_from_a_sample_keeps_the_previous_sample() -> None:
    # 9/01 的取樣裡沒有 A（沒有資訊）：沿用 8/03 的樣本與它的價格
    panel = make_panel({"A": [10.0, 11.0, 12.0, 15.0, 20.0], "B": [5.0] * 5}, index=SAMPLE_DAYS)
    values = pd.DataFrame([{"code": "A", "date": "2026-08-03", "market": "tse", "pe": 10.0, "pb": 1.0, "dividend_yield": 5.0},
                           {"code": "B", "date": "2026-09-01", "market": "tse", "pe": 9.0, "pb": 1.0, "dividend_yield": 3.0}])

    dy = compute("dividend_yield", context(panel, valuations=values))

    assert dy["A"].iloc[3] == pytest.approx(5.0 * 10 / 15)
    assert np.isnan(dy["B"].iloc[1]) and dy["B"].iloc[3] == pytest.approx(3.0)


def test_unusable_zero_sample_keeps_the_previous_sample() -> None:
    # 取樣日沒有成交時，即使樣本是 0 也不用（同「取樣日沒有成交」），沿用上一次的正數樣本
    panel = make_panel({"A": [10.0, 11.0, NAN, 15.0, 20.0]}, index=SAMPLE_DAYS)
    values = pd.DataFrame([{"code": "A", "date": "2026-08-03", "market": "tse", "pe": 10.0, "pb": 1.0, "dividend_yield": 5.0},
                           {"code": "A", "date": "2026-09-01", "market": "tse", "pe": None, "pb": 1.0, "dividend_yield": 0.0}])

    dy = compute("dividend_yield", context(panel, valuations=values))

    assert dy["A"].iloc[3] == pytest.approx(5.0 * 10 / 15)


def test_share_turnover_uses_latest_shares() -> None:
    index = pd.Index([date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)], name="date")
    panel = make_panel({"A": [1.0, 1.0, 1.0]}, index=index, volumes=[1.0, 3.0, 2.0])
    shares = pd.DataFrame([{"code": "A", "date": "2026-09-01", "market": "tse", "shares": 10_000}])

    result = compute("share_turnover", context(panel, shares=shares), days=2)

    assert result["A"].iloc[2] == pytest.approx((3000 / 10000 + 2000 / 10000) / 2)      # 張 → 股
    assert np.isnan(result["A"].iloc[0])


@pytest.mark.parametrize("kind, params", [("revenue_yoy", {}), ("earnings_yield", {}), ("size", {}),
                                          ("share_turnover", {"days": 5})])
def test_missing_fundamental_data_fails_with_a_hint(kind: str, params: dict, tmp_path: Path) -> None:
    panel = make_panel({"A": [1.0, 1.0]})

    with pytest.raises(JobFailed, match="market-update"):
        compute(kind, context(panel), **params)
