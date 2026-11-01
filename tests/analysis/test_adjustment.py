"""還原權息（PROJECT_PLAN.md 14.2）。

案例移植自 Taiwan-Quant-Research 專案的 tests/test_adjustment.py：能對到官方參考價的一律用真實數字，
不拿自己算出來的答案當標準答案（那只是把同一個公式抄兩遍）。
"""

from datetime import date

import pandas as pd
import pytest

from screener.analysis.adjustment import adjust_bars, cumulative_factors, event_factors
from screener.models import ActionKind, CorporateAction, DividendEvent, DividendResult


def bars(*rows: tuple[str, float]) -> pd.DataFrame:
    """(日期, 收盤) 的 K 棒；開高低都等於收盤，測試只看收盤。"""
    days = [date.fromisoformat(d) for d, _ in rows]
    closes = [c for _, c in rows]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000},
                        index=pd.Index(days, name="date"))


def dividend(day: str, cash: float = 0.0, stock: float = 0.0, code: str = "2330") -> DividendEvent:
    return DividendEvent(code, date.fromisoformat(day), cash, stock)


def action(day: str, before: float, after: float, kind: ActionKind = ActionKind.SPLIT) -> CorporateAction:
    return CorporateAction("2330", date.fromisoformat(day), kind, before, after)


def adjusted_close(prices: pd.DataFrame, dividends=(), actions=(), results=()) -> dict[str, float]:
    adjusted = adjust_bars(prices, dividends, actions, results)
    return {day.isoformat(): close for day, close in adjusted["close"].items()}


# --- 對得到官方參考價的真實案例 ----------------------------------------------

def test_cash_dividend_matches_official_reference_price() -> None:
    """2409 友達 2023-08-10 除息 0.80（全部配在公積）：前一日還原為 20.40 − 0.80 = 19.60，與官方參考價相同。"""
    prices = bars(("2023-08-08", 20.40), ("2023-08-09", 20.40), ("2023-08-10", 18.95), ("2023-08-11", 18.50))

    adj = adjusted_close(prices, [dividend("2023-08-10", cash=0.80)])

    assert adj == pytest.approx({"2023-08-08": 19.60, "2023-08-09": 19.60, "2023-08-10": 18.95, "2023-08-11": 18.50})


def test_large_stock_dividend_matches_official_reference_price() -> None:
    """6669 緯穎 2026-09-02 除權，每 10 股配 19.827946 股，7800 → 2610 不是暴跌。

    前一日還原為 7800 / (1 + 1.9827946) = 2615.00，官方參考價 2614.99。
    """
    adj = adjusted_close(bars(("2026-09-01", 7800.0), ("2026-09-02", 2610.0)),
                         [dividend("2026-09-02", stock=19.827946)])

    assert adj["2026-09-01"] == pytest.approx(2614.99, abs=0.05)
    assert adj["2026-09-02"] == pytest.approx(2610.0)


def test_event_on_a_market_closure_day_is_applied_to_the_next_trading_day() -> None:
    """2884 玉山金原定 2024-07-25 除權息，但 07-24、07-25 凱米颱風休市，跳空發生在 07-26。

    前一日還原為 (29.80 − 1.2) / (1 + 0.0200332) = 28.04，官方參考價 28.03。
    """
    adj = adjusted_close(bars(("2024-07-23", 29.80), ("2024-07-26", 27.00)),
                         [dividend("2024-07-25", cash=1.2, stock=0.200332)])

    assert adj["2024-07-23"] == pytest.approx(28.03, abs=0.01)
    assert adj["2024-07-26"] == pytest.approx(27.00)


# --- 官方公告的前後價格優先（D37）--------------------------------------------

def result(day: str, before: float, after: float) -> DividendResult:
    return DividendResult("2330", date.fromisoformat(day), before, after)


def test_rights_issue_without_dividend_uses_official_prices() -> None:
    """3321 同泰 2025-12-05 現金增資除權：股利資料沒有這筆，官方公告 23.0 → 18.97。"""
    adj = adjusted_close(bars(("2025-12-04", 23.0), ("2025-12-05", 19.0)), results=[result("2025-12-05", 23.0, 18.97)])

    assert adj["2025-12-04"] == pytest.approx(18.97)


def test_official_prices_replace_the_formula_on_the_same_day() -> None:
    # 只用股利金額會得到 (100 − 2) / 100；官方另含現金增資，公告 100 → 95
    adj = adjusted_close(bars(("2024-03-11", 100.0), ("2024-03-12", 95.0)), [dividend("2024-03-12", cash=2.0)],
                         results=[result("2024-03-12", 100.0, 95.0)])

    assert adj["2024-03-11"] == pytest.approx(95.0)


def test_official_result_on_a_closure_day_is_applied_to_the_next_trading_day() -> None:
    adj = adjusted_close(bars(("2024-07-23", 29.80), ("2024-07-26", 27.00)), results=[result("2024-07-25", 29.80, 28.03)])

    assert adj["2024-07-23"] == pytest.approx(28.03)


# --- 減資與面額變更 ----------------------------------------------------------

def test_capital_reduction_raises_history() -> None:
    """2609 陽明 2017-05-04 減資，官方公告 6.15 → 13.16。不還原會看成一天暴漲 116%。"""
    adj = adjusted_close(bars(("2017-05-03", 6.15), ("2017-05-04", 13.15)),
                         actions=[action("2017-05-04", 6.15, 13.16, ActionKind.CAPITAL_REDUCTION)])

    assert adj["2017-05-03"] == pytest.approx(13.16, abs=0.01)
    assert adj["2017-05-04"] == pytest.approx(13.15)


