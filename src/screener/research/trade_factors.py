"""交易回測的因子挑選與訊號分組（RESEARCH_PLAN.md 4.7、R40）。

因子值與 M9 相同（research.yaml 的 factors.definitions），由 market.db 建立面板計算，取進場日收盤時的值。
面板含曾入選股票池的股票與觀察清單中從沒入選的股票（R42）；加進來的股票只用來算自己的因子值。

訊號分組：每天依當天股票池內有因子值的股票分組（同 M9 的 factor_eval.quantile_labels：平均排名 ÷ 檔數 × 組數
無條件進位），訊號依自己的因子值落在哪一組歸組；訊號的股票不在股票池時只用來定位，不改變股票池的分組。
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date

import numpy as np
import pandas as pd

from screener.backtest.signals import StockHistory, forward_returns
from screener.backtest.trades import Candidate
from screener.config import ConfigError, MarketConfig, ResearchConfig, TrendConfig
from screener.research.factors import FactorContext, build_factors
from screener.research.factors.panel import build_panel
from screener.research.market.history import value_table
from screener.research.market.store import SqliteMarketStore

NO_VALUE = 0                        # 沒有因子值的一組
NOT_GROUPED = -1                    # 當天股票池內有值的股票少於 min_stocks，不分組


@dataclass(frozen=True)
class FactorTables:
    values: Mapping[str, pd.DataFrame]  # 因子 id → 日期 × 代號
    eligible: pd.DataFrame              # 當天在股票池內（M9 的 Panel.eligible），分組用
    quantiles: int
    min_stocks: int
    _pools: dict[tuple[str, date], np.ndarray | None] = field(default_factory=dict, compare=False, repr=False)

    @property
    def last_day(self) -> date:
        return self.eligible.index[-1]

    def value(self, factor: str, code: str, day: date) -> float:
        table = self.values[factor]
        if day not in table.index or code not in table.columns:
            return math.nan
        value = table.at[day, code]
        return float(value) if pd.notna(value) else math.nan

    def group(self, factor: str, code: str, day: date) -> int:
        """1～quantiles（quantiles 為因子值最大的一組）；NO_VALUE：沒有因子值；NOT_GROUPED：當天不分組。"""
        pool = self._pool(factor, day)
        if pool is None:
            return NOT_GROUPED
        value = self.value(factor, code, day)
        if math.isnan(value):
            return NO_VALUE
        less = int(np.searchsorted(pool, value, side="left"))
        equal = int(np.searchsorted(pool, value, side="right")) - less
        rank = less + (equal + 1) / 2           # 平均排名；在股票池內時與 M9 的 quantile_labels 相同
        return int(np.clip(np.ceil(rank / len(pool) * self.quantiles), 1, self.quantiles))

    def _pool(self, factor: str, day: date) -> np.ndarray | None:
        """當天股票池內有值的因子值（由小到大）；少於 min_stocks 或沒有這一天時為 None。"""
        key = (factor, day)
        if key not in self._pools:
            table = self.values[factor]
            pool = None
            if day in table.index:
                row = table.loc[day].to_numpy(dtype=float)
                selected = np.sort(row[self.eligible.loc[day].to_numpy(dtype=bool) & ~np.isnan(row)])
                pool = selected if len(selected) >= self.min_stocks else None
            self._pools[key] = pool
        return self._pools[key]


def check_factors(research: ResearchConfig, factor_ids: Sequence[str]) -> None:
    """factor_picks 必須是 research.yaml 的連續因子（類別因子沒有高低之分）。"""
    factors = {factor.id: factor for factor in build_factors(research.factors.definitions)}
    for factor_id in factor_ids:
        if factor_id not in factors:
            raise ConfigError(f"backtest.yaml > trade > factor_picks：{factor_id} 不是 research.yaml 的因子 id")
        if factors[factor_id].is_categorical:
            raise ConfigError(f"backtest.yaml > trade > factor_picks：{factor_id} 是類別因子，不能用來挑選")


def build_factor_tables(histories: Sequence[StockHistory], market: MarketConfig, research: ResearchConfig,
                        trend: TrendConfig, factor_ids: Sequence[str]) -> FactorTables:
    """histories：market.db 的歷史資料（load_market 加上 extra_codes）。面板算完因子就釋放，只留因子值與股票池。"""
    check_factors(research, factor_ids)
    factors = {factor.id: factor for factor in build_factors(research.factors.definitions)}
    with SqliteMarketStore(market.db_path) as store:
        values = value_table(store)
        revenue, valuations, shares = store.revenue_frame(), store.valuation_frame(), store.shares_frame()
    panel = build_panel(histories, values)
    context = FactorContext(panel=panel, trend=trend, market=market, revenue=revenue, valuations=valuations,
                            shares=shares)
    tables = {factor_id: factors[factor_id].compute(context) for factor_id in factor_ids}
    return FactorTables(tables, panel.eligible, research.factors.quantiles, research.factors.min_stocks)


def attach_factors(candidates: Sequence[Candidate], tables: FactorTables) -> list[Candidate]:
    """每個候選加上進場日收盤時的因子值（沒有值為 NaN），供 order_candidates 的因子挑選。"""
    return [replace(c, factors={factor: tables.value(factor, c.code, c.entry_day) for factor in tables.values})
            for c in candidates]


# --- 訊號分組 ---------------------------------------------------------------

def signal_returns(candidates: Sequence[Candidate], histories: Sequence[StockHistory], horizon: int,
                   round_trip_pct: float) -> pd.DataFrame:
    """每個候選一列：持有 horizon 日的報酬與超額（算法同訊號回測：該檔自己的 K 棒，基準為同一天 histories 的等權平均，
    只列入當天在股票池內的股票），以及照出場規則的報酬（還原權息，扣來回成本）。單位為 %。"""
    by_code = {h.info.code: h for h in histories}
    baseline = pd.concat([forward_returns(h, [horizon])[horizon] for h in histories], axis=1).mean(axis=1)
    rows = []
    for c in candidates:
        history = by_code[c.code]
        close = history.adjusted["close"].to_numpy()
        i = history.bars.index.get_loc(c.entry_day)
        ret = (close[i + horizon] / close[i] - 1) * 100 if i + horizon < len(close) else math.nan
        base = baseline.get(c.entry_day, math.nan)
        trade = (c.growth[-1][1] - 1) * 100 - round_trip_pct if c.growth else math.nan
        rows.append({"code": c.code, "entry_day": c.entry_day, "ret": ret, "excess": ret - base, "trade": trade})
    return pd.DataFrame(rows, columns=["code", "entry_day", "ret", "excess", "trade"])


def signal_groups(candidates: Sequence[Candidate], returns: pd.DataFrame, tables: FactorTables,
                  factor_ids: Sequence[str]) -> pd.DataFrame:
    """因子 × 組：次數、持有 N 日的平均、中位數、超額、勝率，照出場規則的平均、中位數、勝率；另列最高組減最低組。"""
    rows = []
    for factor in factor_ids:
        groups = pd.Series([tables.group(factor, c.code, c.entry_day) for c in candidates], index=returns.index)
        for group in [*range(1, tables.quantiles + 1), NO_VALUE, NOT_GROUPED]:
            part = returns[groups == group]
            if group in (NO_VALUE, NOT_GROUPED) and part.empty:
                continue
            rows.append({"factor": factor, "group": group, **_stats(part)})
        means = {row["group"]: row for row in rows if row["factor"] == factor}
        top, bottom = means.get(tables.quantiles), means.get(1)
        if top is not None and bottom is not None:
            rows.append({"factor": factor, "group": "top_minus_bottom", "count": np.nan,
                         **{key: top[key] - bottom[key] for key in ("ret_mean", "excess_mean", "trade_mean")}})
    return pd.DataFrame(rows)


def _stats(part: pd.DataFrame) -> dict[str, float]:
    ret, trade = part["ret"].dropna(), part["trade"].dropna()
    return {"count": len(part), "ret_mean": ret.mean(), "ret_median": ret.median(),
            "excess_mean": part["excess"].dropna().mean(), "win_rate": (ret > 0).mean() * 100 if len(ret) else np.nan,
            "trade_mean": trade.mean(), "trade_median": trade.median(),
            "trade_win_rate": (trade > 0).mean() * 100 if len(trade) else np.nan}
