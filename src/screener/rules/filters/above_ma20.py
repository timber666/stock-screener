"""多方前置條件 above_ma20：站上月線（PROJECT_PLAN.md 5.2）。"""

from screener.config import require
from screener.models import Side, StockContext
from screener.rules.base import FilterRule, register_rule
from screener.rules.conditions import is_above_ma


@register_rule("above_ma20")
class AboveMa20(FilterRule):
    name = "站上月線"
    side = Side.LONG
    params_schema = {"ma": int}

    def configure(self, where: str) -> None:
        super().configure(where)
        self.ma: int = self.params["ma"]
        require(self.ma > 0, f"{where} > ma：必須大於 0")

    def passes(self, ctx: StockContext) -> bool:
        """現價 > 今日 MA。沒有足夠的 K 棒算出均線時不通過（D23）。"""
        return is_above_ma(ctx, self.ma)
