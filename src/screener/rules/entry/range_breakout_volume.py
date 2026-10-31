"""E2 range_breakout_volume：盤整長紅爆量過前高（PROJECT_PLAN.md 5.3）。"""

from screener.config import require
from screener.formatting import format_number as num
from screener.models import Side, Signal, StockContext
from screener.rules.base import EntryRule, register_rule
from screener.rules.conditions import body_pct, is_long_red, is_volume_spike, prior_high, volume_ratio


@register_rule("range_breakout_volume")
class RangeBreakoutVolume(EntryRule):
    name = "盤整長紅爆量過前高"
    side = Side.LONG
    params_schema = {**EntryRule.params_schema, "min_body_pct": float, "volume_ratio": float,
                     "high_lookback_days": int}

    def configure(self, where: str) -> None:
        super().configure(where)
        self.min_body_pct: float = self.params["min_body_pct"]
        self.volume_ratio: float = self.params["volume_ratio"]
        self.high_lookback_days: int = self.params["high_lookback_days"]
        require(self.min_body_pct > 0, f"{where} > min_body_pct：必須大於 0")
        require(self.volume_ratio > 0, f"{where} > volume_ratio：必須大於 0")
        require(self.high_lookback_days > 0, f"{where} > high_lookback_days：必須大於 0")

    def evaluate(self, ctx: StockContext) -> Signal | None:
        """長紅、爆量，且現價 > 前高。今日之前不足 high_lookback_days 根時不觸發（D23）。"""
        high = prior_high(ctx, self.high_lookback_days)
        if high is None or not (ctx.today["close"] > high):
            return None
        if not (is_long_red(ctx, self.min_body_pct) and is_volume_spike(ctx, self.volume_ratio)):
            return None
        return self.signal(
            f"實體 {body_pct(ctx):.1f}%，量比 {volume_ratio(ctx):.2f}x，"
            f"突破 {self.high_lookback_days} 日高 {num(high)}"
        )
