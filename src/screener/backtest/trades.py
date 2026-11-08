"""交易回測：照進場與出場規則模擬一個帳戶（RESEARCH_PLAN.md 4.2；PROJECT_PLAN.md 5.4、5.5）。

分成兩步：
1. find_candidates：每個進場訊號與它照出場規則的結果。只和該檔股票有關，與帳戶無關，所以每個訊號只算一次。
2. simulate_account：逐日模擬帳戶的現金、持股與檔數上限。每天收盤先賣再買，單筆金額 = 當天權益 ÷ 檔數上限。

判斷用未還原價（D8）；持有期間的市值與損益以還原權息後的價格計算，等於股利再投入該檔。
"""

import math
import random
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

import numpy as np
import pandas as pd

from screener.analysis.context import build_context
from screener.analysis.market import limit_prices
from screener.analysis.numeric import normalize
from screener.backtest.signals import StockHistory
from screener.config import CostConfig, TrendConfig
from screener.engine import Engine
from screener.models import Position, Side, StockContext, Trend

RANDOM = "random"
END_OF_DATA = "資料結束"
TRADING_DAYS_PER_YEAR = 252         # 年化波動度的換算


# --- 1. 進場訊號與出場 -------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """一個進場訊號與它照出場規則的結果，與帳戶無關。"""

    code: str
    name: str
    entry_day: date
    entry_price: float              # 進場日收盤（未還原）
    ma_gap_pct: float               # (現價 / MA − 1) × 100，nearest_ma 挑選用
    volume_ratio: float | None      # 今日量 / 昨日量，volume_ratio 挑選用；昨日量為 0 時為 None
    exit_day: date
    exit_price: float               # 出場日收盤（未還原）
    exit_reason: str                # 出場規則的名稱；資料用完還沒出場時為「資料結束」
    held_days: int                  # 該檔自己的交易日數
    delayed: bool                   # 出場條件成立當天收跌停，順延到之後第一個沒有跌停的收盤
    growth: tuple[tuple[date, float], ...]  # 進場隔天到出場日：(日期, 還原收盤 / 進場日還原收盤)


def find_candidates(history: StockHistory, engine: Engine, trend_config: TrendConfig, start: date, end: date,
                    pick_ma: int) -> list[Candidate]:
    """start～end（含）符合進場規則、且沒有收在漲停的訊號（5.5），各自找出出場（5.4）。

    與訊號回測相同，掛牌後第六個交易日之前（D38）與不在當月股票池的日子不產生訊號；進場後即使股票池
    更換，仍照出場規則賣出。
    """
    bars = history.bars
    days = list(bars.index)
    closes, volumes = bars["close"].to_numpy(), bars["volume"].to_numpy()
    adjusted = history.adjusted["close"].to_numpy()
    eligible = history.eligible()
    candidates = []
    for i in range(max(bisect_left(days, start), history.first_position, 1), bisect_right(days, end)):
        if not eligible[i]:
            continue
        ctx = build_context(history.info, bars.iloc[:i + 1], trend_config, is_tentative=False)
        if Side.LONG not in engine.entry_sides(ctx, engine.evaluate(ctx)) or _at_limit(history, i, Side.LONG):
            continue
        exit_at, reason, delayed = _find_exit(history, engine, i)
        ma = float(ctx.ma(pick_ma).iloc[-1])
        candidates.append(Candidate(
            code=history.info.code, name=history.info.name, entry_day=days[i], entry_price=float(closes[i]),
            ma_gap_pct=(float(closes[i]) / ma - 1) * 100 if not math.isnan(ma) else math.inf,
            volume_ratio=float(volumes[i] / volumes[i - 1]) if volumes[i - 1] > 0 else None,
            exit_day=days[exit_at], exit_price=float(closes[exit_at]), exit_reason=reason, held_days=exit_at - i,
            delayed=delayed,
            growth=tuple((days[j], float(adjusted[j] / adjusted[i])) for j in range(i + 1, exit_at + 1)),
        ))
    return candidates


