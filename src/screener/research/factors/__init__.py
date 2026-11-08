"""單因子研究的因子定義（RESEARCH_PLAN.md 4.4、4.5）。匯入底下的模組，讓因子註冊到註冊表。"""

from screener.research.factors import fundamental, price, rule_scores, trend  # noqa: F401
from screener.research.factors.base import Factor, FactorContext, build_factors, register_factor

__all__ = ["Factor", "FactorContext", "build_factors", "register_factor"]
