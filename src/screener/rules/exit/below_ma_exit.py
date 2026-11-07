"""below_ma_exit：收盤跌破均線就賣出（PROJECT_PLAN.md 5.4、D51）。"""

from screener.config import require
from screener.formatting import format_number as num
from screener.models import Position, Side, Signal, StockContext
from screener.rules.base import ExitRule, register_rule
from screener.rules.conditions import is_below_ma


@register_rule("below_ma_exit")
class BelowMaExit(ExitRule):
    name = "跌破均線"
    side = Side.LONG
    params_schema = {"ma": int}

    def configure(self, where: str) -> None:
        super().configure(where)
        self.ma: int = self.params["ma"]
        require(self.ma > 0, f"{where} > ma：必須大於 0")

    def evaluate(self, ctx: StockContext, position: Position) -> Signal | None:
        """現價 < 今日 MA；剛好等於時不賣。"""
        if not is_below_ma(ctx, self.ma):
            return None
        return self.signal(f"現價 {num(ctx.today['close'])} < MA{self.ma} {num(ctx.ma(self.ma).iloc[-1])}")
