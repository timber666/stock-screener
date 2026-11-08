"""全市場的動態股票池（RESEARCH_PLAN.md 4.3、R7）。"""

from datetime import date

import numpy as np
import pandas as pd

from screener.research.universe import Universe, build_universe

DAYS = [date(2026, 1, 28), date(2026, 1, 29), date(2026, 1, 30), date(2026, 2, 2), date(2026, 2, 3), date(2026, 3, 2)]


def values(table: dict[str, list[float | None]]) -> pd.DataFrame:
    """成交金額；None 為不在行情表中。"""
    return pd.DataFrame({code: [np.nan if v is None else v for v in column] for code, column in table.items()},
                        index=pd.Index(DAYS, name="date"))


def test_monthly_pool_is_ranked_by_average_value_before_the_first_day() -> None:
    table = values({
        "1111": [10, 10, 10, 1, 1, 1],
        "2222": [5, 5, 5, 50, 50, 50],
        "3333": [None, None, 30, 30, 30, 30],       # 1/30 才掛牌：之前的日子算 0，平均 (0 + 0 + 30) / 3 = 10
    })

    universe = build_universe(table, size=2, lookback_days=3)

    # 1 月：之前不足 3 個交易日，沒有股票池。2 月：1/28～1/30 平均 1111 10、3333 10、2222 5（不含 2/2 當天）
    # 3 月：2/2～2/3 與 1/30 平均 2222 35、3333 30、1111 4
    assert universe.months == {date(2026, 2, 2): frozenset({"1111", "3333"}),
                               date(2026, 3, 2): frozenset({"2222", "3333"})}
    assert universe.members(date(2026, 2, 3)) == {"1111", "3333"}
    assert universe.members(date(2026, 1, 29)) == frozenset()
    assert universe.codes == {"1111", "2222", "3333"}
    assert universe.first_day == date(2026, 2, 2)


def test_ties_are_broken_by_code() -> None:
    table = values({"3333": [10] * 6, "1111": [10] * 6, "2222": [10] * 6})

    assert build_universe(table, size=1, lookback_days=3).months[date(2026, 2, 2)] == {"1111"}


def test_stock_missing_on_the_previous_day_is_not_eligible() -> None:
    # 4444 平均最高，但 2/3 已不在行情表（例：下市），3 月不能入選
    table = values({"4444": [100, 100, 100, 100, None, None], "1111": [1] * 6})

    universe = build_universe(table, size=1, lookback_days=3)

    assert universe.months[date(2026, 2, 2)] == {"4444"}
    assert universe.months[date(2026, 3, 2)] == {"1111"}


def test_mask_marks_the_months_in_the_pool() -> None:
    universe = Universe({date(2026, 2, 2): frozenset({"1111"}), date(2026, 3, 2): frozenset({"2222"})})

    assert list(universe.mask("1111", DAYS)) == [False, False, False, True, True, False]
    assert list(universe.mask("2222", DAYS)) == [False, False, False, False, False, True]
    assert list(universe.mask("9999", DAYS)) == [False] * 6
