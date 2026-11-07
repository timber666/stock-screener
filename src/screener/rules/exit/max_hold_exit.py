"""max_hold_exit：持有到第 N 個交易日就賣出（PROJECT_PLAN.md 5.4、D51）。"""

from screener.config import require
from screener.models import Position, Side, Signal, StockContext
from screener.rules.base import ExitRule, register_rule


@register_rule("max_hold_exit")
class MaxHoldExit(ExitRule):
    name = "持有到期"
    side = Side.LONG
    params_schema = {"days": int}

    def configure(self, where: str) -> None:
        super().configure(where)
        self.days: int = self.params["days"]
        require(self.days > 0, f"{where} > days：必須大於 0")

    def evaluate(self, ctx: StockContext, position: Position) -> Signal | None:
        """進場後第 days 個交易日（進場當天不算）。"""
        if position.held_days < self.days:
            return None
        return self.signal(f"持有 {position.held_days} 個交易日")
