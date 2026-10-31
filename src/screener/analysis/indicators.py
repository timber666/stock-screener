"""技術指標（PROJECT_PLAN.md 10.3）。規則只讀取這裡算好的值，不自行重算。"""

import pandas as pd

from screener.analysis.numeric import DECIMALS


def sma(closes: pd.Series, n: int) -> pd.Series:
    """最近 n 根 K 棒收盤價的簡單移動平均；不足 n 根的位置為 NaN。

    結果四捨五入到小數 DECIMALS 位以消除 rolling 的浮點誤差，讓「收盤 = 均線」（觸碰）的
    比較正確；收盤價與均線不相等時差距至少 0.01/n，不會因此變成相等。
    """
    if n <= 0:
        raise ValueError(f"均線天數必須大於 0：{n}")
    return closes.rolling(window=n, min_periods=n).mean().round(DECIMALS)
