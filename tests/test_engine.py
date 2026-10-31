"""規則引擎的流程（PROJECT_PLAN.md 5.1）：依方向分別判斷前置條件 → 適用趨勢 → 進場規則。"""

from conftest import REPO_CONFIG_DIR, make_bars, make_context
from screener.config import load_config
from screener.engine import Engine
from screener.models import Side, Signal, StockContext, Trend
from screener.rules.base import EntryRule, FilterRule


class StubFilter(FilterRule):
    id = "stub_filter"
    name = "測試前置條件"

    def __init__(self, result: bool, side: Side = Side.LONG) -> None:
        super().__init__({})
        self.result = result
        self.side = side

    def passes(self, ctx: StockContext) -> bool:
        return self.result


class StubEntry(EntryRule):
    id = "stub_entry"
    name = "測試進場"

    def __init__(self, trends: list[str], fires: bool = True, name: str = "測試進場", side: Side = Side.LONG) -> None:
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
                    entry_rules=[StubEntry([], name="甲"), StubEntry([], fires=False), StubEntry(["bull"], name="乙")])

    assert [s.rule_name for s in engine.evaluate(CTX)] == ["甲", "乙"]


def test_failed_filter_skips_entry_rules_of_the_same_side_only() -> None:
    long_rule, short_rule = StubEntry([], name="多"), StubEntry([], name="空", side=Side.SHORT)
    engine = Engine(filters=[StubFilter(True), StubFilter(False), StubFilter(True, side=Side.SHORT)],
                    entry_rules=[long_rule, short_rule])

    assert [s.rule_name for s in engine.evaluate(CTX)] == ["空"]
    assert long_rule.calls == 0


def test_long_signals_come_before_short_signals() -> None:
    engine = Engine(filters=[], entry_rules=[StubEntry([], name="空", side=Side.SHORT), StubEntry([], name="多")])

    assert [(s.rule_name, s.side) for s in engine.evaluate(CTX)] == [("多", Side.LONG), ("空", Side.SHORT)]


def test_rule_for_other_trends_is_not_evaluated() -> None:
    rule = StubEntry(["range", "bear"])

    assert Engine(filters=[], entry_rules=[rule]).evaluate(CTX) == []
    assert rule.calls == 0


def test_repo_rules_long_and_short_filters_are_exclusive() -> None:
    engine = Engine.from_config(load_config(REPO_CONFIG_DIR).rules)
    above, below = engine.filters
    flat = make_context(make_bars([100.0] * 20))                 # 現價 = MA20：兩邊都不通過

    assert (above.side, below.side) == (Side.LONG, Side.SHORT)
    assert not above.passes(flat) and not below.passes(flat)


def test_exit_rules_are_reserved_and_empty() -> None:
    engine = Engine.from_config(load_config(REPO_CONFIG_DIR).rules)

    assert engine.exit_rules == []
    assert engine.evaluate_exits(CTX, position=None) == []
