"""E1 bull_ma5_breakout：多頭突破五日線紅 K（PROJECT_PLAN.md 5.3）。"""

from screener.config import require
from screener.formatting import format_number as num
from screener.models import Side, Signal, StockContext
from screener.rules.base import TriggerRule, register_rule
from screener.rules.conditions import crosses_above_ma, is_red


@register_rule("bull_ma5_breakout")
class BullMa5Breakout(TriggerRule):
    name = "多頭突破五日線紅K"
    side = Side.LONG
    params_schema = {**TriggerRule.params_schema, "ma": int}

    def configure(self, where: str) -> None:
        super().configure(where)
        self.ma: int = self.params["ma"]
        require(self.ma > 0, f"{where} > ma：必須大於 0")

    def evaluate(self, ctx: StockContext) -> Signal | None:
        """突破 MA（昨收 ≤ 昨日 MA，現價 > 今日 MA），且今日為紅 K。"""
        if not (crosses_above_ma(ctx, self.ma) and is_red(ctx)):
            return None
        ma = ctx.ma(self.ma)
        return self.signal(
            f"現價 {num(ctx.today['close'])} > MA{self.ma} {num(ma.iloc[-1])}"
            f"（昨收 {num(ctx.yesterday['close'])} ≤ {num(ma.iloc[-2])}）"
        )