def _find_exit(history: StockHistory, engine: Engine, entry: int) -> tuple[int, str, bool]:
    """(出場的 K 棒位置, 原因, 是否因跌停順延)。

    出場規則只用到均線與持有天數，所以不計算轉折點與趨勢。資料用完還沒出場時，以最後一根收盤結算。
    """
    bars = history.bars
    days = list(bars.index)
    position = Position(code=history.info.code, entry_date=days[entry], entry_price=float(bars["close"].iloc[entry]),
                        held_days=0)
    reason, triggered = None, None
    for j in range(entry + 1, len(bars)):
        if reason is None:
            ctx = StockContext(info=history.info, bars=bars.iloc[:j + 1], is_tentative=False, pivots=[],
                               trend=Trend.UNKNOWN)
            signals = engine.evaluate_exits(ctx, replace(position, held_days=j - entry))
            if signals:
                reason, triggered = signals[0].rule_name, j
        if reason is not None and not _at_limit(history, j, Side.SHORT):
            return j, reason, j > triggered
    return len(bars) - 1, END_OF_DATA, False


def _at_limit(history: StockHistory, i: int, side: Side) -> bool:
    """收在漲停價（多方買不到）或跌停價（賣不掉）。參考價 = 前一日收盤 × 當天的調整因子，判斷方式同 14.1。

    以「等於」判斷：官方參考價有疑點的除權息日（14.2），收盤可能超出以官方參考價算出的漲跌停價，
    例：2464 盟立 2026-09-02 收 202，以官方參考價 182.3 算出的漲停價為 200.5，實際並沒有漲停。
    """
    closes = history.bars["close"]
    up, down = limit_prices(float(closes.iloc[i - 1] * history.factors.iloc[i]))
    return normalize(closes.iloc[i]) == normalize(up if side is Side.LONG else down)


# --- 2. 帳戶 -----------------------------------------------------------------

@dataclass(frozen=True)
class AccountSettings:
    initial_capital: float
    max_positions: int
    pick: str                       # nearest_ma、volume_ratio 或 random
    cost: CostConfig
    min_commission: float


@dataclass(frozen=True)
class Trade:
    """一筆已賣出的交易。金額單位為元。"""

    code: str
    name: str
    entry_day: date
    entry_price: float
    shares: int
    buy_amount: float               # 股數 × 進場價
    buy_fee: float
    exit_day: date
    exit_price: float               # 出場日收盤（未還原）
    sell_amount: float              # 買進金額 × 還原後的漲跌：沒有除權息時等於股數 × 出場價，否則含股利
    sell_fee: float
    tax: float
    exit_reason: str
    held_days: int
    delayed: bool

    @property
    def pnl(self) -> float:
        return self.sell_amount - self.sell_fee - self.tax - self.buy_amount - self.buy_fee

    @property
    def return_pct(self) -> float:
        """扣除所有成本後，相對買進金額的報酬（%）。"""
        return self.pnl / self.buy_amount * 100


@dataclass(frozen=True)
class AccountDay:
    """每個交易日收盤（賣出與買進之後）的帳戶狀態。"""

    day: date
    cash: float
    holdings_value: float
    positions: int
    candidates: int                 # 當天符合進場的訊號（不含持有中與當天賣出的股票）
    bought: int
    rejected_full: int              # 檔數已滿而放棄
    skipped_cash: int               # 現金連 1 股都買不起而跳過

    @property
    def equity(self) -> float:
        return self.cash + self.holdings_value


@dataclass(frozen=True)
class AccountResult:
    settings: AccountSettings
    trades: tuple[Trade, ...]
    days: tuple[AccountDay, ...]


@dataclass
class _Holding:
    candidate: Candidate
    shares: int
    buy_amount: float
    buy_fee: float
    growth: dict[date, float]
    last_growth: float = 1.0

    def value(self, day: date) -> float:
        """收盤市值；該檔當天沒有交易時沿用前一個收盤。"""
        self.last_growth = self.growth.get(day, self.last_growth)
        return self.buy_amount * self.last_growth


