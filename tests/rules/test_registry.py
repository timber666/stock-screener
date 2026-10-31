"""規則註冊表與參數驗證（PROJECT_PLAN.md 10.1、10.2）。"""

from typing import Any

import pytest

from conftest import REPO_CONFIG_DIR
from screener.config import ConfigError, RuleConfig, load_config
from screener.models import Side
from screener.rules import base
from screener.rules.base import EntryRule, FilterRule, create_rules, load_builtin_rules, register_rule

E1_PARAMS = {"trends": ["bull"], "ma": 5}


def entry(rule_id: str, params: dict[str, Any], enabled: bool = True) -> list[RuleConfig]:
    return [RuleConfig(id=rule_id, enabled=enabled, params=params)]


def test_builtin_rules_are_discovered() -> None:
    load_builtin_rules()

    assert {"above_ma20", "below_ma20", "bull_ma5_breakout", "range_breakout_volume", "ma_bull_alignment",
            "bear_ma5_breakdown", "range_breakdown", "ma_bear_alignment"} <= set(base._REGISTRY)


def test_repo_rules_config_builds() -> None:
    rules = load_config(REPO_CONFIG_DIR).rules

    assert [(r.id, r.side) for r in create_rules(rules.filters, FilterRule, "filters")] == [
        ("above_ma20", Side.LONG), ("below_ma20", Side.SHORT),
    ]
    assert [(r.name, r.side) for r in create_rules(rules.entry_rules, EntryRule, "entry_rules")] == [
        ("多頭突破五日線紅K", Side.LONG), ("盤整長紅爆量過前高", Side.LONG), ("三線多排", Side.LONG),
        ("空頭跌破五日線黑K", Side.SHORT), ("盤整長黑破前低", Side.SHORT), ("三線空排", Side.SHORT),
    ]


def test_disabled_rule_is_skipped_but_still_validated() -> None:
    assert create_rules(entry("bull_ma5_breakout", E1_PARAMS, enabled=False), EntryRule, "entry_rules") == []

    with pytest.raises(ConfigError, match="缺少必要欄位 `ma`"):
        create_rules(entry("bull_ma5_breakout", {"trends": []}, enabled=False), EntryRule, "entry_rules")


@pytest.mark.parametrize(
    ("rule_id", "params", "message"),
    [
        ("bull_ma5_breakuot", E1_PARAMS, "entry_rules > bull_ma5_breakuot：未知的規則 id"),
        ("above_ma20", {"ma": 20}, "above_ma20：此規則不能放在 entry_rules"),
        ("bull_ma5_breakout", {"trends": ["bull"]}, "缺少必要欄位 `ma`"),
        ("bull_ma5_breakout", {**E1_PARAMS, "mas": 5}, "未知的欄位 `mas`"),
        ("bull_ma5_breakout", {**E1_PARAMS, "ma": "5"}, "ma：應為整數"),
        ("bull_ma5_breakout", {**E1_PARAMS, "ma": 0}, "ma：必須大於 0"),
        ("bull_ma5_breakout", {**E1_PARAMS, "trends": ["unknown"]}, "trends：應為 bull、range 或 bear"),
        ("ma_bull_alignment", {"trends": [], "mas": [5], "only_on_formation": True}, "mas：應為至少兩個正整數"),
        ("ma_bull_alignment", {"trends": [], "mas": [20, 10, 5], "only_on_formation": True}, "mas：應由短到長排列"),
        ("range_breakout_volume",
         {"trends": [], "min_body_pct": 3, "volume_ratio": 0, "high_lookback_days": 40}, "volume_ratio：必須大於 0"),
        ("range_breakdown",
         {"trends": [], "min_body_pct": 3, "volume_ratio": 1.5, "low_lookback_days": 40}, "未知的欄位 `volume_ratio`"),
    ],
)
def test_invalid_rule_config_is_rejected(rule_id: str, params: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        create_rules(entry(rule_id, params), EntryRule, "entry_rules")


def test_duplicate_registration_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})

    @register_rule("dup")
    class First(FilterRule):
        name = "第一"
        side = Side.LONG

        def passes(self, ctx) -> bool:
            return True

    with pytest.raises(ValueError, match="規則 id 重複註冊：dup"):
        register_rule("dup")(First)


def test_rule_without_side_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})

    class NoSide(EntryRule):
        name = "沒有方向"

        def evaluate(self, ctx):
            return None

    with pytest.raises(TypeError, match="必須以 side 指定多方或空方"):
        register_rule("no_side")(NoSide)
