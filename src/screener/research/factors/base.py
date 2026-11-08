"""因子的註冊表與計算輸入（RESEARCH_PLAN.md 4.4；做法同 PROJECT_PLAN.md 10.2 的規則）。

新增一種因子：在 research/factors/ 寫一個函式，第一個參數為 FactorContext、其餘為關鍵字參數，回傳日期 × 代號的
DataFrame，並以 @register_factor 註冊；在 research.yaml 的 factors > definitions 加一項即可，不需要修改評估程式。
"""

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from screener.config import ConfigError, FactorDefinition, MarketConfig, TrendConfig
from screener.research.factors.panel import Panel

FactorFunction = Callable[..., pd.DataFrame]


@dataclass(frozen=True)
class FactorContext:
    """計算因子的輸入。revenue、valuations、shares 為 market.db 的原始資料表（M9b）。"""

    panel: Panel
    trend: TrendConfig
    market: MarketConfig
    revenue: pd.DataFrame
    valuations: pd.DataFrame
    shares: pd.DataFrame


@dataclass(frozen=True)
class FactorKind:
    function: FactorFunction
    category: str
    groups: Mapping[int, str] | None    # 類別因子：值 → 組名；連續因子為 None


@dataclass(frozen=True)
class Factor:
    """research.yaml 的一個因子，參數已驗證。"""

    id: str
    kind: str
    category: str
    params: Mapping[str, Any]
    function: FactorFunction
    groups: Mapping[int, str] | None

    @property
    def is_categorical(self) -> bool:
        return self.groups is not None

    def compute(self, context: FactorContext) -> pd.DataFrame:
        return self.function(context, **self.params)


_KINDS: dict[str, FactorKind] = {}


def register_factor(kind: str, *, category: str, groups: Mapping[int, str] | None = None
                    ) -> Callable[[FactorFunction], FactorFunction]:
    def decorator(function: FactorFunction) -> FactorFunction:
        if kind in _KINDS:
            raise ValueError(f"因子 {kind} 重複註冊")
        _KINDS[kind] = FactorKind(function, category, groups)
        return function

    return decorator


def build_factors(definitions: Sequence[FactorDefinition]) -> list[Factor]:
    """依 research.yaml 建立因子；kind 不存在或參數不符時報錯，一次列出所有問題。"""
    factors, errors = [], []
    for definition in definitions:
        where = f"research.yaml > factors > definitions 的 {definition.id}"
        kind = _KINDS.get(definition.kind)
        if kind is None:
            errors.append(f"{where}：未知的 kind `{definition.kind}`（可用：{'、'.join(sorted(_KINDS))}）")
            continue
        expected = list(inspect.signature(kind.function).parameters)[1:]
        if set(definition.params) != set(expected):
            errors.append(f"{where}：參數應為 {expected}，實際為 {list(definition.params)}")
            continue
        factors.append(Factor(definition.id, definition.kind, kind.category, definition.params, kind.function,
                              kind.groups))
    if errors:
        raise ConfigError("；".join(errors))
    return factors