def simulate_account(candidates: Sequence[Candidate], calendar: Sequence[date], end: date,
                     settings: AccountSettings, rng: random.Random | None = None) -> AccountResult:
    """從 calendar 第一天開始模擬；end 之後不再買進，持有中的部位照規則賣完為止。

    每天收盤：先賣出今天出場的部位，再以「當天權益 ÷ 檔數上限」為單筆金額買進（不超過可用現金），
    買零股、股數無條件捨去。持有中與當天賣出的股票不再買進；額滿時放棄，不排隊。
    """
    by_day: dict[date, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_day[candidate.entry_day].append(candidate)
    cash = settings.initial_capital
    holdings: dict[str, _Holding] = {}
    trades: list[Trade] = []
    days: list[AccountDay] = []
    for day in calendar:
        if day > end and not holdings:
            break
        sold: set[str] = set()
        for code, holding in list(holdings.items()):
            if holding.candidate.exit_day != day:
                continue
            trade = _sell(holding, settings)
            cash += trade.sell_amount - trade.sell_fee - trade.tax
            trades.append(trade)
            sold.add(code)
            del holdings[code]

        todays = [c for c in by_day.get(day, []) if day <= end and c.code not in holdings and c.code not in sold]
        bought = rejected = skipped = 0
        equity = cash + sum(h.value(day) for h in holdings.values())
        target = equity / settings.max_positions
        for candidate in order_candidates(todays, settings.pick, rng):
            if len(holdings) >= settings.max_positions:
                rejected += 1
                continue
            shares = _affordable_shares(min(target, cash), candidate.entry_price, settings)
            if shares < 1:
                skipped += 1
                continue
            amount = shares * candidate.entry_price
            fee = _commission(amount, settings)
            cash -= amount + fee
            holdings[candidate.code] = _Holding(candidate, shares, amount, fee, dict(candidate.growth))
            bought += 1

        days.append(AccountDay(day=day, cash=cash, holdings_value=sum(h.value(day) for h in holdings.values()),
                               positions=len(holdings), candidates=len(todays), bought=bought,
                               rejected_full=rejected, skipped_cash=skipped))
    return AccountResult(settings=settings, trades=tuple(trades), days=tuple(days))


def order_candidates(candidates: Sequence[Candidate], pick: str, rng: random.Random | None) -> list[Candidate]:
    """訊號超過空位時的優先順序；同分時依代號。"""
    ordered = sorted(candidates, key=lambda c: c.code)
    if pick == "nearest_ma":
        return sorted(ordered, key=lambda c: c.ma_gap_pct)
    if pick == "volume_ratio":
        return sorted(ordered, key=lambda c: -(c.volume_ratio if c.volume_ratio is not None else -math.inf))
    if pick == RANDOM:
        if rng is None:
            raise ValueError("隨機挑選需要 rng")
        rng.shuffle(ordered)
        return ordered
    raise ValueError(f"未知的挑選方式：{pick}")


def _affordable_shares(budget: float, price: float, settings: AccountSettings) -> int:
    """金額加手續費不超過 budget 的最大股數。"""
    shares = math.floor(budget / price)
    while shares > 0 and shares * price + _commission(shares * price, settings) > budget:
        shares -= 1
    return shares


def _commission(amount: float, settings: AccountSettings) -> float:
    cost = settings.cost
    return max(amount * cost.commission_pct / 100 * cost.commission_discount, settings.min_commission)


def _sell(holding: _Holding, settings: AccountSettings) -> Trade:
    c = holding.candidate
    amount = holding.value(c.exit_day)
    return Trade(code=c.code, name=c.name, entry_day=c.entry_day, entry_price=c.entry_price, shares=holding.shares,
                 buy_amount=holding.buy_amount, buy_fee=holding.buy_fee, exit_day=c.exit_day, exit_price=c.exit_price,
                 sell_amount=amount, sell_fee=_commission(amount, settings), tax=amount * settings.cost.tax_pct / 100,
                 exit_reason=c.exit_reason, held_days=c.held_days, delayed=c.delayed)


# --- 3. 績效 -----------------------------------------------------------------

def equity_series(result: AccountResult) -> pd.Series:
    return pd.Series([d.equity for d in result.days], index=pd.Index([d.day for d in result.days], name="date"))


def benchmark_series(histories: Sequence[StockHistory], calendar: Sequence[date], initial_capital: float) -> pd.Series:
    """全清單每天等權的報酬（還原權息、不扣成本）累積成的帳戶，從 calendar 第一天收盤開始。
    與訊號回測的基準相同，掛牌後第六個交易日之前不列入；全市場只列入當天在股票池內的股票。"""
    returns = [_benchmark_returns(h) for h in histories]
    daily = pd.concat(returns, axis=1).mean(axis=1).reindex(calendar).fillna(0.0)
    daily.iloc[0] = 0.0
    return initial_capital * (1 + daily).cumprod()


def _benchmark_returns(history: StockHistory) -> pd.Series:
    """每天的報酬；前一天要已過掛牌後前五日（與原本 iloc[first_position + 1:] 相同），且當天在股票池內。"""
    keep = history.eligible()
    keep[:history.first_position + 1] = False
    return history.adjusted["close"].pct_change()[keep]


def drawdown(equity: pd.Series) -> pd.Series:
    """相對之前最高點的回落（%），0 為創新高。"""
    return (equity / equity.cummax() - 1) * 100


def account_metrics(result: AccountResult, benchmark: pd.Series) -> dict[str, float]:
    """帳戶、交易與資金使用的統計。報酬與比例都以 % 表示。"""
    equity = equity_series(result)
    initial = result.settings.initial_capital
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    dd = drawdown(equity)
    trough = dd.idxmin()
    peak = equity.loc[:trough].idxmax()
    recovered = equity.loc[trough:][equity.loc[trough:] >= equity.loc[peak]]
    recovery_day = recovered.index[0] if len(recovered) else None
    positions = list(equity.index)
    pnls = [t.pnl for t in sorted(result.trades, key=lambda t: (t.exit_day, t.entry_day, t.code))]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p <= 0]
    bench = benchmark.reindex(equity.index).ffill()
    usage = [d.holdings_value / d.equity for d in result.days if d.equity > 0]
    return {
        "total_return": (equity.iloc[-1] / initial - 1) * 100,
        "cagr": _annualized(equity.iloc[-1] / initial, years),
        "max_drawdown": float(dd.min()),
        "drawdown_days": positions.index(recovery_day if recovery_day is not None else positions[-1])
                         - positions.index(peak),
        "drawdown_recovered": recovery_day is not None,
        "volatility": float(equity.pct_change().std() * math.sqrt(TRADING_DAYS_PER_YEAR) * 100),
        "trades": len(pnls),
        "win_rate": len(wins) / len(pnls) * 100 if pnls else np.nan,
        "payoff": (np.mean(wins) / abs(np.mean(losses))) if wins and losses and np.mean(losses) != 0 else np.nan,
        "avg_held_days": float(np.mean([t.held_days for t in result.trades])) if result.trades else np.nan,
        "max_losing_streak": _longest_streak(pnls),
        "avg_positions": float(np.mean([d.positions for d in result.days])),
        "capital_usage": float(np.mean(usage) * 100) if usage else np.nan,
        "flat_days": float(np.mean([d.positions == 0 for d in result.days]) * 100),
        "rejected_full": sum(d.rejected_full for d in result.days),
        "skipped_cash": sum(d.skipped_cash for d in result.days),
        "costs": sum(t.buy_fee + t.sell_fee + t.tax for t in result.trades),
        "final_equity": float(equity.iloc[-1]),
        "benchmark_return": (bench.iloc[-1] / bench.iloc[0] - 1) * 100,
        "benchmark_cagr": _annualized(bench.iloc[-1] / bench.iloc[0], years),
        "benchmark_max_drawdown": float(drawdown(bench).min()),
    }


def _annualized(growth: float, years: float) -> float:
    """年化報酬（%）。權益歸零或為負（虧光後最低手續費仍照收）時為 −100%。"""
    return (max(growth, 0.0) ** (1 / years) - 1) * 100


def _longest_streak(pnls: Sequence[float]) -> int:
    longest = current = 0
    for pnl in pnls:
        current = current + 1 if pnl <= 0 else 0
        longest = max(longest, current)
    return longest
