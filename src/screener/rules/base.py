"""規則介面與註冊表（PROJECT_PLAN.md 10.1、10.2）。

新增規則：在 rules/filters、rules/trigger、rules/entry 或 rules/exit 新增模組，繼承對應的類別並以
@register_rule("id") 註冊，再到 rules.yaml 加入設定。模組會被自動匯入，不需要修改引擎。
"""

import importlib
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar, TypeVar

from screener.config import RuleConfig, check_keys, get_field, require
from screener.models import Side, Signal, StockContext, Trend

_RULE_PACKAGES = ("filters", "trigger", "entry", "exit")
_REGISTRY: dict[str, type["Rule"]] = {}


class Rule(ABC):
    id: ClassVar[str]               # 由 register_rule 設定
    name: ClassVar[str]             # 顯示名稱，例：多頭突破五日線紅K
    params_schema: ClassVar[Mapping[str, type]] = {}   # 參數名稱 → 型別；全部必填

    def __init__(self, params: Mapping[str, Any]) -> None:
        where = f"rules.yaml > {self.id} > params"
        check_keys(params, set(self.params_schema), where)
        self.params = {key: get_field(params, key, kind, where) for key, kind in self.params_schema.items()}
        self.configure(where)

    def configure(self, where: str) -> None:
        """檢查參數值並存成屬性；子類別覆寫時要先呼叫 super().configure(where)。"""


class FilterRule(Rule):
    """前置條件：同方向的前置條件全部通過，才評估該方向的觸發規則（5.2）。"""

    side: ClassVar[Side]

    @abstractmethod
    def passes(self, ctx: StockContext) -> bool: ...


class TriggerRule(Rule):
    """觸發規則（5.3）。params 必須有 trends：適用的趨勢，空陣列代表不限（含資料不足，D18）。"""

    side: ClassVar[Side]
    params_schema: ClassVar[Mapping[str, type]] = {"trends": list}

    def configure(self, where: str) -> None:
        super().configure(where)
        allowed = {Trend.BULL.value, Trend.RANGE.value, Trend.BEAR.value}
        for trend in self.params["trends"]:
            require(trend in allowed, f"{where} > trends：應為 bull、range 或 bear，實際為 {trend!r}")
        self.trends = frozenset(Trend(t) for t in self.params["trends"])

    def applies_to(self, trend: Trend) -> bool:
        return not self.trends or trend in self.trends

    @abstractmethod
    def evaluate(self, ctx: StockContext) -> Signal | None: ...

    def signal(self, detail: str) -> Signal:
        return Signal(rule_id=self.id, rule_name=self.name, detail=detail, side=self.side)


class EntryRule(Rule):
    """進場規則（5.5）：判斷當天觸發的候選是否符合進場，不影響觸發規則與趨勢。"""

    side: ClassVar[Side]

    @abstractmethod
    def matches(self, ctx: StockContext, signals: Sequence[Signal]) -> bool: ...


class ExitRule(Rule):
    """出場規則：已定義（5.4），目前不實作任何子類別，只保留介面。"""

    @abstractmethod
    def evaluate(self, ctx: StockContext, position: Any) -> Signal | None: ...


R = TypeVar("R", bound=Rule)


def register_rule(rule_id: str) -> Callable[[type[R]], type[R]]:
    def decorator(cls: type[R]) -> type[R]:
        if rule_id in _REGISTRY:
            raise ValueError(f"規則 id 重複註冊：{rule_id}")
        if issubclass(cls, (FilterRule, TriggerRule, EntryRule)) and not isinstance(getattr(cls, "side", None), Side):
            raise TypeError(f"規則 {rule_id} 必須以 side 指定多方或空方")
        cls.id = rule_id
        _REGISTRY[rule_id] = cls
        return cls

    return decorator


def load_builtin_rules() -> None:
    """匯入 rules/filters、trigger、entry、exit 下的所有模組，讓它們完成註冊。"""
    for package_name in _RULE_PACKAGES:
        package = importlib.import_module(f"{__package__}.{package_name}")
        for module in pkgutil.iter_modules(package.__path__):
            importlib.import_module(f"{package.__name__}.{module.name}")


def create_rules(configs: Sequence[RuleConfig], kind: type[R], section: str) -> list[R]:
    """依 rules.yaml 的設定建立規則，只回傳已啟用的。停用的規則也會建立，以便檢查參數。"""
    load_builtin_rules()
    rules: list[R] = []
    for config in configs:
        where = f"rules.yaml > {section} > {config.id}"
        cls = _REGISTRY.get(config.id)
        require(cls is not None, f"{where}：未知的規則 id，已註冊的有 {', '.join(sorted(_REGISTRY))}")
        require(issubclass(cls, kind), f"{where}：此規則不能放在 {section}")
        rule = cls(config.params)
        if config.enabled:
            rules.append(rule)
    return rules
