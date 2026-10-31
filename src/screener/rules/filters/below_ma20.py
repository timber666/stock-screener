"""空方前置條件 below_ma20：在月線之下（PROJECT_PLAN.md 5.2）。"""

from screener.config import require
from screener.models import Side, StockContext
from screener.rules.base import FilterRule, register_rule
from screener.rules.conditions import is_below_ma


@register_rule("below_ma20")
class BelowMa20(FilterRule):
    name = "跌破月線"
    side = Side.SHORT
    params_schema = {"ma": int}

    def configure(self, where: str) -> None:
        super().configure(where)
        self.ma: int = self.params["ma"]
        require(self.ma > 0, f"{where} > ma：必須大於 0")

    def passes(self, ctx: StockContext) -> bool:
        """現價 < 今日 MA。剛好等於均線時多方、空方都不通過；算不出均線時不通過（D23）。"""
        return is_below_ma(ctx, self.ma)
