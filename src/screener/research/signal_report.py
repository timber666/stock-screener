"""訊號成績單的計算（RESEARCH_PLAN.md 4.1）。

讀取 scan 記錄的執行紀錄、訊號與報價快照，每天只用第一次推播的那一次（D45），回答兩個問題：

1. 13:10 推播的訊號，到收盤還成立嗎？以收盤 K 棒與目前的規則檔重算訊號（與 scan --replay 相同的
   build_context 與 Engine），同一天、同一檔、同一規則比對。13:10 沒有評估的股票，收盤才觸發的訊號
   另外計數，不算「收盤才出現」（R4）。
2. 推播的訊號之後表現如何？以 13:10 現價進場（R2，替代價也照用），計算到之後第 N 個交易日（該檔自己的
   日 K）收盤的方向報酬，空方取負號。基準為同一天有快照的股票各自以 13:10 現價進場、相同持有期間的
   等權平均；超額報酬 = 方向報酬 − 同方向的基準（同 M6）。

除權息調整：持有期間內某天快照的參考價與資料庫前一日收盤不同時，以「參考價 / 前一日收盤」為當天的
調整因子，這就是證交所的除權息（或減資）參考價；該天沒有快照時改用資料庫的事件資料（D37）；事件資料
也沒有涵蓋到那天時（events 之後才發生），該訊號標註「未調整」。進場當天不需要調整：13:10 現價已經是
除權息後的價格。

報酬只用到期間最後一天（含）以前的收盤，同一個期間重跑的結果相同。只讀取資料庫，不寫入
（PROJECT_PLAN.md 10.4 規則 3）。
"""

import logging
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol

import numpy as np
import pandas as pd

from screener.analysis.adjustment import event_factors
from screener.analysis.context import bars_to_frame, build_context
from screener.analysis.market import limit_prices
from screener.analysis.numeric import normalize
from screener.backtest.stats import DEDUPED, EXCLUDE, INCLUDE, RAW_PRICE, SUMMARY_COLUMNS, VALIDATE
from screener.config import TrendConfig
from screener.data.base import EventCoverage, EventStore, PriceStore, ScanStore
from screener.engine import Engine
from screener.jobs import JobFailed
from screener.models import Market, PriceSubstitute, Quote, ScanRun, Side, Signal, SignalRecord, StockInfo, Trend

logger = logging.getLogger(__name__)

FRIDAY = 4                              # date.weekday() 的週五
CONSISTENT, VANISHED, APPEARED = "一致", "收盤消失", "收盤才出現"
NOT_EVALUATED = "13:10 未評估"           # 13:10 沒有評估、收盤才觸發：不列入一致率（R4）
NO_CLOSE_BAR = "沒有當日日 K"            # 資料庫沒有該檔當天的日 K，無法重算收盤訊號
SNAPSHOT, EVENTS = "快照參考價", "事件資料"
WEEK = "week"                           # 「本週」：持有到期間最後一個交易日收盤


class Store(PriceStore, EventStore, ScanStore, Protocol):
    """成績單讀取日 K、事件資料與盤中掃描的紀錄。"""


# --- 期間與推播紀錄 ---------------------------------------------------------

@dataclass(frozen=True)
class Period:
    start: date                         # 週一
    end: date                           # 期間的最後一天


def report_period(end: date | None, today: date) -> Period:
    """end 所在那一週的週一到 end。未指定 end 時為上一週：今天之前最近的週五（今天是週五時為上週五）。"""
    if end is None:
        end = today - timedelta(days=(today.weekday() - FRIDAY) % 7 or 7)
    return Period(start=end - timedelta(days=end.weekday()), end=end)


@dataclass(frozen=True)
class ScanDay:
    """某一天第一次推播的掃描。"""

    run: ScanRun
    signals: tuple[SignalRecord, ...]
    snapshot: Mapping[str, Quote]

    @property
    def day(self) -> date:
        return self.run.day

    def evaluated(self, code: str) -> bool:
        """13:10 是否有評估這檔：當天的報價、今日有成交且拿得到現價（與合成暫定 K 棒的條件相同，8.2）。"""
        quote = self.snapshot.get(code)
        return (quote is not None and quote.date == self.day
                and None not in (quote.open, quote.high, quote.low, quote.volume)
                and quote.current_price()[0] is not None)


