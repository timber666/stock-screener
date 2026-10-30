"""核心資料型別（PROJECT_PLAN.md 10.1）。"""

from dataclasses import dataclass
from enum import Enum


class Market(str, Enum):
    TSE = "tse"    # 上市
    OTC = "otc"    # 上櫃


@dataclass(frozen=True)
class StockInfo:
    code: str
    name: str
    market: Market
    group: str                      # 族群，例：PCB
    subcategory: str | None         # 子分類，例：CCL；族群未列子分類時為 None
    themes: tuple[str, ...] = ()

    @property
    def industry(self) -> str:
        """產業鏈位置，格式同 watchlist.yaml：族群/子分類。"""
        return f"{self.group}/{self.subcategory}" if self.subcategory else self.group
