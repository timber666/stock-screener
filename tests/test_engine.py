"""規則引擎的流程（PROJECT_PLAN.md 5.1）：依方向分別判斷前置條件 → 適用趨勢 → 觸發規則。"""

from collections.abc import Sequence
from dataclasses import replace

from conftest import REPO_CONFIG_DIR, make_bars, make_context
from screener.config import load_config
from screener.engine import Engine
from screener.models import Position, Side, Signal, StockContext, Trend
from screener.rules.base import EntryRule, FilterRule, TriggerRule


class StubFilter(FilterRule):
    id = "stub_filter"
    name = "測試前置條件"

    def __init__(self, result: bool, side: Side = Side.LONG) -> None:
        super().__init__({})
        self.result = result
        self.side = side

    def passes(self, ctx: StockContext) -> bool:
        return self.result


class StubTrigger(TriggerRule):
    id = "stub_entry"
    name = "測試觸發"

    def __init__(self, trends: list[str], fires: bool = True, name: str = "測試觸發", side: Side = Side.LONG) -> None:
        super().__init__({"trends": trends})
        self.fires = fires
        self.label = name
        self.side = side
        self.calls = 0

    def evaluate(self, ctx: StockContext) -> Signal | None:
        self.calls += 1
        return Signal(self.id, self.label, "detail", self.side) if self.fires else None


CTX = make_context(make_bars([10.0, 11.0]), trend=Trend.BULL)


def test_returns_signals_of_all_triggered_rules_in_order() -> None:
    engine = Engine(filters=[StubFilter(True)],
                    trigger_rules=[StubTrigger([], name="甲"), StubTrigger([], fires=False), StubTrigger(["bull"], name="乙")])

    assert [s.rule_name for s in engine.evaluate(CTX)] == ["甲", "乙"]


def test_failed_filter_skips_trigger_rules_of_the_same_side_only() -> None:
    long_rule, short_rule = StubTrigger([], name="多"), StubTrigger([], name="空", side=Side.SHORT)
    engine = Engine(filters=[StubFilter(True), StubFilter(False), StubFilter(True, side=Side.SHORT)],
                    trigger_rules=[long_rule, short_rule])

    assert [s.rule_name for s in engine.evaluate(CTX)] == ["空"]
    assert long_rule.calls == 0


def test_long_signals_come_before_short_signals() -> None:
    engine = Engine(filters=[], trigger_rules=[StubTrigger([], name="空", side=Side.SHORT), StubTrigger([], name="多")])

    assert [(s.rule_name, s.side) for s in engine.evaluate(CTX)] == [("多", Side.LONG), ("空", Side.SHORT)]


def test_rule_for_other_trends_is_not_evaluated() -> None:
    rule = StubTrigger(["range", "bear"])

    assert Engine(filters=[], trigger_rules=[rule]).evaluate(CTX) == []
    assert rule.calls == 0


def test_repo_rules_long_and_short_filters_are_exclusive() -> None:
    engine = Engine.from_config(load_config(REPO_CONFIG_DIR).rules)
    above, below = engine.filters
    flat = make_context(make_bars([100.0] * 20))                 # 現價 = MA20：兩邊都不通過

    assert (above.side, below.side) == (Side.LONG, Side.SHORT)
    assert not above.passes(flat) and not below.passes(flat)


class StubEntry(EntryRule):
    id = "stub_entry_rule"
    name = "測試進場"

    def __init__(self, result: bool, side: Side = Side.LONG) -> None:
        super().__init__({})
        self.result = result
        self.side = side

    def matches(self, ctx: StockContext, signals: Sequence[Signal]) -> bool:
        return self.result


def test_entry_sides_lists_the_sides_of_matching_rules() -> None:
    engine = Engine(filters=[], trigger_rules=[], entry_rules=[StubEntry(True), StubEntry(False, side=Side.SHORT)])

    assert engine.entry_sides(CTX, []) == {Side.LONG}


def test_entry_rules_are_built_from_config_and_default_to_none() -> None:
    engine = Engine.from_config(load_config(REPO_CONFIG_DIR).rules)

    assert [rule.id for rule in engine.entry_rules] == ["bull_ma5_entry"]
    assert Engine(filters=[], trigger_rules=[]).entry_sides(CTX, []) == set()


def test_exit_rules_come_from_config_and_fire_in_order() -> None:
    engine = Engine.from_config(load_config(REPO_CONFIG_DIR).rules)
    falling = make_context(make_bars([10.0] * 5 + [9.0]))       # 現價 9 < MA5 9.8
    held = Position(code="2330", entry_date=falling.bars.index[0], entry_price=10.0, held_days=5)

    assert [rule.id for rule in engine.exit_rules] == ["below_ma_exit", "max_hold_exit"]
    assert [s.rule_name for s in engine.evaluate_exits(falling, held)] == ["跌破均線", "持有到期"]
    assert engine.evaluate_exits(make_context(make_bars([10.0] * 6)), replace(held, held_days=4)) == []
    assert Engine(filters=[], trigger_rules=[]).evaluate_exits(falling, held) == []