def test_par_value_change_lowers_history() -> None:
    """2327 國巨 2025-08-25 面額變更，官方公告 546 → 136.5（四分之一）。"""
    adj = adjusted_close(bars(("2025-08-22", 546.0), ("2025-08-25", 140.0)),
                         actions=[action("2025-08-25", 546.0, 136.5)])

    assert adj["2025-08-22"] == pytest.approx(136.5)


def test_dividend_and_action_on_the_same_day_are_multiplied() -> None:
    # 除息因子 (100 − 10) / 100 = 0.9，減資因子 200 / 100 = 2.0，相乘 1.8
    adj = adjusted_close(bars(("2024-03-11", 100.0), ("2024-03-12", 180.0)),
                         [dividend("2024-03-12", cash=10.0)], [action("2024-03-12", 100.0, 200.0)])

    assert adj["2024-03-11"] == pytest.approx(180.0)


def test_action_on_a_closure_day_is_applied_to_the_next_trading_day() -> None:
    adj = adjusted_close(bars(("2024-07-23", 100.0), ("2024-07-26", 200.0)),
                         actions=[action("2024-07-25", 100.0, 200.0)])

    assert adj["2024-07-23"] == pytest.approx(200.0)


# --- 同一天多筆事件 ----------------------------------------------------------

def test_same_day_dividends_are_combined_into_one_reference_price() -> None:
    """(200 − 5) / (1 + 1.0) = 97.5。分開套用時第二筆會拿當天收盤當成前一日收盤，算出 95.0。"""
    prices = bars(("2024-03-11", 200.0), ("2024-03-12", 100.0))

    adj = adjusted_close(prices, [dividend("2024-03-12", stock=10.0), dividend("2024-03-12", cash=5.0)])

    assert adj == pytest.approx({"2024-03-11": 97.5, "2024-03-12": 100.0})


# --- 定義與邊界 --------------------------------------------------------------

def test_event_day_itself_is_not_discounted() -> None:
    adj = adjusted_close(bars(("2026-09-01", 7800.0), ("2026-09-02", 2610.0)),
                         [dividend("2026-09-02", stock=19.827946)])

    assert adj["2026-09-02"] == pytest.approx(2610.0)


def test_multiple_events_are_compounded() -> None:
    """收盤固定 100，01-06 與 01-08 各除息 10：因子都是 0.9，01-05 在兩次之前 → 81。"""
    prices = bars(("2026-01-05", 100.0), ("2026-01-06", 100.0), ("2026-01-07", 100.0), ("2026-01-08", 100.0))

    adj = adjusted_close(prices, [dividend("2026-01-06", cash=10.0), dividend("2026-01-08", cash=10.0)])

    assert list(adj.values()) == pytest.approx([81.0, 90.0, 90.0, 100.0])


def test_latest_day_is_never_adjusted() -> None:
    """後還原的定義：最後一天恆等於原始價；被改動就代表方向搞反成前還原。"""
    adj = adjusted_close(bars(("2026-01-05", 100.0), ("2026-01-06", 120.0), ("2026-01-07", 150.0)),
                         [dividend("2026-01-06", cash=10.0)])

    assert adj["2026-01-07"] == 150.0


def test_event_after_the_last_bar_changes_nothing() -> None:
    """例：今天除息、收盤資料還沒抓進來。之後沒有任何價格，不影響已觀測到的報酬。"""
    adj = adjusted_close(bars(("2024-07-22", 100.0), ("2024-07-23", 100.0)), [dividend("2024-07-25", cash=10.0)])

    assert list(adj.values()) == [100.0, 100.0]


def test_dividend_on_the_first_bar_changes_nothing() -> None:
    assert event_factors(bars(("2024-07-22", 100.0)), [dividend("2024-07-22", cash=10.0)], []).tolist() == [1.0]


def test_no_events_leaves_prices_untouched() -> None:
    prices = bars(("2026-01-05", 100.0), ("2026-01-06", 120.0))

    pd.testing.assert_frame_equal(adjust_bars(prices, [], []), prices)


def test_adjusted_prices_are_not_rounded() -> None:
    """3.00 × (3.00 / 3.10) = 2.9032…；round(2) 成 2.90 會差 0.11%。"""
    adj = adjusted_close(bars(("2024-03-11", 3.00), ("2024-03-12", 3.10), ("2024-03-13", 3.00)),
                         [dividend("2024-03-13", cash=0.10)])

    assert adj["2024-03-11"] == pytest.approx(3.00 * 3.00 / 3.10, rel=1e-12)


def test_all_prices_are_adjusted_but_volume_is_not() -> None:
    prices = bars(("2024-03-11", 100.0), ("2024-03-12", 90.0))

    adjusted = adjust_bars(prices, [dividend("2024-03-12", cash=10.0)], [])

    assert adjusted.iloc[0][["open", "high", "low", "close"]].tolist() == pytest.approx([90.0] * 4)
    assert adjusted["volume"].tolist() == [1000, 1000]


def test_cumulative_factors_exclude_the_event_day() -> None:
    factors = pd.Series([1.0, 0.9, 1.0, 0.5])

    assert cumulative_factors(factors).tolist() == pytest.approx([0.45, 0.5, 0.5, 1.0])
