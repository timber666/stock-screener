"""rt-bars：A（錄製檔、歷史逐筆成交）對 B（歷史分 K）與對照清單（REALTIME_PLAN.md 7.1 第 6 點）。"""

import csv
import io
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from fake_shioaji import FakeClient, FakeContract, to_ts
from screener.config import load_config
from screener.jobs import JobFailed, rt_bars
from screener.realtime.config import ShioajiKeys, load_daytrade_config, load_realtime_config
from screener.realtime.recorder import Recorder, recording_path

DAY = date(2026, 11, 19)
PREV = date(2026, 11, 18)
CODE = "4977"


def _kbars(code: str, start: date, end: date) -> dict[str, list]:
    """前一交易日 13:01、13:25、13:30 三根；當天 09:01、09:02（量 0）、09:03、13:26（量 0）、13:30。以結束時間標示。"""
    rows = [(PREV, "13:01", 49.0, 1), (PREV, "13:25", 50.0, 1), (PREV, "13:30", 51.0, 2), (DAY, "09:01", 52.0, 3), (DAY, "09:02", 52.0, 0),
            (DAY, "09:03", 53.0, 1), (DAY, "13:26", 53.0, 0), (DAY, "13:30", 54.0, 5)]
    rows = [row for row in rows if start <= row[0] <= end]
    labels = [datetime.combine(d, time.fromisoformat(t)) for d, t, _, _ in rows]
    return {"ts": [to_ts(label) for label in labels], "Open": [r[2] for r in rows], "High": [r[2] for r in rows],
            "Low": [r[2] for r in rows], "Close": [r[2] for r in rows], "Volume": [r[3] for r in rows],
            "Amount": [r[2] * r[3] * 1000 for r in rows]}


def _ticks(code: str, day: date) -> dict[str, list]:
    """和 _kbars 當天的 K 棒一致的逐筆成交，另有一筆 14:30 的盤後定價。"""
    rows = [("09:00:05", 52.0, 3), ("09:02:30", 53.0, 1), ("13:30:00", 54.0, 5), ("14:30:00", 54.0, 2)]
    moments = [datetime.combine(day, time.fromisoformat(t)) for t, _, _ in rows]
    return {"ts": [to_ts(m) for m in moments], "close": [r[1] for r in rows], "volume": [r[2] for r in rows],
            "bid_price": [0.0] * 4, "bid_volume": [0] * 4, "ask_price": [0.0] * 4, "ask_volume": [0] * 4,
            "tick_type": [1] * 4}


@pytest.fixture
def setup(config_dir: Path):
    config = load_config(config_dir)
    realtime = load_realtime_config(config_dir / "realtime.yaml")
    daytrade = load_daytrade_config(config_dir / "daytrade.yaml")
    client = FakeClient({s.code: FakeContract(s.code, s.name) for s in config.watchlist.stocks},
                        kbars_data=_kbars, ticks_data=_ticks)
    return config, realtime, daytrade, client


def _run(setup, source: str | None):
    config, realtime, daytrade, client = setup
    out = io.StringIO()
    out_dir = rt_bars.run(config, realtime, daytrade, ShioajiKeys("k", "s"), day=DAY, codes=[CODE], source=source,
                          client_factory=lambda: client, out=out)
    return out_dir, out.getvalue()


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_ticks_match_kbars(setup) -> None:
    out_dir, text = _run(setup, "ticks")

    rows = _rows(out_dir / f"minutes_{CODE}_ticks.csv")
    assert [(r["start"], r["close"], r["volume"], r["same_as_b"]) for r in rows] == [
        ("09:00", "52.0", "3", "True"), ("09:02", "53.0", "1", "True"), ("13:30", "54.0", "5", "True")]
    assert "開高低收量不同：0 根" in text
    assert "14:30:00.000000 54.0 × 2（收盤之後）" in text
    # 3MA 延續前一交易日：09:00 那根 = (50 + 51 + 53) / 3（09:00～09:02 的收盤是 53）
    assert float(rows[0]["ma3_drawn"]) == pytest.approx((50 + 51 + 53) / 3)
    # 09:01 時當天第一根 3 分 K 還沒走完，能用的是前一交易日 13:30 那根：(49 + 50 + 51) / 3
    assert float(rows[0]["ma3_known"]) == pytest.approx(50.0)
    assert rows[1]["ma3_known"] == rows[0]["ma3_drawn"]     # 09:03 走完後才能用當天第一根
    # 即時值：09:00 那根 1 分 K 走完時，形成中的 3 分 K 以 52 代入：(50 + 51 + 52) / 3
    assert float(rows[0]["ma3_live"]) == pytest.approx(51.0)
    assert rows[1]["ma3_live"] == rows[1]["ma3_drawn"]       # 09:02 那根走完，09:00 那根 3 分 K 也走完
    assert float(rows[-1]["vp"]) == 54.0                                                 # 收盤量 5 最大


