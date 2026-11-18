"""錄製檔的寫入與讀取（REALTIME_PLAN.md 7.1 第 4 點）。"""

import gzip
import threading
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path

import pytest

from screener.realtime.recorder import Recorder, jsonable, read_records, recording_path


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 19, 9, 0, 0)

    def __call__(self) -> datetime:
        return self.now


class Exchange(Enum):
    TSE = "TSE"


def test_records_are_written_and_read_back(tmp_path: Path) -> None:
    clock = Clock()
    path = recording_path(tmp_path, date(2026, 11, 19))
    recorder = Recorder(path, flush_interval_sec=1, clock=clock).open()
    recorder.put("tick", 0, {"code": "4977", "close": Decimal("187.5"), "datetime": datetime(2026, 11, 19, 9, 0, 7),
                             "exchange": Exchange.TSE, "simtrade": False})
    clock.now += timedelta(seconds=2)
    recorder.put("event", 1, {"resp_code": 0, "event": "Session up"})
    recorder.close()

    records = list(read_records(path))

    assert path.name == "2026-11-19.jsonl.gz"
    assert [(r["type"], r["conn"]) for r in records] == [("tick", 0), ("event", 1)]
    assert records[0]["received_at"] == "2026-11-19T09:00:00.000000"
    assert records[0]["data"] == {"code": "4977", "close": "187.5", "datetime": "2026-11-19T09:00:07",
                                  "exchange": "TSE", "simtrade": False}
    assert recorder.counts == {"tick": 1, "event": 1}


def test_restart_appends_to_the_same_day(tmp_path: Path) -> None:
    path = tmp_path / "day.jsonl.gz"
    for code in ("1303", "4951"):
        recorder = Recorder(path, flush_interval_sec=1).open()
        recorder.put("tick", 0, {"code": code})
        recorder.close()

    assert [r["data"]["code"] for r in read_records(path)] == ["1303", "4951"]


def test_flushed_records_survive_an_abrupt_stop(tmp_path: Path) -> None:
    # 程式被強制結束：flush 之前的資料讀得到，截斷的結尾被略過
    path = tmp_path / "day.jsonl.gz"
    recorder = Recorder(path, flush_interval_sec=1).open()
    for n in range(3):
        recorder.put("tick", 0, {"n": n})
    recorder.pump()
    recorder.flush()
    recorder.put("tick", 0, {"n": 3, "padding": "x" * 5000})
    recorder.pump()
    data = path.read_bytes()
    truncated = tmp_path / "truncated.jsonl.gz"
    truncated.write_bytes(data + gzip.compress(b'{"type": "tick"')[:-12])      # 不完整的結尾

    assert [r["data"]["n"] for r in read_records(truncated)] == [0, 1, 2]
    recorder.close()


@pytest.mark.parametrize("cut", [1, 10, 30, 60])
def test_file_cut_short_keeps_complete_lines(tmp_path: Path, cut: int) -> None:
    path = tmp_path / "day.jsonl.gz"
    recorder = Recorder(path, flush_interval_sec=1).open()
    for n in range(3):
        recorder.put("tick", 0, {"n": n})
    recorder.pump()
    recorder.flush()
    recorder.put("tick", 0, {"n": 3, "padding": "x" * 5000})
    recorder.close()
    short = tmp_path / "short.jsonl.gz"
    short.write_bytes(path.read_bytes()[:-cut])

    numbers = [r["data"]["n"] for r in read_records(short)]

    assert numbers in ([0, 1, 2], [0, 1, 2, 3])            # flush 之前的資料都在


def test_put_from_other_threads(tmp_path: Path) -> None:
    path = tmp_path / "day.jsonl.gz"
    recorder = Recorder(path, flush_interval_sec=1).open()
    threads = [threading.Thread(target=lambda i=i: [recorder.put("tick", i, {"n": n}) for n in range(100)])
               for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    recorder.close()

    assert len(list(read_records(path))) == 400


def test_jsonable_handles_time_and_nested_values() -> None:
    assert jsonable({"t": time(9, 0, 1, 5), "d": date(2026, 11, 19), "prices": (Decimal("1.10"), 2)}) == {
        "t": "09:00:01.000005", "d": "2026-11-19", "prices": ["1.10", 2]}
