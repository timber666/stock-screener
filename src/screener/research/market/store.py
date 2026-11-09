"""market.db：全市場的每日行情、報酬指數與事件（RESEARCH_PLAN.md 4.3）。

只存原始資料，還原價在讀取時計算（同 PROJECT_PLAN.md 14.2）。研究指令專用，每日系統不讀取，
研究也不寫入 screener.db（PROJECT_PLAN.md 10.4 規則 3）。
"""

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from types import TracebackType

import pandas as pd

from screener.models import ActionKind, CorporateAction, DividendResult, Market
from screener.research.market.models import (CLOSED, OK, DailyQuotes, Delisting, FetchRecord, MarketQuote,
                                             MonthlyRevenue, Security, SharesOutstanding, Valuation)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_quotes (
    code            TEXT    NOT NULL,
    date            TEXT    NOT NULL,   -- YYYY-MM-DD
    market          TEXT    NOT NULL,   -- tse | otc
    name            TEXT    NOT NULL,
    open            REAL,               -- 沒有成交時為 NULL
    high            REAL,
    low             REAL,
    close           REAL,
    volume          INTEGER NOT NULL,   -- 成交股數（股）
    value           INTEGER NOT NULL,   -- 成交金額（元）
    trades          INTEGER NOT NULL,   -- 成交筆數
    change          REAL,               -- 漲跌價差；沒有成交或有標示時為 NULL。參考價 = close - change
    flag            TEXT,               -- 漲跌欄位的標示：上市 X（不比價）、上櫃「除息」等
    next_limit_up   REAL,               -- 上櫃的次日漲停價、跌停價（上市沒有）
    next_limit_down REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS daily_quotes_date ON daily_quotes (date);
CREATE TABLE IF NOT EXISTS return_index (           -- 發行量加權股價報酬指數，取自上市每日行情
    date    TEXT PRIMARY KEY,
    value   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS return_index_monthly (   -- 同一個指數的月表（MFI94U），品質報告對照用
    date    TEXT PRIMARY KEY,
    value   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dividend_results (       -- 官方除權息結果：證交所 TWT49U、櫃買中心除權息表
    code            TEXT NOT NULL,
    date            TEXT NOT NULL,      -- 除權息交易日
    market          TEXT NOT NULL,
    before_price    REAL NOT NULL,      -- 除權息前收盤價
    after_price     REAL NOT NULL,      -- 除權息參考價
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS corporate_actions (      -- 減資（證交所、櫃買中心）與面額變更（FinMind）
    code            TEXT NOT NULL,
    date            TEXT NOT NULL,      -- 恢復買賣日（減資）、事件日（面額變更）
    kind            TEXT NOT NULL,      -- capital_reduction | split
    before_price    REAL NOT NULL,
    after_price     REAL NOT NULL,
    source          TEXT NOT NULL,      -- tse | otc | finmind
    PRIMARY KEY (code, date, kind)
);
CREATE TABLE IF NOT EXISTS trading_calendar (       -- FinMind TaiwanStockTradingDate，品質報告對照用
    date    TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS delistings (             -- FinMind TaiwanStockDelisting（普通股）
    code    TEXT PRIMARY KEY,
    date    TEXT NOT NULL,
    name    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS securities (             -- 由每日行情彙整，每次 market-update 後重建
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,          -- 最後一個交易日的名稱
    market      TEXT NOT NULL,          -- 最後一個交易日的市場
    first_date  TEXT NOT NULL,          -- 第一次出現在行情的日子
    last_date   TEXT NOT NULL,
    delisted_on TEXT                    -- FinMind 的下市日
);
CREATE TABLE IF NOT EXISTS monthly_revenue (        -- 公開資訊觀測站的月營收（千元）與當時的產業別（M9）
    code                 TEXT NOT NULL,
    month                TEXT NOT NULL,     -- 營收月份，YYYY-MM-01
    market               TEXT NOT NULL,
    name                 TEXT NOT NULL,
    industry             TEXT NOT NULL,
    revenue              INTEGER,
    previous             INTEGER,
    last_year            INTEGER,
    cumulative           INTEGER,
    cumulative_last_year INTEGER,
    PRIMARY KEY (code, month)
);
CREATE TABLE IF NOT EXISTS valuations (             -- 每月第一個交易日的本益比、淨值比、殖利率（M9）
    code            TEXT NOT NULL,
    date            TEXT NOT NULL,
    market          TEXT NOT NULL,
    pe              REAL,               -- 虧損時為 NULL
    pb              REAL,
    dividend_yield  REAL,               -- %
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS shares_outstanding (     -- 每月第一個交易日的發行股數（M9）
    code    TEXT NOT NULL,
    date    TEXT NOT NULL,
    market  TEXT NOT NULL,
    shares  INTEGER NOT NULL,
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS fetch_log (              -- 抓取紀錄，供中斷後續跑
    source      TEXT NOT NULL,          -- tse_daily、otc_daily、tse_dividends …
    start       TEXT NOT NULL,
    end         TEXT NOT NULL,
    status      TEXT NOT NULL,          -- ok | closed（非交易日）
    rows        INTEGER NOT NULL,
    fetched_on  TEXT NOT NULL,
    PRIMARY KEY (source, start)
);
"""

_QUOTE_COLUMNS = ("code", "date", "market", "name", "open", "high", "low", "close", "volume", "value", "trades",
                  "change", "flag", "next_limit_up", "next_limit_down")
_TEXT_COLUMNS = {"code", "date", "market", "name", "flag"}
_CHUNK_ROWS = 200_000


class SqliteMarketStore:
    """開啟（不存在時建立）market.db。用完要 close，或以 with 使用。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SqliteMarketStore":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    # --- 寫入 -----------------------------------------------------------------

    def record_day(self, day: date, results: Sequence[tuple[str, DailyQuotes | None]], fetched_on: date) -> None:
        """在同一個交易內寫入一天各市場的行情（None 為非交易日）、報酬指數與抓取紀錄。"""
        with self._conn:
            for source, daily in results:
                if daily is not None:
                    self._conn.executemany(
                        f"INSERT OR REPLACE INTO daily_quotes ({', '.join(_QUOTE_COLUMNS)})"
                        f" VALUES ({', '.join('?' * len(_QUOTE_COLUMNS))})",
                        [_quote_row(q) for q in daily.quotes])
                    if daily.return_index is not None:
                        self._conn.execute("INSERT OR REPLACE INTO return_index (date, value) VALUES (?, ?)",
                                           (day.isoformat(), daily.return_index))
                self._log(FetchRecord(source, day, day, CLOSED if daily is None else OK,
                                      0 if daily is None else len(daily.quotes), fetched_on))

    def record_dividend_results(self, record: FetchRecord, market: Market, results: Sequence[DividendResult]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO dividend_results (code, date, market, before_price, after_price)"
                " VALUES (?, ?, ?, ?, ?)",
                [(r.code, r.date.isoformat(), market.value, r.before_price, r.after_price) for r in results])
            self._log(record)

    def record_actions(self, record: FetchRecord | None, source: str, actions: Sequence[CorporateAction]) -> None:
        """減資、面額變更；record 為 None 時不寫抓取紀錄（FinMind 的面額變更每次都重抓）。"""
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO corporate_actions (code, date, kind, before_price, after_price, source)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(a.code, a.date.isoformat(), a.kind.value, a.before_price, a.after_price, source) for a in actions])
            if record is not None:
                self._log(record)

    def record_return_index_monthly(self, record: FetchRecord, values: dict[date, float]) -> None:
        with self._conn:
            self._conn.executemany("INSERT OR REPLACE INTO return_index_monthly (date, value) VALUES (?, ?)",
                                   [(day.isoformat(), value) for day, value in values.items()])
            self._log(record)

    def record_revenues(self, record: FetchRecord, revenues: Sequence[MonthlyRevenue]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO monthly_revenue (code, month, market, name, industry, revenue, previous,"
                " last_year, cumulative, cumulative_last_year) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(r.code, r.month.isoformat(), r.market.value, r.name, r.industry, r.revenue, r.previous, r.last_year,
                  r.cumulative, r.cumulative_last_year) for r in revenues])
            self._log(record)

    def record_valuations(self, record: FetchRecord, valuations: Sequence[Valuation]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO valuations (code, date, market, pe, pb, dividend_yield)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(v.code, v.date.isoformat(), v.market.value, v.pe, v.pb, v.dividend_yield) for v in valuations])
            self._log(record)

    def record_shares(self, record: FetchRecord, shares: Sequence[SharesOutstanding]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO shares_outstanding (code, date, market, shares) VALUES (?, ?, ?, ?)",
                [(s.code, s.date.isoformat(), s.market.value, s.shares) for s in shares])
            self._log(record)

    def record_calendar(self, days: Iterable[date]) -> None:
        with self._conn:
            self._conn.executemany("INSERT OR IGNORE INTO trading_calendar (date) VALUES (?)",
                                   [(day.isoformat(),) for day in days])

    def replace_delistings(self, delistings: Iterable[Delisting]) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM delistings")
            self._conn.executemany("INSERT INTO delistings (code, date, name) VALUES (?, ?, ?)",
                                   [(d.code, d.date.isoformat(), d.name) for d in delistings])

    def refresh_securities(self) -> int:
        """由每日行情重建證券清單，回傳檔數。"""
        with self._conn:
            self._conn.execute("DELETE FROM securities")
            self._conn.execute("""
                INSERT INTO securities (code, name, market, first_date, last_date, delisted_on)
                SELECT q.code, q.name, q.market, s.first_date, s.last_date, d.date
                FROM (SELECT code, MIN(date) AS first_date, MAX(date) AS last_date FROM daily_quotes GROUP BY code) s
                JOIN daily_quotes q ON q.code = s.code AND q.date = s.last_date
                LEFT JOIN delistings d ON d.code = s.code
            """)
        (n,) = self._conn.execute("SELECT COUNT(*) FROM securities").fetchone()
        return n

    def _log(self, record: FetchRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO fetch_log (source, start, end, status, rows, fetched_on) VALUES (?, ?, ?, ?, ?, ?)",
            (record.source, record.start.isoformat(), record.end.isoformat(), record.status, record.rows,
             record.fetched_on.isoformat()))

    # --- 讀取 -----------------------------------------------------------------

    def fetch_records(self, source: str) -> dict[date, FetchRecord]:
        """該來源的抓取紀錄，key 為期間的第一天。"""
        rows = self._conn.execute(
            "SELECT start, end, status, rows, fetched_on FROM fetch_log WHERE source = ?", (source,)).fetchall()
        return {date.fromisoformat(start): FetchRecord(source, date.fromisoformat(start), date.fromisoformat(end),
                                                       status, n, date.fromisoformat(fetched_on))
                for start, end, status, n, fetched_on in rows}

    def trading_days(self, market: Market | None = None) -> list[date]:
        """抓取紀錄中有行情的日子；不指定市場時為任一市場有行情的日子。"""
        sources = [f"{m.value}_daily" for m in ([market] if market else list(Market))]
        rows = self._conn.execute(
            f"SELECT DISTINCT start FROM fetch_log WHERE status = ? AND source IN ({', '.join('?' * len(sources))})"
            " ORDER BY start", [OK, *sources]).fetchall()
        return [date.fromisoformat(d) for (d,) in rows]

    def covered_through(self) -> date | None:
        """兩個市場都查詢過的最後一天（含休市日）；還沒有任何紀錄時為 None。"""
        rows = self._conn.execute("SELECT source, MAX(start) FROM fetch_log WHERE source IN ('tse_daily', 'otc_daily')"
                                  " GROUP BY source").fetchall()
        return min(date.fromisoformat(last) for _, last in rows) if len(rows) == len(Market) else None

    def calendar(self) -> list[date]:
        return [date.fromisoformat(d) for (d,) in self._conn.execute("SELECT date FROM trading_calendar ORDER BY date")]

    def closed_days(self) -> list[date]:
        """抓取紀錄中判定為非交易日的日子（兩個市場都沒有行情）。"""
        rows = self._conn.execute("SELECT DISTINCT start FROM fetch_log WHERE source = 'tse_daily' AND status = ?"
                                  " ORDER BY start", (CLOSED,)).fetchall()
        return [date.fromisoformat(d) for (d,) in rows]

    def securities(self) -> list[Security]:
        rows = self._conn.execute(
            "SELECT code, name, market, first_date, last_date, delisted_on FROM securities ORDER BY code").fetchall()
        return [Security(code, name, Market(market), date.fromisoformat(first), date.fromisoformat(last),
                         date.fromisoformat(delisted) if delisted else None)
                for code, name, market, first, last, delisted in rows]

    def quotes_of(self, code: str) -> list[MarketQuote]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_QUOTE_COLUMNS)} FROM daily_quotes WHERE code = ? ORDER BY date", (code,)).fetchall()
        return [_quote_from_row(row) for row in rows]

    def quotes_frame(self, columns: Sequence[str], start: date | None = None, end: date | None = None) -> pd.DataFrame:
        """全部股票的行情（指定欄位），供股票池與品質報告使用；date 為字串 YYYY-MM-DD。

        2015 年起約 550 萬列。分批讀取，文字欄位（代號、日期、名稱等）相同的值共用同一個字串物件：
        每列各自一份字串時約需 2 GB 記憶體，共用後約四分之一。
        """
        unknown = set(columns) - set(_QUOTE_COLUMNS)
        if unknown:
            raise ValueError(f"未知的欄位：{unknown}")
        sql = f"SELECT {', '.join(columns)} FROM daily_quotes WHERE date BETWEEN ? AND ?"
        params = ((start or date.min).isoformat(), (end or date.max).isoformat())
        text_columns = [c for c in columns if c in _TEXT_COLUMNS]
        shared: dict[str, str] = {}
        chunks = []
        for chunk in pd.read_sql_query(sql, self._conn, params=params, chunksize=_CHUNK_ROWS):
            for column in text_columns:
                chunk[column] = [shared.setdefault(v, v) if isinstance(v, str) else v for v in chunk[column]]
            chunks.append(chunk)
        if not chunks:
            return pd.DataFrame(columns=list(columns))
        return pd.concat(chunks, ignore_index=True)

    def revenue_frame(self) -> pd.DataFrame:
        """月營收與產業別；month 為字串 YYYY-MM-01。"""
        return pd.read_sql_query("SELECT code, month, market, name, industry, revenue, previous, last_year, cumulative,"
                                 " cumulative_last_year FROM monthly_revenue", self._conn)

    def valuation_frame(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT code, date, market, pe, pb, dividend_yield FROM valuations", self._conn)

    def shares_frame(self) -> pd.DataFrame:
        return pd.read_sql_query("SELECT code, date, market, shares FROM shares_outstanding", self._conn)

    def dividend_results(self, code: str | None = None) -> list[tuple[DividendResult, Market]]:
        sql = "SELECT code, date, before_price, after_price, market FROM dividend_results"
        rows = self._select(sql, code)
        return [(DividendResult(c, date.fromisoformat(d), before, after), Market(m)) for c, d, before, after, m in rows]

    def actions(self, code: str | None = None) -> list[tuple[CorporateAction, str]]:
        """(事件, 來源)。"""
        sql = "SELECT code, date, kind, before_price, after_price, source FROM corporate_actions"
        rows = self._select(sql, code)
        return [(CorporateAction(c, date.fromisoformat(d), ActionKind(kind), before, after), source)
                for c, d, kind, before, after, source in rows]

    def delistings(self) -> list[Delisting]:
        rows = self._conn.execute("SELECT code, date, name FROM delistings ORDER BY code").fetchall()
        return [Delisting(code, date.fromisoformat(d), name) for code, d, name in rows]

    def return_index(self, monthly: bool = False) -> dict[date, float]:
        table = "return_index_monthly" if monthly else "return_index"
        return {date.fromisoformat(d): value
                for d, value in self._conn.execute(f"SELECT date, value FROM {table} ORDER BY date").fetchall()}

    def _select(self, sql: str, code: str | None) -> list[tuple]:
        if code is None:
            return self._conn.execute(f"{sql} ORDER BY code, date").fetchall()
        return self._conn.execute(f"{sql} WHERE code = ? ORDER BY date", (code,)).fetchall()


def _quote_row(q: MarketQuote) -> tuple:
    return (q.code, q.date.isoformat(), q.market.value, q.name, q.open, q.high, q.low, q.close, q.volume, q.value,
            q.trades, q.change, q.flag, q.next_limit_up, q.next_limit_down)


def _quote_from_row(row: tuple) -> MarketQuote:
    code, day, market, name, *rest = row
    return MarketQuote(code, date.fromisoformat(day), Market(market), name, *rest)
