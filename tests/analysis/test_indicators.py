import math

import pandas as pd
import pytest

from screener.analysis.indicators import sma


def test_sma_averages_last_n_closes() -> None:
    result = sma(pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 20.0]), 5)

    assert all(math.isnan(v) for v in result.iloc[:4])
    assert result.iloc[4:].tolist() == [12.0, 14.0]


def test_sma_equals_close_exactly_when_mathematically_equal() -> None:
    # 後 5 根合計 51.75，MA5 恰為 10.35；未修正的 rolling mean 會得到 10.350000000000001，
    # 「收盤 ≥ MA5」的觸碰就會漏判
    closes = pd.Series([10.85, 10.85, 10.25, 10.05, 10.55, 10.55, 10.35])
    assert closes.rolling(5).mean().iloc[-1] != 10.35       # 確認這組資料確實有浮點誤差

    ma = sma(closes, 5)

    assert ma.iloc[-1] == closes.iloc[-1]


def test_sma_keeps_small_real_differences() -> None:
    # 最小差距：收盤價差 0.01 的 1/n
    ma = sma(pd.Series([10.00, 10.00, 10.00, 10.00, 10.01]), 5)

    assert ma.iloc[-1] == 10.002
    assert ma.iloc[-1] > 10.00


@pytest.mark.parametrize("n", [0, -5])
def test_sma_rejects_non_positive_window(n: int) -> None:
    with pytest.raises(ValueError, match="均線天數必須大於 0"):
        sma(pd.Series([1.0]), n)