def first_scan_days(store: ScanStore, start: date, end: date) -> list[ScanDay]:
    """start～end 每天第一次推播的掃描，依日期排列（D45）。"""
    first: dict[date, ScanRun] = {}
    for run in store.get_scan_runs(start, end):
        first.setdefault(run.day, run)
    return [ScanDay(run, tuple(store.get_signal_records(run.id)), store.get_snapshot(run.id))
            for run in first.values()]


# --- 價格與除權息調整 -------------------------------------------------------

@dataclass(frozen=True)
class Adjustment:
    day: date
    factor: float
    source: str                         # SNAPSHOT 或 EVENTS


@dataclass(frozen=True)
class Holding:
    """以 entry 進場、持有到 exit_day 收盤的報酬（%，不分方向）。"""

    exit_day: date
    ret: float
    adjustments: tuple[Adjustment, ...]
    unadjusted: tuple[date, ...]        # 沒有快照、事件資料也沒有涵蓋的日子：可能有除權息而沒有調整


class PriceBook:
    """資料庫的日 K 與事件資料，加上快照的參考價，用來計算持有報酬與除權息調整。"""

    def __init__(self, bars: Mapping[str, pd.DataFrame], factors: Mapping[str, pd.Series],
                 coverage: Mapping[str, EventCoverage | None],
                 references: Mapping[date, Mapping[str, Quote]]) -> None:
        self._bars = dict(bars)
        self._factors = factors
        self._coverage = coverage
        self._references = references   # 交易日 → 當天第一次推播的快照
        self._days = {code: list(frame.index) for code, frame in self._bars.items()}
        self._positions = {code: {day: i for i, day in enumerate(days)} for code, days in self._days.items()}

    @classmethod
    def load(cls, store: Store, codes: Iterable[str], scan_days: Sequence[ScanDay]) -> "PriceBook":
        """資料庫沒有日 K 的代號略過。"""
        bars, factors, coverage = {}, {}, {}
        for code in codes:
            daily = store.get_bars(code)
            if not daily:
                continue
            frame = bars_to_frame(daily)
            bars[code] = frame
            factors[code] = event_factors(frame, store.get_dividends(code), store.get_actions(code),
                                          store.get_dividend_results(code))
            coverage[code] = store.event_coverage(code)
        return cls(bars, factors, coverage, {scan_day.day: scan_day.snapshot for scan_day in scan_days})

    def codes(self) -> list[str]:
        return list(self._bars)

    def has_bar(self, code: str, day: date) -> bool:
        return day in self._positions.get(code, {})

    def bars_until(self, code: str, day: date) -> pd.DataFrame:
        """該檔到 day（含）為止的日 K；day 要有日 K。"""
        return self._bars[code].iloc[:self._positions[code][day] + 1]

    def close(self, code: str, day: date) -> float | None:
        i = self._positions.get(code, {}).get(day)
        return None if i is None else float(self._bars[code]["close"].iloc[i])

    def trading_days(self, start: date, end: date) -> list[date]:
        """start～end 之間，任何一檔有日 K 的日子。"""
        return sorted({day for days in self._days.values() for day in days if start <= day <= end})

    def nth_day(self, code: str, day: date, n: int, last: date) -> date | None:
        """day 之後第 n 個交易日（該檔自己的日 K）；日 K 不足或晚於 last 時為 None。"""
        days = self._days[code]
        i = self._positions[code][day] + n
        return days[i] if i < len(days) and days[i] <= last else None

    def last_day(self, code: str, day: date, last: date) -> date:
        """day（含）到 last（含）之間，該檔最後一根日 K 的日期。"""
        days = self._days[code]
        return days[max(bisect_right(days, last) - 1, self._positions[code][day])]

    def holding(self, code: str, day: date, entry: float, exit_day: date) -> Holding:
        """day 以 entry 進場，持有到 exit_day 收盤。之間每一天的調整因子相乘，exit_day 的收盤除以它，
        換算成與進場價可以比較的價格。"""
        days = self._days[code]
        product = 1.0
        adjustments: list[Adjustment] = []
        unadjusted: list[date] = []
        for i in range(self._positions[code][day] + 1, self._positions[code][exit_day] + 1):
            factor = self.factor(code, days[i])
            if factor is None:
                unadjusted.append(days[i])
            elif factor.factor != 1:
                adjustments.append(factor)
                product *= factor.factor
        close = float(self._bars[code]["close"].iloc[self._positions[code][exit_day]])
        return Holding(exit_day, (close / (entry * product) - 1) * 100, tuple(adjustments), tuple(unadjusted))

    def factor(self, code: str, day: date) -> Adjustment | None:
        """day 當天的調整因子；沒有快照、事件資料也沒有涵蓋到這天時為 None（無法判斷有沒有除權息）。"""
        position = self._positions[code][day]
        quote = self._references.get(day, {}).get(code)
        if quote is not None and quote.date == day and quote.prev_close is not None and position > 0:
            previous = float(self._bars[code]["close"].iloc[position - 1])
            if normalize(previous) == normalize(quote.prev_close):
                return Adjustment(day, 1.0, SNAPSHOT)
            return Adjustment(day, quote.prev_close / previous, SNAPSHOT)
        coverage = self._coverage.get(code)
        if coverage is not None and coverage.start <= day <= coverage.end:
            return Adjustment(day, float(self._factors[code].iloc[position]), EVENTS)
        return None


