"""漲跌停價（PROJECT_PLAN.md 14.1）。案例取自 MIS 2026-10-30 的 u／w 欄位與官方除權息結果。"""

import pytest

from screener.backtest.market import limit_prices


@pytest.mark.parametrize(
    ("reference", "up", "down"),
    [
        (2415.0, 2655.0, 2175.0),       # 2330 台積電：升降單位 5 元
        (814.0, 895.0, 733.0),          # 6488 環球晶：升降單位 1 元
        (368.5, 405.0, 332.0),          # 4542 科嶠：升降單位 0.5 元
        (1499.99, 1645.0, 1350.0),      # 2330 2025-12-11 除息，參考價 1499.99 與官方公告的漲跌停價
    ],
)
def test_matches_official_limit_prices(reference: float, up: float, down: float) -> None:
    assert limit_prices(reference) == (up, down)


def test_limit_crossing_a_tick_boundary_uses_the_tick_at_the_limit_price() -> None:
    # 9.98 × 1.1 = 10.978：10 元以上升降單位 0.05，捨去成 10.95；9.98 × 0.9 = 8.982：0.01，進位成 8.99
    assert limit_prices(9.98) == (10.95, 8.99)
