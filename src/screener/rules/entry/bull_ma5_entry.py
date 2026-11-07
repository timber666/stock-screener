"""bull_ma5_entry：多方進場，E1 當天現價過昨高且五日線上揚（PROJECT_PLAN.md 5.5、D50）。"""

from collections.abc import Sequence

from screener.config import require
from screener.models import Side, Signal, StockContext
from screener.rules.base import EntryRule, register_rule
from screener.rules.conditions import is_above_yesterday_high, is_ma_rising
from screener.rules.trigger.bull_ma5_breakout import BullMa5Breakout


@register_rule("bull_ma5_entry")
class BullMa5Entry(EntryRule):
    name = "多頭突破五日線進場"
    side = Side.LONG
    params_schema = {"ma": int}

    def configure(self, where: str) -> None:
        super().configure(where)
        self.ma: int = self.params["ma"]
        require(self.ma > 0, f"{where} > ma：必須大於 0")

    def matches(self, ctx: StockContext, signals: Sequence[Signal]) -> bool:
        """當天觸發 E1，且現價 > 昨日最高價、今日 MA > 昨日 MA。"""
        return (any(signal.rule_id == BullMa5Breakout.id for signal in signals)
                and is_above_yesterday_high(ctx) and is_ma_rising(ctx, self.ma))