# --- 成績單 -----------------------------------------------------------------

@dataclass(frozen=True)
class SignalReport:
    period: Period
    trading_days: tuple[date, ...]      # 期間內的交易日（資料庫有日 K 或有推播的日子）
    scan_days: tuple[ScanDay, ...]      # 期間內每天第一次推播
    first_day: date | None              # M7 上線以來第一個有推播的日子
    missing_days: tuple[date, ...]      # 期間內沒有推播紀錄的交易日
    changed_rule_days: tuple[date, ...]  # 推播時的規則檔與目前不同的日子
    horizons: tuple[int, ...]
    rule_names: dict[str, str]          # 規則 id → 名稱，依規則檔順序
    rows: pd.DataFrame                  # 每個訊號一列，欄位見 _row

    @property
    def last_trading_day(self) -> date | None:
        return self.trading_days[-1] if self.trading_days else None

    @property
    def week_rows(self) -> pd.DataFrame:
        """期間內的訊號，含收盤才出現的訊號（明細 CSV 的內容）。"""
        return self.rows[self.rows["in_week"]]

    @property
    def pushed_rows(self) -> pd.DataFrame:
        """M7 上線以來推播的訊號（不含收盤才出現的訊號）。"""
        return self.rows[self.rows["pushed"]]


def build_report(store: Store, stocks: Sequence[StockInfo], *, engine: Engine, trend_config: TrendConfig,
                 fingerprint: str, period: Period, horizons: Sequence[int]) -> SignalReport:
    """fingerprint：目前規則檔的指紋；推播時的指紋不同時標註「期間內規則有變更」。"""
    scan_days = first_scan_days(store, date.min, period.end)
    week = [scan_day for scan_day in scan_days if scan_day.day >= period.start]
    infos = {stock.code: stock for stock in stocks}
    snapshot_codes = {code for scan_day in scan_days for code in scan_day.snapshot}
    book = PriceBook.load(store, [*infos, *sorted(snapshot_codes - set(infos))], scan_days)
    for scan_day in week:
        if not any(book.has_bar(code, scan_day.day) for code in book.codes()):
            raise JobFailed(f"資料庫沒有 {scan_day.day} 的日 K，無法重算收盤訊號與報酬，請先執行 update")

    trading_days = sorted(set(book.trading_days(period.start, period.end)) | {d.day for d in week})
    calculator = _Calculator(book, period.end, trading_days[-1] if trading_days else period.end, horizons)
    rows: list[dict] = []
    for scan_day in scan_days:
        in_week = scan_day.day >= period.start
        closing = _closing_signals(book, infos, engine, trend_config, scan_day.day) if in_week else {}
        rule_changed = scan_day.run.rules_fingerprint != fingerprint
        pushed = {(s.code, s.rule_id) for s in scan_day.signals}
        for record in scan_day.signals:
            if not in_week:
                status = ""
            elif not book.has_bar(record.code, scan_day.day):
                status = NO_CLOSE_BAR
            else:
                status = CONSISTENT if (record.code, record.rule_id) in closing else VANISHED
            rows.append(_row(scan_day, record.code, infos, record.rule_id, record.rule_name, record.side,
                             record.trend, status, in_week=in_week, rule_changed=rule_changed,
                             calculator=calculator, price=record.price, substitute=record.price_substitute))
        for (code, rule_id), (signal, trend) in closing.items():
            if (code, rule_id) not in pushed:
                status = APPEARED if scan_day.evaluated(code) else NOT_EVALUATED
                rows.append(_row(scan_day, code, infos, rule_id, signal.rule_name, signal.side, trend, status,
                                 in_week=True, rule_changed=rule_changed, calculator=calculator))

    names = {rule.id: rule.name for rule in engine.trigger_rules}
    for row in rows:
        names.setdefault(row["rule_id"], row["rule_name"])     # 已從規則檔移除的規則排在最後
    order = {rule_id: i for i, rule_id in enumerate(names)}
    frame = pd.DataFrame(rows, columns=_columns(horizons))
    if len(frame):
        frame = (frame.assign(_order=frame["rule_id"].map(order))
                 .sort_values(["date", "pushed", "_order", "code"], ascending=[True, False, True, True])
                 .drop(columns="_order").reset_index(drop=True))
    frame = frame.astype({"in_week": bool, "pushed": bool, "unadjusted": bool, "rule_changed": bool})
    report = SignalReport(
        period=period, trading_days=tuple(trading_days), scan_days=tuple(week),
        first_day=scan_days[0].day if scan_days else None,
        missing_days=tuple(day for day in trading_days if day not in {d.day for d in week}),
        changed_rule_days=tuple(d.day for d in week if d.run.rules_fingerprint != fingerprint),
        horizons=tuple(horizons), rule_names=names, rows=frame,
    )
    logger.info("訊號成績單 %s ~ %s：推播 %d 天、交易日 %d 天，期間內訊號 %d 個，M7 上線以來推播的訊號 %d 個",
                period.start, period.end, len(week), len(trading_days),
                int(frame["pushed"][frame["in_week"]].sum()) if len(frame) else 0, int(frame["pushed"].sum()))
    return report


