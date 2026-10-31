"""以 SQLite 實作 PriceStore（PROJECT_PLAN.md 8.3）。"""

import sqlite3
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from types import TracebackType

from screener.models import DailyBar

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
)
"""

_UPSERT = """
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
    """開啟（不存在時建立）資料庫檔與 daily_prices 表。用完要 close，或以 with 使用。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(_SCHEMA)

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

    def latest_date(self, code: str) -> date | None:
        (value,) = self._conn.execute(
            "SELECT MAX(date) FROM daily_prices WHERE code = ?", (code,)
        ).fetchone()
        return date.fromisoformat(value) if value else None

    def upsert_bars(self, bars: Sequence[DailyBar]) -> int:
        rows = [(b.code, b.date.isoformat(), b.open, b.high, b.low, b.close, b.volume) for b in bars]
        with self._conn:                    # 同一批在同一個交易內寫入
            self._conn.executemany(_UPSERT, rows)
        return len(rows)

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
