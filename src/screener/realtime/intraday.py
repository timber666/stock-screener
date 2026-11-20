"""intraday.db：歷史 1 分 K 的讀寫（REALTIME_PLAN.md 7.2 第 3 點）。

| 資料表 | 內容 |
|---|---|
| bars_1m | 1 分 K：minute 為開始時間 HH:MM（第 2 節的定義，已轉換、刪除量為 0 的 K 棒）；量為張、金額為元 |
| days | 每個查過的股日；bars = 0 為「沒有資料」（查詢時流量低於停止點，Shioaji 仍沒有回傳 K 棒） |
| fetch_log | 每次查詢：區間、要查的股日數、寫入的 K 棒數、刪除的量為 0 的 K 棒數、查詢前的流量、結果 |

每次查詢的 K 棒、股日與查詢紀錄在同一個交易內寫入：中斷時不會留下半個區間，重跑時從缺的股日繼續。
只有即時系統讀寫這個資料庫；每日系統與研究程式都不讀取（REALTIME_PLAN.md 4.2 規則 3）。
"""

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import TracebackType

import pandas as pd

from screener.realtime.bars import Bar

OK = "ok"                # 有 K 棒
EMPTY = "empty"          # 流量低於停止點時沒有回傳任何 K 棒：要查的股日都記為沒有資料
FAILED = "failed"        # 查詢失敗（重試後仍失敗）或合約查不到：不寫入股日，下次再查
STATUSES = (OK, EMPTY, FAILED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_1m (
    code    TEXT    NOT NULL,
    date    TEXT    NOT NULL,   -- YYYY-MM-DD
    minute  TEXT    NOT NULL,   -- 開始時間 HH:MM；13:30 為收盤成交
    open    REAL    NOT NULL,
    high    REAL    NOT NULL,
    low     REAL    NOT NULL,
    close   REAL    NOT NULL,
    volume  INTEGER NOT NULL,   -- 張
    amount  REAL    NOT NULL,   -- 元
    PRIMARY KEY (code, date, minute)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS days (
    code        TEXT    NOT NULL,
    date        TEXT    NOT NULL,
    bars        INTEGER NOT NULL,   -- 0 = 沒有資料
    fetched_at  TEXT    NOT NULL,   -- YYYY-MM-DDTHH:MM:SS
    PRIMARY KEY (code, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL,
    start       TEXT    NOT NULL,   -- 查詢的起日（含）
    end         TEXT    NOT NULL,   -- 查詢的迄日（含）
    fetched_at  TEXT    NOT NULL,
    days        INTEGER NOT NULL,   -- 這次要查的股日數
    bars        INTEGER NOT NULL,   -- 寫入的 K 棒數
    zero_bars   INTEGER NOT NULL,   -- 刪除的量為 0 的 K 棒數（之前的日子應為 0，T5）
    usage_bytes INTEGER,            -- 查詢前 usage() 的已用量；usage() 約每 5 秒才更新，相鄰兩筆的差是大約的流量
    status      TEXT    NOT NULL,   -- ok | empty | failed
    note        TEXT                -- 失敗的原因；回傳了不在要查的股日裡的日子（沒有寫入）
);
"""


@dataclass(frozen=True)
class FetchRecord:
    code: str
    start: date
    end: date
    fetched_at: datetime
    days: int
    bars: int
    zero_bars: int
    usage_bytes: int | None
    status: str
    note: str | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"未知的查詢結果：{self.status}")


class IntradayStore:
    """intraday.db。readonly 時以唯讀開啟（rt-check），資料庫不存在就報錯。"""

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        self.path = path
        if readonly:
            if not path.is_file():
                raise FileNotFoundError(f"找不到 {path}：先執行 rt-backfill")
            self._conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path)
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "IntradayStore":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    # --- 寫入 -----------------------------------------------------------------

    def save_query(self, record: FetchRecord, bars: Sequence[Bar], days: Mapping[date, int]) -> None:
        """一次查詢的結果：K 棒、每個查過的股日（K 棒數，0 = 沒有資料）與查詢紀錄，在同一個交易內寫入。"""
        fetched_at = _timestamp(record.fetched_at)
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO bars_1m (code, date, minute, open, high, low, close, volume, amount) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(record.code, bar.start.date().isoformat(), bar.start.strftime("%H:%M"), bar.open, bar.high, bar.low,
                  bar.close, bar.volume, bar.amount) for bar in bars])
            self._conn.executemany("INSERT OR REPLACE INTO days (code, date, bars, fetched_at) VALUES (?, ?, ?, ?)",
                                   [(record.code, day.isoformat(), count, fetched_at)
                                    for day, count in sorted(days.items())])
            self._insert_log(record)

    def log_failure(self, record: FetchRecord) -> None:
        """失敗的查詢只寫查詢紀錄：股日不記錄，下次再查。"""
        with self._conn:
            self._insert_log(record)

    def _insert_log(self, record: FetchRecord) -> None:
        self._conn.execute(
            "INSERT INTO fetch_log (code, start, end, fetched_at, days, bars, zero_bars, usage_bytes, status, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.code, record.start.isoformat(), record.end.isoformat(), _timestamp(record.fetched_at), record.days,
             record.bars, record.zero_bars, record.usage_bytes, record.status, record.note))

    # --- 讀取 -----------------------------------------------------------------

    def fetched_days(self) -> dict[str, dict[date, int]]:
        """每檔查過的股日 → K 棒數（0 = 沒有資料）。"""
        result: dict[str, dict[date, int]] = {}
        for code, day, count in self._conn.execute("SELECT code, date, bars FROM days ORDER BY code, date"):
            result.setdefault(code, {})[date.fromisoformat(day)] = count
        return result

    def bars(self, code: str, start: date | None = None, end: date | None = None) -> list[Bar]:
        """一檔股票的 1 分 K，依時間排序；start、end 為日期（含）。"""
        return [_to_bar(row) for row in self.bar_rows(code, start, end)]

    def bar_rows(self, code: str, start: date | None = None,
                 end: date | None = None) -> Iterator[tuple[str, str, float, float, float, float, int, float]]:
        """(date, minute, open, high, low, close, volume, amount)，依時間排序。"""
        sql = "SELECT date, minute, open, high, low, close, volume, amount FROM bars_1m WHERE code = ?"
        params: list[str] = [code]
        if start is not None:
            sql += " AND date >= ?"
            params.append(start.isoformat())
        if end is not None:
            sql += " AND date <= ?"
            params.append(end.isoformat())
        yield from self._conn.execute(sql + " ORDER BY date, minute", params)

    def bars_frame(self, code: str) -> pd.DataFrame:
        """一檔股票全部的 1 分 K：date、minute（字串）、open、high、low、close、volume，依時間排序（rt-check 用）。"""
        return pd.read_sql_query("SELECT date, minute, open, high, low, close, volume FROM bars_1m WHERE code = ? "
                                 "ORDER BY date, minute", self._conn, params=(code,))

    def fetch_log(self) -> list[FetchRecord]:
        rows = self._conn.execute("SELECT code, start, end, fetched_at, days, bars, zero_bars, usage_bytes, status, note "
                                  "FROM fetch_log ORDER BY id")
        return [FetchRecord(code=code, start=date.fromisoformat(start), end=date.fromisoformat(end),
                            fetched_at=datetime.fromisoformat(fetched_at), days=days, bars=bars, zero_bars=zero_bars,
                            usage_bytes=usage_bytes, status=status, note=note)
                for code, start, end, fetched_at, days, bars, zero_bars, usage_bytes, status, note in rows]


def _timestamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _to_bar(row: tuple[str, str, float, float, float, float, int, float]) -> Bar:
    day, minute, open_, high, low, close, volume, amount = row
    return Bar(start=datetime.fromisoformat(f"{day}T{minute}"), minutes=1, open=open_, high=high, low=low, close=close,
               volume=volume, amount=amount)
