"""paper_portfolio.db：模擬組合的狀態（RESEARCH_PLAN.md 4.6）。

只保存每次換股的完整排名。帳戶明細由排名與 market.db 以回測相同的模擬算出，規則不會分岔。
每日系統不讀取，也不寫入 screener.db（PROJECT_PLAN.md 10.4）。
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import TracebackType

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rebalances (
    signal_day    TEXT PRIMARY KEY,     -- 訊號日 YYYY-MM-DD
    available_day TEXT NOT NULL,        -- 可用日 D
    factor        TEXT NOT NULL,
    top_n         INTEGER NOT NULL,
    pick          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rankings (   -- 當月因子值不足、不換股時沒有資料列
    signal_day    TEXT NOT NULL,
    rank          INTEGER NOT NULL,     -- 1 為第一名
    code          TEXT NOT NULL,
    PRIMARY KEY (signal_day, rank)
);
"""


@dataclass(frozen=True)
class SavedRebalance:
    signal: date
    available: date
    factor: str
    top_n: int
    pick: str
    ranking: tuple[str, ...]


class PaperStore:
    """開啟（不存在時建立）paper_portfolio.db。用完要 close，或以 with 使用。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PaperStore":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    def rebalances(self) -> list[SavedRebalance]:
        """全部換股紀錄，依訊號日排序。"""
        ranks: dict[str, list[str]] = {}
        for signal, code in self._conn.execute("SELECT signal_day, code FROM rankings ORDER BY signal_day, rank"):
            ranks.setdefault(signal, []).append(code)
        rows = self._conn.execute("SELECT signal_day, available_day, factor, top_n, pick FROM rebalances"
                                  " ORDER BY signal_day").fetchall()
        return [SavedRebalance(date.fromisoformat(signal), date.fromisoformat(available), factor, top_n, pick,
                               tuple(ranks.get(signal, ())))
                for signal, available, factor, top_n, pick in rows]

    def save(self, items: Sequence[SavedRebalance], created_at: datetime) -> None:
        """在同一個交易內寫入，失敗時不會留下不完整的排名。"""
        with self._conn:
            for item in items:
                self._conn.execute("INSERT INTO rebalances (signal_day, available_day, factor, top_n, pick, created_at)"
                                   " VALUES (?, ?, ?, ?, ?, ?)",
                                   (item.signal.isoformat(), item.available.isoformat(), item.factor, item.top_n,
                                    item.pick, created_at.isoformat(timespec="seconds")))
                self._conn.executemany("INSERT INTO rankings (signal_day, rank, code) VALUES (?, ?, ?)",
                                       [(item.signal.isoformat(), i, code) for i, code in enumerate(item.ranking, start=1)])
