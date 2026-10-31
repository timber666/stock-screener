"""消除浮點誤差（PROJECT_PLAN.md 10.3）。

價格最多兩位小數、成交量為整數、設定值只有少數幾位小數，由它們加減乘除得到的精確值
都只有有限位小數，四捨五入到小數 DECIMALS 位就能消除浮點誤差，讓「剛好等於門檻」的
比較正確。真正不相等時差距遠大於 10^-DECIMALS，不會因此變成相等。
"""

DECIMALS = 6


def normalize(value: float) -> float:
    return round(value, DECIMALS)
