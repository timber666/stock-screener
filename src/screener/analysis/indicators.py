"""技術指標（PROJECT_PLAN.md 10.3）。規則只讀取這裡算好的值，不自行重算。"""

import pandas as pd

# 收盤價最多兩位小數。均線與收盤價相等時，四捨五入到小數 6 位可消除 rolling 的浮點誤差，
# 讓「收盤 = 均線」（觸碰）的比較正確；不相等時差距至少 0.01/n，不會因四捨五入而變成相等。
_MA_DECIMALS = 6


def sma(closes: pd.Series, n: int) -> pd.Series:
    """最近 n 根 K 棒收盤價的簡單移動平均；不足 n 根的位置為 NaN。"""
    if n <= 0:
        raise ValueError(f"均線天數必須大於 0：{n}")
    return closes.rolling(window=n, min_periods=n).mean().round(_MA_DECIMALS)