def _closing_signals(book: PriceBook, infos: Mapping[str, StockInfo], engine: Engine, trend_config: TrendConfig,
                     day: date) -> dict[tuple[str, str], tuple[Signal, Trend]]:
    """以當天的收盤 K 棒重算訊號（與 scan --replay 相同），key 為 (代號, 規則 id)。當天沒有日 K 的股票不評估。"""
    signals = {}
    for code in book.codes():
        if not book.has_bar(code, day):
            continue
        info = infos.get(code) or StockInfo(code, code, Market.TSE, "", None)    # 已移出觀察清單；規則用不到
        ctx = build_context(info, book.bars_until(code, day), trend_config, is_tentative=False)
        for signal in engine.evaluate(ctx):
            signals[(code, signal.rule_id)] = (signal, ctx.trend)
    return signals


class _Calculator:
    """訊號與基準的持有報酬。基準依（日期、持有期間）計算一次後快取。"""

    def __init__(self, book: PriceBook, end: date, last_trading_day: date, horizons: Sequence[int]) -> None:
        self.book = book
        self.end = end
        self.last_trading_day = last_trading_day
        self.horizons = horizons
        self._baselines: dict[tuple[date, int | str], float] = {}

    def exit_day(self, code: str, day: date, key: int | str) -> date | None:
        if not self.book.has_bar(code, day):
            return None
        if key == WEEK:
            return self.book.last_day(code, day, self.last_trading_day)
        return self.book.nth_day(code, day, key, self.end)

    def holding(self, code: str, day: date, entry: float, key: int | str) -> Holding | None:
        exit_day = self.exit_day(code, day, key)
        return None if exit_day is None else self.book.holding(code, day, entry, exit_day)

    def baseline(self, scan_day: ScanDay, key: int | str) -> float:
        """同一天有快照的股票，各自以 13:10 現價進場、相同持有期間的報酬（%，不分方向）的等權平均。"""
        cache_key = (scan_day.day, key)
        if cache_key not in self._baselines:
            returns = []
            for code, quote in scan_day.snapshot.items():
                price = quote.current_price()[0]
                if quote.date != scan_day.day or price is None:
                    continue
                if (holding := self.holding(code, scan_day.day, price, key)) is not None:
                    returns.append(holding.ret)
            self._baselines[cache_key] = float(np.mean(returns)) if returns else np.nan
        return self._baselines[cache_key]


