"""全市場資料的品質報告：python -m screener market-check（RESEARCH_PLAN.md 4.3）。

只讀取 market.db 與 screener.db（唯讀開啟，研究不寫入 screener.db，PROJECT_PLAN.md 10.4 規則 3），不連網。
終端印出摘要，明細寫成 CSV（research.yaml 的 market.check_dir）；每次執行前清掉上一次的 CSV，
沒有問題的檢查不會留下檔案。
"""

import logging
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

import pandas as pd

from screener.config import AppConfig, ResearchConfig
from screener.jobs import JobFailed
from screener.research.market import check
from screener.research.market.check import Section
from screener.research.market.store import SqliteMarketStore

logger = logging.getLogger(__name__)

_QUOTE_COLUMNS = ["code", "date", "market", "name", "open", "high", "low", "close", "volume", "change", "flag"]


def run(config: AppConfig, research: ResearchConfig, *, out: TextIO | None = None) -> list[Section]:
    market = research.market
    if not market.db_path.is_file():
        raise JobFailed(f"找不到 {market.db_path}，請先執行 market-update")
    with SqliteMarketStore(market.db_path) as store:
        quotes = store.quotes_frame(_QUOTE_COLUMNS)
        trading_days = [d.isoformat() for d in store.trading_days()]
        calendar = [d.isoformat() for d in store.calendar()]
        closed = [d.isoformat() for d in store.closed_days()]
        securities = pd.DataFrame(
            [{"code": s.code, "name": s.name, "market": s.market.value, "first_date": s.first_date.isoformat(),
              "last_date": s.last_date.isoformat(), "delisted_on": s.delisted_on.isoformat() if s.delisted_on else None}
             for s in store.securities()],
            columns=["code", "name", "market", "first_date", "last_date", "delisted_on"])
        delistings = pd.DataFrame([{"code": d.code, "date": d.date.isoformat(), "name": d.name}
                                   for d in store.delistings()], columns=["code", "date", "name"])
        events = _events([(r.code, r.date.isoformat(), "dividend", m.value, r.before_price, r.after_price, m.value)
                          for r, m in store.dividend_results()]
                         + [(a.code, a.date.isoformat(), a.kind.value, "", a.before_price, a.after_price, source)
                            for a, source in store.actions()])
        daily_index = {d.isoformat(): v for d, v in store.return_index().items()}
        monthly_index = {d.isoformat(): v for d, v in store.return_index(monthly=True).items()}
        revenue, valuations, shares = store.revenue_frame(), store.valuation_frame(), store.shares_frame()
    if quotes.empty:
        raise JobFailed(f"{market.db_path} 沒有任何行情，請先執行 market-update")

    tse_days = sorted(set(quotes.loc[quotes["market"] == "tse", "date"]))
    sections = [check.overview(quotes, trading_days, calendar, closed, securities)]
    watchlist = _read_watchlist(config.settings.db_path)
    if watchlist is None:
        sections.append(Section("與 screener.db 比對", [f"找不到 {config.settings.db_path}，略過"]))
    else:
        bars, listings, screener_events = watchlist
        sections.append(check.compare_watchlist(quotes, bars, listings, trading_days))
    sections.append(check.check_events(quotes, events))
    if watchlist is not None:
        sections.append(check.compare_screener_events(events, screener_events, bars, quotes))
    sections += [check.check_return_index(daily_index, monthly_index, tse_days),
                 check.check_anomalies(quotes, trading_days, events),
                 check.check_delistings(securities, delistings, trading_days),
                 check.check_listings(quotes, trading_days[0]),
                 check.check_classification(securities),
                 check.check_fundamentals(revenue, valuations, shares, quotes)]

    files = _write_tables(sections, market.check_dir)
    (out or sys.stdout).write(format_report(sections, trading_days, market.check_dir, files) + "\n")
    logger.info("品質報告完成：明細 %d 個檔案在 %s", len(files), market.check_dir)
    return sections


def _events(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["code", "date", "kind", "market", "before_price", "after_price", "source"])


def _read_watchlist(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame] | None:
    """screener.db 的日 K、上市櫃日與事件；以唯讀開啟，不存在時為 None。"""
    if not path.is_file():
        return None
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as conn:
        bars = pd.read_sql_query("SELECT code, date, open, high, low, close, volume FROM daily_prices", conn)
        listings = pd.read_sql_query("SELECT code, listed_on FROM listings", conn)
        events = pd.read_sql_query(
            "SELECT code, date, 'dividend' AS kind, before_price, after_price FROM dividend_results"
            " UNION ALL SELECT code, date, kind, before_price, after_price FROM corporate_actions", conn)
    conn.close()
    return bars, listings, events


def _write_tables(sections: Sequence[Section], directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.glob("*.csv"):
        old.unlink()
    files = []
    for section in sections:
        for name, table in section.tables.items():
            path = directory / f"{name}.csv"
            table.to_csv(path, index=False, encoding="utf-8-sig")
            files.append(path)
    return files


def format_report(sections: Sequence[Section], trading_days: Sequence[str], directory: Path,
                  files: Sequence[Path]) -> str:
    lines = [f"全市場資料品質報告（market.db：{trading_days[0]}～{trading_days[-1]}）"]
    for section in sections:
        lines += ["", f"■ {section.title}", *(f"  {line}" for line in section.lines)]
        lines += [f"  → 明細：{directory / f'{name}.csv'}（{len(table):,} 列）" for name, table in section.tables.items()]
    lines += ["", f"明細 {len(files)} 個檔案在 {directory}" if files else "沒有需要列出的明細"]
    return "\n".join(lines)
