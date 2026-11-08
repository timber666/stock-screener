"""由 market.db 讀出的歷史資料：日 K、掛牌日、股票池與還原權息（RESEARCH_PLAN.md 4.3）。"""

from datetime import date

import pandas as pd
import pytest

from screener.models import ActionKind, CorporateAction, DividendResult, Market
from screener.research.market.history import build_history
from screener.research.market.models import Security
from screener.research.universe import Universe

DAYS = ["2026-01-29", "2026-01-30", "2026-02-02", "2026-02-03", "2026-02-04"]


CALENDAR = {day: i for i, day in enumerate(DAYS)}


def quotes(closes: list[float | None], changes: list[float | None], volumes: list[int] | None = None) -> pd.DataFrame:
    """行情表的資料列；收盤為 None 是當天沒有成交。"""
    nan = float("nan")
    prices = [nan if c is None else c for c in closes]
    return pd.DataFrame({"code": "1111", "date": DAYS[:len(closes)], "open": prices, "high": prices, "low": prices,
                         "close": prices, "volume": volumes or [0 if c is None else 1000 for c in closes],
                         "change": [nan if c is None else c for c in changes]})


def build(rows: pd.DataFrame, results=(), actions=(), first: date = date(2026, 1, 29), listed_before=date(2026, 1, 29)):
    return build_history(rows, security(first), list(results), list(actions), UNIVERSE, listed_before=listed_before,
                         calendar=CALENDAR)


def security(first: date = date(2026, 1, 29), market: Market = Market.OTC) -> Security:
    return Security("1111", "測試", market, first, date(2026, 2, 4), None)


UNIVERSE = Universe({date(2026, 2, 2): frozenset({"1111"})})


def test_bars_volume_and_universe() -> None:
    history, derived = build(quotes([10, 10, 10], [0, 0, 0], volumes=[1499, 1500, 0]))

    assert list(history.bars.index) == [date(2026, 1, 29), date(2026, 1, 30), date(2026, 2, 2)]
    assert list(history.bars["volume"]) == [1, 2, 0]            # 股 → 張，四捨五入
    assert list(history.in_universe) == [False, False, True]
    assert history.listed_on is None                            # 第一次出現就是資料的第一天：視為之前就已掛牌
    assert (history.info.group, history.info.market, history.info.subcategory) == ("上櫃", Market.OTC, None)
    assert derived == []
    assert list(history.eligible()) == [False, False, True]


def test_first_appearance_after_the_data_start_is_the_listing_day() -> None:
    history, _ = build(quotes([10] * 5, [0] * 5), listed_before=date(2015, 1, 5))

    assert history.listed_on == date(2026, 1, 29)
    assert history.first_position == 5                          # 前五個交易日不產生訊號


def test_reference_from_the_quotes_fills_a_missing_event() -> None:
    # 02-02 收 9.5、漲跌 −0.6：參考價 10.1 ≠ 前一日收盤 11，以 10.1 / 11 還原
    history, derived = build(quotes([10, 11, 9.5], [0, 1, -0.6]))

    assert derived == [DividendResult("1111", date(2026, 2, 2), 11.0, 10.1)]
    assert history.factors.iloc[2] == pytest.approx(10.1 / 11)
    assert history.adjusted["close"].iloc[1] == pytest.approx(11 * 10.1 / 11)


def test_official_event_takes_priority_and_is_moved_to_the_next_trading_day() -> None:
    # 官方除權息日 01-31（週六）順延到 02-02，推出的參考價不另外計入
    official = DividendResult("1111", date(2026, 1, 31), 11.0, 10.0)
    history, derived = build(quotes([10, 11, 9.5], [0, 1, -0.6]), results=[official])

    assert derived == []
    assert history.factors.iloc[2] == pytest.approx(10 / 11)


def test_flagged_day_without_event_is_not_adjusted() -> None:
    # 標 X、除息等的日子漲跌為 None，推不出參考價，也沒有官方事件：不還原（品質報告列出）
    history, derived = build(quotes([10, 11, 9.5], [0, 1, None]))

    assert derived == [] and list(history.factors) == [1.0, 1.0, 1.0]


def test_reference_reset_after_a_day_without_trades_is_not_an_event() -> None:
    # 上櫃股票沒有成交的隔天，櫃買中心會調整參考價（9960 2024-10-01：前一個成交 26.55，參考價 26.35），不是事件
    history, derived = build(quotes([26.55, None, 26.25], [0.3, None, -0.1]))

    assert list(history.bars.index) == [date(2026, 1, 29), date(2026, 2, 2)]
    assert derived == [] and list(history.factors) == [1.0, 1.0]


def test_capital_reduction_is_applied() -> None:
    action = CorporateAction("1111", date(2026, 2, 2), ActionKind.CAPITAL_REDUCTION, 11.0, 22.0)
    history, _ = build(quotes([10, 11, 21], [0, 1, None]), actions=[action])

    assert history.factors.iloc[2] == pytest.approx(2.0)
