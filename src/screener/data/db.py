"""以 SQLite 實作 PriceStore、EventStore 與 ScanStore（PROJECT_PLAN.md 8.3、14.2）。"""

import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from types import TracebackType

from screener.data.base import EventCoverage
from screener.models import (ActionKind, CorporateAction, DailyBar, DividendEvent, DividendResult, Listing, Market,
                             PriceSubstitute, Quote, ScanRun, Side, SignalRecord, Trend)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_prices (
    code    TEXT    NOT NULL,
    date    TEXT    NOT NULL,   -- YYYY-MM-DD
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    volume  INTEGER NOT NULL,   -- 單位：張
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS dividend_events (
    code            TEXT NOT NULL,
    date            TEXT NOT NULL,  -- 除權息交易日
    cash_dividend   REAL NOT NULL,  -- 元／股
    stock_dividend  REAL NOT NULL,  -- 元／股（面額 10 元，即每 10 股配股數）
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS dividend_results (
    code            TEXT NOT NULL,
    date            TEXT NOT NULL,
    before_price    REAL NOT NULL,
    after_price     REAL NOT NULL,  -- 官方除權息參考價
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS corporate_actions (
    code            TEXT NOT NULL,
    date            TEXT NOT NULL,
    kind            TEXT NOT NULL,  -- capital_reduction | split
    before_price    REAL NOT NULL,
    after_price     REAL NOT NULL,
    PRIMARY KEY (code, date, kind)
);
CREATE TABLE IF NOT EXISTS listings (
    code        TEXT PRIMARY KEY,
    market      TEXT NOT NULL,      -- tse | otc
    listed_on   TEXT NOT NULL       -- 在目前市場掛牌的日子
);
CREATE TABLE IF NOT EXISTS event_coverage (
    code    TEXT PRIMARY KEY,
    start   TEXT NOT NULL,          -- 已查詢過事件的期間（含頭尾）
    end     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_runs (      -- 實際推播的盤中掃描，每次一筆（D45）
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date                TEXT    NOT NULL,   -- 交易日
    scanned_at          TEXT    NOT NULL,   -- 掃描開始的時間（本機），YYYY-MM-DDTHH:MM:SS
    rules_fingerprint   TEXT    NOT NULL,   -- 規則檔解析後內容的雜湊
    observed            INTEGER NOT NULL,
    evaluated           INTEGER NOT NULL,
    not_evaluated       INTEGER NOT NULL,
    substituted         INTEGER NOT NULL,   -- 現價為替代值的檔數
    prev_close_mismatch INTEGER NOT NULL,
    raw_file            TEXT                -- MIS 原始資料的壓縮檔（D46）
);
CREATE TABLE IF NOT EXISTS signals (        -- 推播的訊號：一檔股票觸發的一條進場規則
    run_id              INTEGER NOT NULL REFERENCES scan_runs (id),
    code                TEXT    NOT NULL,
    rule_id             TEXT    NOT NULL,
    rule_name           TEXT    NOT NULL,
    side                TEXT    NOT NULL,   -- long | short
    trend               TEXT    NOT NULL,   -- bull | range | bear | unknown
    price               REAL    NOT NULL,   -- 推播時的現價
    price_substitute    TEXT,               -- best_bid | best_ask；NULL 為成交價
    change_pct          REAL,               -- 漲跌幅（%），以 MIS 參考價計算
    detail              TEXT    NOT NULL,
    PRIMARY KEY (run_id, code, rule_id)
);
CREATE TABLE IF NOT EXISTS quote_snapshots (  -- 該次掃描 MIS 回傳的全部股票；原始資料另存壓縮檔（D46）
    run_id      INTEGER NOT NULL REFERENCES scan_runs (id),
    code        TEXT    NOT NULL,
    date        TEXT    NOT NULL,           -- MIS 資料日期
    open        REAL,                       -- 沒有值（MIS 的 "-"）時為 NULL
    high        REAL,
    low         REAL,
    last        REAL,                       -- 成交價：z，為 "-" 時用最後一筆成交 trade.z（D47）
    prev_close  REAL,                       -- 參考價 y（除權息日為除權息參考價）
    volume      INTEGER,                    -- 累積成交量（張）
    best_bid    REAL,
    best_ask    REAL,
    PRIMARY KEY (run_id, code)
);
"""

_UPSERT_BAR = """
INSERT INTO daily_prices (code, date, open, high, low, close, volume)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (code, date) DO UPDATE SET
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume
"""


class SqlitePriceStore:
    """開啟（不存在時建立）資料庫檔與資料表。用完要 close，或以 with 使用。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqlitePriceStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- 日 K ----------------------------------------------------------------

    def latest_date(self, code: str) -> date | None:
        return self._date_of("SELECT MAX(date) FROM daily_prices WHERE code = ?", code)

    def earliest_date(self, code: str) -> date | None:
        return self._date_of("SELECT MIN(date) FROM daily_prices WHERE code = ?", code)

    def upsert_bars(self, bars: Sequence[DailyBar]) -> int:
        rows = [(b.code, b.date.isoformat(), b.open, b.high, b.low, b.close, b.volume) for b in bars]
        return self._write(_UPSERT_BAR, rows)

    def get_bars(self, code: str) -> list[DailyBar]:
        rows = self._conn.execute(
            "SELECT code, date, open, high, low, close, volume FROM daily_prices"
            " WHERE code = ? ORDER BY date",
            (code,),
        ).fetchall()
        return [
            DailyBar(code=c, date=date.fromisoformat(d), open=o, high=h, low=lo, close=cl, volume=v)
            for c, d, o, h, lo, cl, v in rows
        ]

    # --- 除權息與公司行動 -----------------------------------------------------

    def upsert_dividends(self, events: Sequence[DividendEvent]) -> int:
        return self._write(
            "INSERT OR REPLACE INTO dividend_events (code, date, cash_dividend, stock_dividend) VALUES (?, ?, ?, ?)",
            [(e.code, e.date.isoformat(), e.cash_dividend, e.stock_dividend) for e in events],
        )

    def get_dividends(self, code: str) -> list[DividendEvent]:
        rows = self._rows("SELECT date, cash_dividend, stock_dividend FROM dividend_events", code)
        return [DividendEvent(code, date.fromisoformat(d), cash, stock) for d, cash, stock in rows]

    def upsert_dividend_results(self, results: Sequence[DividendResult]) -> int:
        return self._write(
            "INSERT OR REPLACE INTO dividend_results (code, date, before_price, after_price) VALUES (?, ?, ?, ?)",
            [(r.code, r.date.isoformat(), r.before_price, r.after_price) for r in results],
        )

    def get_dividend_results(self, code: str) -> list[DividendResult]:
        rows = self._rows("SELECT date, before_price, after_price FROM dividend_results", code)
        return [DividendResult(code, date.fromisoformat(d), before, after) for d, before, after in rows]

    def upsert_actions(self, actions: Sequence[CorporateAction]) -> int:
        return self._write(
            "INSERT OR REPLACE INTO corporate_actions (code, date, kind, before_price, after_price)"
            " VALUES (?, ?, ?, ?, ?)",
            [(a.code, a.date.isoformat(), a.kind.value, a.before_price, a.after_price) for a in actions],
        )

    def get_actions(self, code: str) -> list[CorporateAction]:
        rows = self._rows("SELECT date, kind, before_price, after_price FROM corporate_actions", code)
        return [CorporateAction(code, date.fromisoformat(d), ActionKind(kind), before, after)
                for d, kind, before, after in rows]

    def event_coverage(self, code: str) -> EventCoverage | None:
        row = self._conn.execute("SELECT start, end FROM event_coverage WHERE code = ?", (code,)).fetchone()
        return EventCoverage(date.fromisoformat(row[0]), date.fromisoformat(row[1])) if row else None

    def set_event_coverage(self, code: str, coverage: EventCoverage) -> None:
        self._write("INSERT OR REPLACE INTO event_coverage (code, start, end) VALUES (?, ?, ?)",
                    [(code, coverage.start.isoformat(), coverage.end.isoformat())])

    def upsert_listings(self, listings: Sequence[Listing]) -> int:
        return self._write("INSERT OR REPLACE INTO listings (code, market, listed_on) VALUES (?, ?, ?)",
                           [(x.code, x.market.value, x.listed_on.isoformat()) for x in listings])

    def get_listing(self, code: str) -> Listing | None:
        row = self._conn.execute("SELECT market, listed_on FROM listings WHERE code = ?", (code,)).fetchone()
        return Listing(code, Market(row[0]), date.fromisoformat(row[1])) if row else None

    # --- 盤中掃描的紀錄 -------------------------------------------------------

    def record_scan(self, run: ScanRun, signals: Sequence[SignalRecord], quotes: Sequence[Quote]) -> int:
        with self._conn:                    # 同一個交易：任何一步失敗時整次掃描都不寫入
            cursor = self._conn.execute(
                "INSERT INTO scan_runs (date, scanned_at, rules_fingerprint, observed, evaluated, not_evaluated,"
                " substituted, prev_close_mismatch, raw_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run.day.isoformat(), run.scanned_at.isoformat(timespec="seconds"), run.rules_fingerprint,
                 run.observed, run.evaluated, run.not_evaluated, run.substituted, run.prev_close_mismatch,
                 run.raw_file),
            )
            run_id = cursor.lastrowid
            self._conn.executemany(
                "INSERT INTO signals (run_id, code, rule_id, rule_name, side, trend, price, price_substitute,"
                " change_pct, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, s.code, s.rule_id, s.rule_name, s.side.value, s.trend.value, s.price,
                  s.price_substitute.value if s.price_substitute else None, s.change_pct, s.detail)
                 for s in signals],
            )
            self._conn.executemany(
                "INSERT INTO quote_snapshots (run_id, code, date, open, high, low, last, prev_close, volume,"
                " best_bid, best_ask) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, q.code, q.date.isoformat(), q.open, q.high, q.low, q.last, q.prev_close, q.volume,
                  q.best_bid, q.best_ask) for q in quotes],
            )
        return run_id

    def get_scan_runs(self, start: date, end: date) -> list[ScanRun]:
        rows = self._conn.execute(
            "SELECT id, date, scanned_at, rules_fingerprint, observed, evaluated, not_evaluated, substituted,"
            " prev_close_mismatch, raw_file FROM scan_runs WHERE date BETWEEN ? AND ? ORDER BY date, scanned_at, id",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [ScanRun(day=date.fromisoformat(d), scanned_at=datetime.fromisoformat(at), rules_fingerprint=fp,
                        observed=observed, evaluated=evaluated, not_evaluated=not_evaluated, substituted=substituted,
                        prev_close_mismatch=mismatch, raw_file=raw_file, id=run_id)
                for run_id, d, at, fp, observed, evaluated, not_evaluated, substituted, mismatch, raw_file in rows]

    def get_signal_records(self, run_id: int) -> list[SignalRecord]:
        rows = self._conn.execute(
            "SELECT code, rule_id, rule_name, side, trend, price, price_substitute, change_pct, detail FROM signals"
            " WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [SignalRecord(code=code, rule_id=rule_id, rule_name=name, side=Side(side), trend=Trend(trend),
                             price=price, price_substitute=PriceSubstitute(sub) if sub else None, change_pct=change,
                             detail=detail)
                for code, rule_id, name, side, trend, price, sub, change, detail in rows]

    def get_snapshot(self, run_id: int) -> dict[str, Quote]:
        rows = self._conn.execute(
            "SELECT code, date, open, high, low, last, prev_close, volume, best_bid, best_ask FROM quote_snapshots"
            " WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return {code: Quote(code, date.fromisoformat(d), *values) for code, d, *values in rows}

    # --- 共用 -----------------------------------------------------------------

    def _date_of(self, sql: str, code: str) -> date | None:
        (value,) = self._conn.execute(sql, (code,)).fetchone()
        return date.fromisoformat(value) if value else None

    def _rows(self, select: str, code: str) -> list[tuple]:
        """select 的資料表依 code 篩選、依日期排序。"""
        return self._conn.execute(f"{select} WHERE code = ? ORDER BY date", (code,)).fetchall()

    def _write(self, sql: str, rows: Sequence[tuple]) -> int:
        with self._conn:                    # 同一批在同一個交易內寫入
            self._conn.executemany(sql, rows)
        return len(rows)