def test_kbars_source_needs_no_ticks(setup) -> None:
    out_dir, text = _run(setup, "kbars")

    assert [r["start"] for r in _rows(out_dir / f"minutes_{CODE}_kbars.csv")] == ["09:00", "09:02", "13:30"]
    assert "A 對 B" not in text                                # A 就是 B，不比對
    assert not any(call[0] == "ticks" for call in setup[3].calls)


def test_record_source_and_default(setup) -> None:
    _, realtime, _, _ = setup
    recorder = Recorder(recording_path(realtime.record.tick_dir, DAY), flush_interval_sec=1).open()
    for moment, price, volume, trial in (("08:59:50", 52.0, 9, True), ("09:00:05", 52.0, 3, False),
                                         ("09:02:30", 53.0, 1, False), ("13:30:00", 54.0, 5, False)):
        recorder.put("tick", 0, {"code": CODE, "datetime": f"{DAY}T{moment}", "close": str(price), "volume": volume,
                                 "simtrade": trial, "intraday_odd": False})
    recorder.close()

    out_dir, text = _run(setup, None)                        # 有錄製檔時預設用 record

    assert (out_dir / f"minutes_{CODE}_record.csv").is_file()
    assert "排除的試撮 1 筆" in text and "開高低收量不同：0 根" in text


def test_missing_recording(setup) -> None:
    with pytest.raises(JobFailed, match="找不到錄製檔"):
        _run(setup, "record")


def test_codes_outside_watchlist(setup) -> None:
    config, realtime, daytrade, client = setup
    with pytest.raises(JobFailed, match="不在觀察清單"):
        rt_bars.run(config, realtime, daytrade, ShioajiKeys("k", "s"), day=DAY, codes=["0000"],
                    client_factory=lambda: client)


def test_compare_with_mis_snapshot(setup, tmp_path: Path) -> None:
    # T11：scan 保存的 MIS 原始資料，在 MIS 的資料時間（秒）結束時比對 A 的現價、開高低與累積量
    import gzip
    import json
    import sqlite3

    from screener.data.db import SqlitePriceStore

    config = setup[0]
    raw = tmp_path / "snapshot.json.gz"
    record = {"c": CODE, "t": "13:10:00", "z": "-", "o": "52.0000", "h": "53.0000", "l": "52.0000", "v": "4",
              "trade": {"t": "13:09:40", "z": "53.0000"}}
    with gzip.open(raw, "wt", encoding="utf-8") as f:
        json.dump({"date": DAY.isoformat(), "scanned_at": f"{DAY}T13:10:01", "records": [record]}, f)
    SqlitePriceStore(config.settings.db_path).close()             # 建立資料表
    with sqlite3.connect(config.settings.db_path) as conn:
        conn.execute("INSERT INTO scan_runs (date, scanned_at, rules_fingerprint, observed, evaluated, not_evaluated, "
                     "substituted, prev_close_mismatch, raw_file) VALUES (?, ?, 'x', 1, 1, 0, 0, 0, ?)",
                     (DAY.isoformat(), f"{DAY}T13:10:01", str(raw)))

    _, text = _run(setup, "ticks")

    assert "MIS 資料時間 13:10:00，現價取 trade.z（13:09:40）：現價、開高低、累積量都相同" in text