def _columns(horizons: Sequence[int]) -> list[str]:
    return ["date", "code", "name", "rule_id", "rule_name", "side", "trend", "status", "in_week", "pushed",
            "price", "substitute", "limit_locked", "close", "ret_close", "ret_week", "base_week", "excess_week",
            *(f"{key}_{n}" for n in horizons for key in ("ret", "base", "excess")),
            "adjustment", "unadjusted", "rule_changed"]


def _row(scan_day: ScanDay, code: str, infos: Mapping[str, StockInfo], rule_id: str, rule_name: str, side: Side,
         trend: Trend, status: str, *, in_week: bool, rule_changed: bool, calculator: _Calculator,
         price: float | None = None, substitute: PriceSubstitute | None = None) -> dict:
    """一個訊號的明細。price 為 None 時是收盤才出現的訊號：沒有 13:10 的進場價，不計算報酬。"""
    day = scan_day.day
    sign = 1 if side is Side.LONG else -1
    close = calculator.book.close(code, day)
    row = {"date": day, "code": code, "name": infos[code].name if code in infos else code, "rule_id": rule_id,
           "rule_name": rule_name, "side": side.value, "trend": trend.label, "status": status, "in_week": in_week,
           "pushed": price is not None, "price": price if price is not None else np.nan,
           "substitute": substitute.label if substitute is not None else "",
           "limit_locked": _limit_locked(scan_day.snapshot.get(code), price, side),
           "close": close if close is not None else np.nan, "adjustment": "", "unadjusted": False,
           "rule_changed": rule_changed}
    row["ret_close"] = sign * (close / price - 1) * 100 if price is not None and close is not None else np.nan
    adjustments: dict[date, Adjustment] = {}
    unadjusted: set[date] = set()
    for key in [WEEK, *calculator.horizons]:
        computed = price is not None and (in_week or key != WEEK)     # 「本週」只算期間內的訊號
        holding = calculator.holding(code, day, price, key) if computed else None
        ret = sign * holding.ret if holding is not None else np.nan
        base = sign * calculator.baseline(scan_day, key) if holding is not None else np.nan
        row[f"ret_{key}"], row[f"base_{key}"], row[f"excess_{key}"] = ret, base, ret - base
        if holding is not None:
            adjustments.update({a.day: a for a in holding.adjustments})
            unadjusted.update(holding.unadjusted)
    notes = [f"{a.day:%m/%d} {a.source} ×{a.factor:.4f}" for _, a in sorted(adjustments.items())]
    if unadjusted:
        notes.append(f"未調整：{'、'.join(f'{d:%m/%d}' for d in sorted(unadjusted))} 沒有快照與事件資料")
    row["adjustment"], row["unadjusted"] = "；".join(notes), bool(unadjusted)
    return row


def _limit_locked(quote: Quote | None, price: float | None, side: Side) -> bool | None:
    """13:10 多方訊號在漲停價、空方訊號在跌停價（R5，判斷方式同 M6：以參考價計算漲跌停價）。
    沒有快照的參考價時為 None。"""
    if price is None or quote is None or quote.prev_close is None:
        return None
    up, down = limit_prices(quote.prev_close)
    return normalize(price) == normalize(up if side is Side.LONG else down)


