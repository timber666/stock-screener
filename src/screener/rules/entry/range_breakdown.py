"""E6 range_breakdown：盤整長黑破前低（PROJECT_PLAN.md 5.3）。與 E2 對稱，但不看成交量。"""

from screener.config import require
from screener.formatting import format_number as num
from screener.models import Side, Signal, StockContext
from screener.rules.base import EntryRule, register_rule
from screener.rules.conditions import body_pct, is_long_black, prior_low


@register_rule("range_breakdown")
class RangeBreakdown(EntryRule):
    name = "盤整長黑破前低"
    side = Side.SHORT
    params_schema = {**EntryRule.params_schema, "min_body_pct": float, "low_lookback_days": int}

    def configure(self, where: str) -> None:
        super().configure(where)
        self.min_body_pct: float = self.params["min_body_pct"]
        self.low_lookback_days: int = self.params["low_lookback_days"]
        require(self.min_body_pct > 0, f"{where} > min_body_pct：必須大於 0")
        require(self.low_lookback_days > 0, f"{where} > low_lookback_days：必須大於 0")

    def evaluate(self, ctx: StockContext) -> Signal | None:
        """長黑，且現價 < 前低。今日之前不足 low_lookback_days 根時不觸發（D23）。"""
        low = prior_low(ctx, self.low_lookback_days)
        if low is None or not (ctx.today["close"] < low):
            return None
        if not is_long_black(ctx, self.min_body_pct):
            return None
        return self.signal(f"實體 {-body_pct(ctx):.1f}%，跌破 {self.low_lookback_days} 日低 {num(low)}")
