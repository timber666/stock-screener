"""E4 bear_ma5_breakdown：空頭跌破五日線黑 K（PROJECT_PLAN.md 5.3），與 E1 對稱。"""

from screener.config import require
from screener.formatting import format_number as num
from screener.models import Side, Signal, StockContext
from screener.rules.base import EntryRule, register_rule
from screener.rules.conditions import crosses_below_ma, is_black


@register_rule("bear_ma5_breakdown")
class BearMa5Breakdown(EntryRule):
    name = "空頭跌破五日線黑K"
    side = Side.SHORT
    params_schema = {**EntryRule.params_schema, "ma": int}

    def configure(self, where: str) -> None:
        super().configure(where)
        self.ma: int = self.params["ma"]
        require(self.ma > 0, f"{where} > ma：必須大於 0")

    def evaluate(self, ctx: StockContext) -> Signal | None:
        """跌破 MA（昨收 ≥ 昨日 MA，現價 < 今日 MA），且今日為黑 K。"""
        if not (crosses_below_ma(ctx, self.ma) and is_black(ctx)):
            return None
        ma = ctx.ma(self.ma)
        return self.signal(
            f"現價 {num(ctx.today['close'])} < MA{self.ma} {num(ma.iloc[-1])}"
            f"（昨收 {num(ctx.yesterday['close'])} ≥ {num(ma.iloc[-2])}）"
        )