# --- 統計 -------------------------------------------------------------------

def consistency(report: SignalReport) -> pd.DataFrame:
    """期間內各規則的一致、收盤消失、收盤才出現的次數與一致率（%）= 一致 /（一致 + 收盤消失）；
    索引為規則 id，依規則檔順序，只列出有訊號的規則。"""
    rows = report.week_rows
    table = pd.DataFrame({status: rows[rows["status"] == status].groupby("rule_id").size()
                          for status in (CONSISTENT, VANISHED, APPEARED)}).fillna(0).astype(int)
    table = table.reindex([r for r in report.rule_names if r in table.index])
    judged = table[CONSISTENT] + table[VANISHED]
    table["rate"] = (table[CONSISTENT] / judged.where(judged > 0)) * 100
    return table


def performance(rows: pd.DataFrame, key: int | str, rule_order: Sequence[str]) -> pd.DataFrame:
    """各規則持有 key（天數或 WEEK）的次數、平均報酬、勝率（方向報酬 > 0 的比例，%）與平均超額報酬；
    索引為規則 id，只列出有完成持有的規則。"""
    done = rows[rows["pushed"] & rows[f"ret_{key}"].notna()]
    grouped = done.groupby("rule_id")
    table = pd.DataFrame({"count": grouped.size(), "mean": grouped[f"ret_{key}"].mean(),
                          "win_rate": grouped[f"ret_{key}"].apply(lambda r: (r > 0).mean() * 100),
                          "excess": grouped[f"excess_{key}"].mean()})
    return table.reindex([r for r in rule_order if r in table.index])


@dataclass(frozen=True)
class Extreme:
    day: date
    code: str
    name: str
    ret: float
    rule_names: tuple[str, ...]


def extremes(report: SignalReport, top_n: int) -> tuple[list[Extreme], list[Extreme]]:
    """本週表現最好、最差各 top_n 個。同一天同一檔同方向的訊號報酬相同，合併成一個並列出全部規則。"""
    rows = report.week_rows
    rows = rows[rows["pushed"] & rows["ret_week"].notna()]
    merged = [Extreme(day, code, group["name"].iloc[0], float(group["ret_week"].iloc[0]),
                      tuple(group["rule_name"]))
              for (day, code, _), group in rows.groupby(["date", "code", "side"], sort=False)]
    ordered = sorted(merged, key=lambda e: (-e.ret, e.day, e.code))
    return ordered[:top_n], list(reversed(ordered))[:top_n]


def read_m6_validation(path: Path, horizons: Sequence[int]) -> dict[tuple[str, int], float]:
    """M6 回測的 summary.csv 中，各規則驗證段的平均超額報酬（%），key 為（規則名稱, 持有天數）。

    分組與 M6 的終端摘要相同：未還原價判斷、去重、排除漲跌停鎖死、含跳空事件。檔案不存在時回傳空的結果。
    """
    if not path.is_file():
        logger.info("找不到 M6 回測的統計 %s，成績單不顯示「M6 驗證段」", path)
        return {}
    columns = SUMMARY_COLUMNS
    try:
        summary = pd.read_csv(path, encoding="utf-8-sig")
        rows = summary[(summary[columns["price"]] == RAW_PRICE) & (summary[columns["segment"]] == VALIDATE)
                       & (summary[columns["sample"]] == DEDUPED) & (summary[columns["limit_locked"]] == EXCLUDE)
                       & (summary[columns["gap_event"]] == INCLUDE) & summary[columns["horizon"]].isin(horizons)]
        return {(row[columns["rule_name"]], int(row[columns["horizon"]])): float(row[columns["excess"]])
                for _, row in rows.iterrows() if pd.notna(row[columns["excess"]])}
    except (KeyError, ValueError) as exc:
        logger.warning("M6 回測的統計 %s 格式不符，成績單不顯示「M6 驗證段」：%s", path, exc)
        return {}
