"""E3 ma_bull_alignment 三線多排、E5 ma_bear_alignment 三線空排（PROJECT_PLAN.md 5.3）。"""

from collections.abc import Callable, Sequence
from typing import ClassVar

from screener.config import require
from screener.formatting import format_number as num
from screener.models import Side, Signal, StockContext
from screener.rules.base import TriggerRule, register_rule
from screener.rules.conditions import is_strictly_ascending, is_strictly_descending, ma_values


class MaAlignment(TriggerRule):
    """均線排列：今日形成指定的排列；only_on_formation 時昨日必須尚未形成。不看 K 棒顏色（D25）。"""

    params_schema = {**TriggerRule.params_schema, "mas": list, "only_on_formation": bool}
    is_aligned: ClassVar[Callable[[Sequence[float]], bool]]
    symbol: ClassVar[str]               # 顯示用的比較符號

    def configure(self, where: str) -> None:
        super().configure(where)
        self.mas: list[int] = self.params["mas"]
        self.only_on_formation: bool = self.params["only_on_formation"]
        require(
            len(self.mas) >= 2 and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in self.mas),
            f"{where} > mas：應為至少兩個正整數",
        )
        require(self.mas == sorted(set(self.mas)), f"{where} > mas：應由短到長排列且不重複，例如 [5, 10, 20]")

    def evaluate(self, ctx: StockContext) -> Signal | None:
        """昨日的均線算不出來時，無法確認是否為形成當天，不觸發（D23）。"""
        today = ma_values(ctx, self.mas)
        if today is None or not self.is_aligned(today):
            return None
        if self.only_on_formation:
            yesterday = ma_values(ctx, self.mas, days_ago=1)
            if yesterday is None or self.is_aligned(yesterday):
                return None
        return self.signal(f" {self.symbol} ".join(f"MA{n} {num(v)}" for n, v in zip(self.mas, today)))


@register_rule("ma_bull_alignment")
class MaBullAlignment(MaAlignment):
    name = "三線多排"
    side = Side.LONG
    is_aligned = staticmethod(is_strictly_descending)      # MA5 > MA10 > MA20
    symbol = ">"


@register_rule("ma_bear_alignment")
class MaBearAlignment(MaAlignment):
    name = "三線空排"
    side = Side.SHORT
    is_aligned = staticmethod(is_strictly_ascending)       # MA5 < MA10 < MA20
    symbol = "<"
