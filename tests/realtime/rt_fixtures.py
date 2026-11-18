"""即時系統測試共用的資料：tests/fixtures/realtime/ 的手工逐筆成交與交易時段。"""

import csv
from datetime import date, datetime, time
from pathlib import Path

from screener.realtime.bars import Trade
from screener.realtime.config import Session

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "realtime"
SESSION = Session(open=time(9, 0), closing_call=time(13, 25), close=time(13, 30))


def load_trades(name: str, day: date, prev_day: date | None = None) -> tuple[list[Trade], list[Trade]]:
    """回傳（今天的成交, 前一交易日的成交）。CSV 的 prev 欄為 1 的列屬於 prev_day。"""
    today: list[Trade] = []
    previous: list[Trade] = []
    with (FIXTURES / f"{name}.csv").open(encoding="utf-8") as f:
        rows = csv.DictReader(line for line in f if not line.startswith("#"))
        for row in rows:
            is_prev = row.get("prev") == "1"
            moment = datetime.combine(prev_day if is_prev else day, time.fromisoformat(row["time"]))
            trade = Trade(time=moment, price=float(row["price"]), volume=int(row["volume"]),
                          is_trial=row["trial"] == "1")
            (previous if is_prev else today).append(trade)
    return today, previous


def at(day: date, text: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(text))
