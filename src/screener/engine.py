"""規則引擎：依多方、空方分別評估前置條件與觸發規則（PROJECT_PLAN.md 5.1）。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from screener.config import RulesConfig
from screener.models import Side, Signal, StockContext
from screener.rules.base import EntryRule, ExitRule, FilterRule, TriggerRule, create_rules


@dataclass(frozen=True)
class Engine:
    filters: Sequence[FilterRule]
    trigger_rules: Sequence[TriggerRule]
    exit_rules: Sequence[ExitRule] = ()
    entry_rules: Sequence[EntryRule] = ()

    @classmethod
    def from_config(cls, config: RulesConfig) -> "Engine":
        """依 rules.yaml 建立已啟用的規則；id 或參數錯誤時拋出 ConfigError。"""
        return cls(
            filters=create_rules(config.filters, FilterRule, "filters"),
            trigger_rules=create_rules(config.trigger_rules, TriggerRule, "trigger_rules"),
            exit_rules=create_rules(config.exit_rules, ExitRule, "exit_rules"),
            entry_rules=create_rules(config.entry_rules, EntryRule, "entry_rules"),
        )

    def evaluate(self, ctx: StockContext) -> list[Signal]:
        """多方、空方各自判斷：該方向的前置條件全部通過後，逐一評估適用於該檔趨勢的觸發規則。

        回傳所有觸發的訊號，先多方後空方，同方向內依 rules.yaml 的順序。
        """
        signals = []
        for side in Side:
            if not all(rule.passes(ctx) for rule in self.filters if rule.side is side):
                continue
            for rule in self.trigger_rules:
                if rule.side is side and rule.applies_to(ctx.trend) and (signal := rule.evaluate(ctx)) is not None:
                    signals.append(signal)
        return signals

    def entry_sides(self, ctx: StockContext, signals: Sequence[Signal]) -> set[Side]:
        """符合進場規則的方向（5.5）。只判斷規則本身；漲跌停不成交要用參考價判斷，由呼叫端處理。"""
        return {rule.side for rule in self.entry_rules if rule.matches(ctx, signals)}

    def evaluate_exits(self, ctx: StockContext, position: Any) -> list[Signal]:
        """出場規則的呼叫點（5.4）。目前沒有任何出場規則的實作，也還沒有部位資料。"""
        return [signal for rule in self.exit_rules if (signal := rule.evaluate(ctx, position)) is not None]
