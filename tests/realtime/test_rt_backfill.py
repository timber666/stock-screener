"""rt-backfill：要查的股日、區間與順序、流量停止點、沒有資料、續跑、案例股、--dry-run（REALTIME_PLAN.md 7.2 第 3、8 點）。

以 M12 的假連線（fake_shioaji.FakeClient）測試，不連網。假的 kbars 和 Shioaji 一樣以結束時間標示：每個有資料的日子
09:01（量 2）、09:02（量 0，當天的資料才會補上的空 K 棒）、13:30（量 3）三根，轉換後是 09:00、13:30 兩根（7.1）。
"""

import io
import sqlite3
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import pytest

from fake_shioaji import FakeClient, FakeContract, to_ts
from screener.config import load_config
from screener.data.db import SqlitePriceStore
from screener.jobs import JobFailed, rt_backfill
from screener.models import DailyBar, Listing, Market
from screener.realtime.backfill import Chunk, estimate, first_day, missing_days, plan_chunks, select_chunk
from screener.realtime.bars import Bar
from screener.realtime.config import ShioajiKeys, load_daytrade_config, load_realtime_config
from screener.realtime.intraday import IntradayStore
from screener.realtime.shioaji_source import ShioajiRequestError

FIRST_CODE, SECOND_CODE = "1303", "4977"          # 觀察清單中 1303 排在 4977 之前
DAYS = [d.date() for d in pd.bdate_range("2026-10-01", "2026-11-19")]   # 36 個平日
SUSPENDED = date(2026, 10, 15)                    # SECOND_CODE 這天沒有日 K（例：暫停交易）
KEYS = ShioajiKeys("key", "secret")


# --- 規劃（純函式）------------------------------------------------------------------

def test_plan_chunks_newest_first_within_query_days() -> None:
    # 11-19 往前 29 天是 10-21：第一個區間 10-21～11-19；第二個區間從 10-20 起往前，只剩 10-01～10-20。
    # 全部區間依迄日由新到舊，同一個迄日依觀察清單的順序（A 在 B 之前）。
    missing = {"B": [d for d in DAYS if d != SUSPENDED], "A": DAYS}

    chunks = plan_chunks(missing, 30, order=["A", "B"])

    assert [(c.code, c.start, c.end) for c in chunks] == [
        ("A", date(2026, 10, 21), date(2026, 11, 19)), ("B", date(2026, 10, 21), date(2026, 11, 19)),
        ("A", date(2026, 10, 1), date(2026, 10, 20)), ("B", date(2026, 10, 1), date(2026, 10, 20))]
    assert all((c.end - c.start).days <= 29 for c in chunks)
    assert len(chunks[3].days) == 13 and SUSPENDED not in chunks[3].days      # 10-01～10-20 的 14 個平日少一天
    assert sum(len(c.days) for c in chunks) == 2 * len(DAYS) - 1


def test_plan_chunks_only_cover_missing_days() -> None:
    # 中間已經查過的股日不會被包進區間：缺 11-19 與 10-01～10-02 時，11-19 自成一次查詢。
    chunks = plan_chunks({"A": [date(2026, 10, 1), date(2026, 10, 2), date(2026, 11, 19)]}, 30, order=["A"])

    assert [(c.start, c.end, len(c.days)) for c in chunks] == [(date(2026, 11, 19), date(2026, 11, 19), 1),
                                                               (date(2026, 10, 1), date(2026, 10, 2), 2)]


def test_missing_days_subtracts_fetched_days() -> None:
    fetched = {"A": {date(2026, 10, 1): 200, date(2026, 10, 2): 0}}     # 沒有資料（0）也算查過

    assert missing_days({"A": DAYS[:3], "B": DAYS[:1]}, fetched) == {"A": [DAYS[2]], "B": [DAYS[0]]}
    assert missing_days({"A": DAYS[:2]}, fetched) == {}


def test_estimate_traffic_and_trading_days() -> None:
    # 2 次查詢、30 個股日 × 1,000 bytes = 30,000 bytes；停止點 20,000 bytes → 2 個交易日；每次 15,000 bytes → 每天 1 次
    chunks = [Chunk("A", DAYS[0], DAYS[19], tuple(DAYS[:20])), Chunk("B", DAYS[0], DAYS[9], tuple(DAYS[:10]))]

    result = estimate(chunks, bytes_per_stock_day=1000, stop_bytes=20_000)

    assert (result.stock_days, result.queries, result.bytes, result.trading_days, result.queries_per_day) == (
        30, 2, 30_000, 2, 1)


@pytest.mark.parametrize("code, listed_on, transfers, expected", [
    ("A", date(2022, 11, 3), (), date(2022, 11, 3)),        # 上市櫃日較晚：之前是興櫃交易（L25）
    ("A", date(2013, 11, 20), (), date(2020, 6, 1)),        # 早就上市櫃：從 start 起
    ("A", date(2026, 6, 1), ("A",), date(2020, 6, 1)),      # 轉市場：上市櫃日之前也是上市櫃交易
    ("A", None, (), date(2020, 6, 1)),                      # listings 沒有這一檔
])
def test_first_day_is_start_or_listing_date(code, listed_on, transfers, expected) -> None:
    assert first_day(code, date(2020, 6, 1), listed_on, transfers) == expected


def test_select_chunk_keeps_only_wanted_days() -> None:
    chunk = Chunk("A", DAYS[0], DAYS[2], (DAYS[0], DAYS[2]))
    bars = [Bar(datetime.combine(day, time(9, 0)), 1, 10, 10, 10, 10, 1, 10_000) for day in DAYS[:3]]

    result = select_chunk(bars, chunk)

    assert [bar.start.date() for bar in result.bars] == [DAYS[0], DAYS[2]]
    assert result.counts == {DAYS[0]: 1, DAYS[2]: 1}
    assert result.extra_days == [DAYS[1]]


# --- 指令 -------------------------------------------------------------------------

def _kbars_for(days: set[date]):
    """假的 kbars：start～end 之間屬於 days 的日子各三根（結束時間標示），見檔案開頭。"""

    def kbars(code: str, start: date, end: date) -> dict[str, list]:
        rows = []
        for day in sorted(d for d in days if start <= d <= end):
            rows += [(datetime.combine(day, time(9, 1)), 50.0, 51.0, 49.5, 50.5, 2),
                     (datetime.combine(day, time(9, 2)), 50.5, 50.5, 50.5, 50.5, 0),
                     (datetime.combine(day, time(13, 30)), 51.0, 51.0, 51.0, 51.0, 3)]
        return {"ts": [to_ts(r[0]) for r in rows], "Open": [r[1] for r in rows], "High": [r[2] for r in rows],
                "Low": [r[3] for r in rows], "Close": [r[4] for r in rows], "Volume": [r[5] for r in rows],
                "Amount": [r[4] * r[5] * 1000 for r in rows]}

    return kbars


@pytest.fixture
def setup(config_dir: Path):
    config = load_config(config_dir)
    realtime = load_realtime_config(config_dir / "realtime.yaml")
    daytrade = load_daytrade_config(config_dir / "daytrade.yaml")
    with SqlitePriceStore(config.settings.db_path) as store:
        store.upsert_bars([DailyBar(code, day, 50.0, 51.0, 49.5, 51.0, 6) for code in (FIRST_CODE, SECOND_CODE)
                           for day in DAYS if not (code == SECOND_CODE and day == SUSPENDED)])
    client = FakeClient({s.code: FakeContract(s.code, s.name) for s in config.watchlist.stocks},
                        kbars_data=_kbars_for(set(DAYS)))
    return config, realtime, daytrade, client


def _run(setup, **kwargs):
    config, realtime, daytrade, client = setup
    out = io.StringIO()
    kwargs.setdefault("client_factory", lambda: client)
    kwargs.setdefault("now", lambda: datetime(2026, 11, 20, 14, 30))         # 週五 14:30（L19 的排程時間）
    summary = rt_backfill.run(config, kwargs.pop("realtime", realtime), daytrade, KEYS, out=out, **kwargs)
    return summary, out.getvalue()


def _queries(client: FakeClient) -> list[tuple[str, date, date]]:
    return [call[1:] for call in client.calls if call[0] == "kbars"]


def _store(setup) -> IntradayStore:
    return IntradayStore(setup[1].backfill.db_path, readonly=True)


def test_backfill_writes_converted_bars_newest_first(setup) -> None:
    summary, text = _run(setup)
    client = setup[3]

    assert _queries(client) == [(FIRST_CODE, date(2026, 10, 21), date(2026, 11, 19)),
                                (SECOND_CODE, date(2026, 10, 21), date(2026, 11, 19)),
                                (FIRST_CODE, date(2026, 10, 1), date(2026, 10, 20)),
                                (SECOND_CODE, date(2026, 10, 1), date(2026, 10, 20))]
    with _store(setup) as store:
        fetched = store.fetched_days()
        bars = store.bars(FIRST_CODE, DAYS[0], DAYS[0])
        log = store.fetch_log()
    # 標示減 1 分鐘、量為 0 的 K 棒刪除、13:30 維持
    assert [(bar.start.time(), bar.volume, bar.amount) for bar in bars] == [(time(9, 0), 2, 101_000.0),
                                                                            (time(13, 30), 3, 153_000.0)]
    assert set(fetched[FIRST_CODE]) == set(DAYS) and set(fetched[FIRST_CODE].values()) == {2}
    assert SUSPENDED not in fetched[SECOND_CODE]                      # 只查 screener.db 有日 K 的日子
    # 10-21～11-19 有 22 個平日、10-01～10-20 有 14 個（SECOND_CODE 少了 SUSPENDED，13 個）；每天轉換後 2 根、刪除 1 根。
    # zero_bars 算的是回傳的全部 K 棒：SECOND_CODE 的 SUSPENDED 沒有寫入，但那天的量為 0 的 K 棒也算在內（14）
    assert [(r.status, r.days, r.bars, r.zero_bars) for r in log] == [("ok", 22, 44, 22), ("ok", 22, 44, 22),
                                                                      ("ok", 14, 28, 14), ("ok", 13, 26, 14)]
    assert (summary.queries, summary.recorded, summary.no_data, summary.remaining, summary.stopped) == (4, 71, 0, 0,
                                                                                                         "done")
    assert "寫入 71 個股日" in text and "停止原因：要查的股日都查完了" in text
    assert client.logged_out


def test_backfill_only_queries_missing_days_and_new_trading_days(setup) -> None:
    config, realtime, daytrade, client = setup
    _run(setup)
    client.calls.clear()

    _, text = _run(setup)                                          # 都查過了：不登入、不查詢
    assert client.calls == [] and "都查過了" in text

    new_day = date(2026, 11, 20)                                   # 每天新增的交易日會自動補上
    with SqlitePriceStore(config.settings.db_path) as store:
        store.upsert_bars([DailyBar(FIRST_CODE, new_day, 50.0, 51.0, 49.5, 51.0, 6)])
    client.kbars_data = _kbars_for({*DAYS, new_day})
    _run(setup)
    assert _queries(client) == [(FIRST_CODE, new_day, new_day)]


def test_backfill_stops_at_traffic_stop_and_resumes(setup) -> None:
    # 每次 kbars 用 10,000 bytes、每次 usage 700 bytes（FakeClient）。停止點 0.02 MB = 20,971 bytes：
    # 第 1 次查詢前 700、第 2 次前 11,400、第 3 次前 22,100 ≥ 20,971 → 查了 2 次就停止，下次從缺的股日繼續。
    config, realtime, daytrade, client = setup
    small = replace(realtime, backfill=replace(realtime.backfill, traffic_stop_mb=0.02))

    summary, text = _run(setup, realtime=small)
    assert len(_queries(client)) == 2 and summary.stopped == "traffic"
    assert summary.remaining == 71 - 44 and "流量到停止點 0.02 MB" in text and "剩下 27 個股日" in text

    client.calls.clear()
    client.usage_bytes = 0                                         # 隔天 08:00 流量重置
    summary, _ = _run(setup)
    assert _queries(client) == [(FIRST_CODE, date(2026, 10, 1), date(2026, 10, 20)),
                                (SECOND_CODE, date(2026, 10, 1), date(2026, 10, 20))]
    assert summary.stopped == "done"


def test_empty_result_below_stop_is_recorded_as_no_data(setup) -> None:
    # SECOND_CODE 在 10-01～10-20 沒有資料（例：上市櫃前的興櫃期間）：流量低於停止點，記為沒有資料，不會再查
    config, realtime, daytrade, client = setup
    with_data = _kbars_for(set(DAYS))
    client.kbars_data = lambda code, start, end: (with_data(code, start, end)
                                                  if code == FIRST_CODE or start >= date(2026, 10, 21)
                                                  else _kbars_for(set())(code, start, end))
    summary, _ = _run(setup)

    with _store(setup) as store:
        fetched = store.fetched_days()[SECOND_CODE]
        log = store.fetch_log()
    assert {fetched[d] for d in DAYS if d < date(2026, 10, 21) and d != SUSPENDED} == {0}
    assert log[-1].status == "empty" and log[-1].bars == 0
    assert (summary.empty, summary.no_data, summary.remaining) == (1, 13, 0)


def test_empty_result_over_the_limit_is_not_recorded(setup) -> None:
    # 超過上限時 Shioaji 回傳空值、不報錯：查詢後的用量已到停止點時，不能記為沒有資料，停止並留到下次
    config, realtime, daytrade, client = setup
    with_data = _kbars_for(set(DAYS))

    def kbars(code: str, start: date, end: date) -> dict[str, list]:
        if code == SECOND_CODE:
            client.usage_bytes = 600 * 1024 * 1024
            return _kbars_for(set())(code, start, end)
        return with_data(code, start, end)

    client.kbars_data = kbars
    summary, _ = _run(setup)

    with _store(setup) as store:
        assert SECOND_CODE not in store.fetched_days()
        assert [r.code for r in store.fetch_log()] == [FIRST_CODE]
    assert summary.stopped == "traffic" and summary.queries == 1


def test_consecutive_failures_stop_with_exit_error(setup) -> None:
    config, realtime, daytrade, client = setup

    def kbars(code: str, start: date, end: date) -> dict[str, list]:
        raise ShioajiRequestError("kbars 失敗（3 次）：TimeoutError")

    client.kbars_data = kbars
    with pytest.raises(JobFailed, match="連續 3 次查詢失敗"):
        _run(setup)

    with _store(setup) as store:
        assert store.fetched_days() == {}                          # 失敗的區間不記錄股日，下次再查
        assert [r.status for r in store.fetch_log()] == ["failed"] * 3
    assert client.logged_out


def test_failures_that_are_not_consecutive_do_not_stop(setup) -> None:
    config, realtime, daytrade, client = setup
    with_data = _kbars_for(set(DAYS))
    calls = iter(range(100))

    def kbars(code: str, start: date, end: date) -> dict[str, list]:
        if next(calls) % 2 == 0:
            raise ShioajiRequestError("暫時的錯誤")
        return with_data(code, start, end)

    client.kbars_data = kbars
    summary, _ = _run(setup)

    assert (summary.failed, summary.ok, summary.stopped) == (2, 2, "done")
    assert summary.remaining == 22 + 14                           # 失敗的是第 1、3 次（FIRST_CODE 的兩個區間），下次再查


@pytest.mark.parametrize("moment, allowed", [
    (datetime(2026, 11, 20, 8, 29), True),       # 週五開盤前
    (datetime(2026, 11, 20, 8, 30), False),      # 平日 08:30～13:40 不執行：盤中 kbars 有次數限制
    (datetime(2026, 11, 20, 13, 39), False),
    (datetime(2026, 11, 20, 13, 40), True),
    (datetime(2026, 11, 21, 10, 0), True),       # 週六
])
def test_backfill_refuses_to_run_during_market_hours(setup, moment: datetime, allowed: bool) -> None:
    config, realtime, daytrade, client = setup
    if allowed:
        _run(setup, now=lambda: moment, codes=[FIRST_CODE], start=date(2026, 11, 19))
        assert _queries(client) == [(FIRST_CODE, date(2026, 11, 19), date(2026, 11, 19))]
    else:
        with pytest.raises(JobFailed, match="平日 08:30～13:40 不回補"):
            _run(setup, now=lambda: moment)
        assert client.calls == []
        _run(setup, now=lambda: moment, dry_run=True, client_factory=None)       # --dry-run 不查詢，隨時可以


def test_login_failure_is_job_failure(setup) -> None:
    config, realtime, daytrade, client = setup
    client.login_error = "登入失敗：Sign data is timeout"

    with pytest.raises(JobFailed, match="Sign data is timeout"):
        _run(setup)
    assert _queries(client) == []


def test_interrupted_query_leaves_earlier_queries_and_resumes(setup) -> None:
    # 中斷（關機、Ctrl+C）時，已寫入的查詢保留，進行中的那次沒有寫入；重跑時從缺的股日繼續
    config, realtime, daytrade, client = setup
    with_data = _kbars_for(set(DAYS))

    def kbars(code: str, start: date, end: date) -> dict[str, list]:
        if code == SECOND_CODE:
            raise KeyboardInterrupt
        return with_data(code, start, end)

    client.kbars_data = kbars
    with pytest.raises(KeyboardInterrupt):
        _run(setup)
    with _store(setup) as store:
        assert set(store.fetched_days()) == {FIRST_CODE}
    assert client.logged_out

    client.kbars_data = with_data
    client.calls.clear()
    _run(setup)
    assert [code for code, _, _ in _queries(client)] == [SECOND_CODE, FIRST_CODE, SECOND_CODE]


def test_days_not_in_daily_bars_are_not_written(setup) -> None:
    # SECOND_CODE 在 SUSPENDED 沒有日 K，Shioaji 卻有 K 棒：不寫入，記在查詢紀錄的 note
    config, realtime, daytrade, client = setup
    summary, _ = _run(setup)

    with _store(setup) as store:
        assert store.bars(SECOND_CODE, SUSPENDED, SUSPENDED) == []
        notes = [r.note for r in store.fetch_log() if r.note]
    assert notes == ["不在日 K 的日子（沒有寫入）：2026-10-15"]


def test_missing_contract_skips_the_stock(setup) -> None:
    config, realtime, daytrade, client = setup
    del client.contracts[FIRST_CODE]

    summary, text = _run(setup)

    assert [code for code, _, _ in _queries(client)] == [SECOND_CODE, SECOND_CODE]
    assert summary.skipped_codes == [FIRST_CODE] and "合約查不到、本次略過：1303" in text


def test_dry_run_does_not_login_or_write(setup) -> None:
    config, realtime, daytrade, client = setup

    summary, text = _run(setup, dry_run=True, client_factory=None)

    assert client.calls == [] and not realtime.backfill.db_path.exists()
    assert "要查的股日 71" in text and "查詢 4 次" in text
    lines = text.split("\n")
    assert lines[lines.index("最先查的 4 次：") + 1] == f"  {FIRST_CODE} 2026-10-21～2026-11-19（22 個股日）"
    assert "沒有上市櫃日（screener.db 的 listings，先執行 events），從 2020-06-01 起：1303, 4977" in text


def test_days_before_listing_are_not_backfilled(setup) -> None:
    # SECOND_CODE 11-02 才上市櫃：之前的日 K 是興櫃交易，不回補（L25）；FIRST_CODE 是轉市場的股票，照常從 start 起
    config, realtime, daytrade, client = setup
    with SqlitePriceStore(config.settings.db_path) as store:
        store.upsert_listings([Listing(SECOND_CODE, Market.OTC, date(2026, 11, 2)),
                               Listing(FIRST_CODE, Market.TSE, date(2026, 11, 2))])
    realtime = replace(realtime, backfill=replace(realtime.backfill, market_transfers=(FIRST_CODE,)))

    summary, text = _run(setup, realtime=realtime)

    assert _queries(client) == [(FIRST_CODE, date(2026, 10, 21), date(2026, 11, 19)),
                                (SECOND_CODE, date(2026, 11, 2), date(2026, 11, 19)),
                                (FIRST_CODE, date(2026, 10, 1), date(2026, 10, 20))]
    assert summary.before_listing == 35 - 14 and summary.no_listing == []
    assert "上市櫃日之前（興櫃）的 21 個股日不回補（L25；轉市場的 1303 從 2020-06-01 起）" in text


def test_codes_start_end_limit_the_range(setup) -> None:
    config, realtime, daytrade, client = setup

    _run(setup, codes=[SECOND_CODE], start=date(2026, 11, 2), end=date(2026, 11, 6))

    assert _queries(client) == [(SECOND_CODE, date(2026, 11, 2), date(2026, 11, 6))]


def test_codes_outside_watchlist_are_rejected(setup) -> None:
    with pytest.raises(JobFailed, match="不在觀察清單的代號：2486"):
        _run(setup, codes=["2486"])


def test_case_stock_uses_screener_trading_days(setup) -> None:
    # 2486 不在觀察清單（L20）：交易日以 screener.db 的日期為準（觀察清單任一檔有日 K 的日子）
    config, realtime, daytrade, client = setup
    client.contracts["2486"] = FakeContract("2486", "一詮")
    case = replace(realtime.backfill.cases[0], start=date(2026, 10, 13), end=date(2026, 10, 16))
    realtime = replace(realtime, backfill=replace(realtime.backfill, cases=(case,)))

    _run(setup, realtime=realtime, case=True)

    assert _queries(client) == [("2486", date(2026, 10, 13), date(2026, 10, 16))]
    with _store(setup) as store:
        assert sorted(store.fetched_days()["2486"]) == [date(2026, 10, d) for d in (13, 14, 15, 16)]
    with pytest.raises(JobFailed, match="不在 backfill.cases 的代號"):
        _run(setup, realtime=realtime, case=True, codes=[FIRST_CODE])


def test_missing_screener_db_is_job_failure(setup) -> None:
    config, realtime, daytrade, client = setup
    Path(config.settings.db_path).unlink()

    with pytest.raises(JobFailed, match="找不到"):
        _run(setup)


# --- intraday.db --------------------------------------------------------------------

def test_save_query_is_one_transaction(tmp_path: Path) -> None:
    # 一次查詢的 K 棒、股日與查詢紀錄一起寫入：其中一根寫入失敗時，整次都不會留下（不會有半個區間）
    from screener.realtime.intraday import FetchRecord

    path = tmp_path / "intraday.db"
    good = Bar(datetime(2026, 11, 19, 9, 0), 1, 10.0, 10.0, 10.0, 10.0, 1, 10_000.0)
    bad = Bar(datetime(2026, 11, 19, 9, 1), 1, 10.0, 10.0, 10.0, 10.0, None, 10_000.0)   # type: ignore[arg-type]
    record = FetchRecord("A", date(2026, 11, 19), date(2026, 11, 19), datetime(2026, 11, 20, 14, 30), 1, 2, 0, 700,
                         "ok")
    with IntradayStore(path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.save_query(record, [good, bad], {date(2026, 11, 19): 2})
        assert store.fetched_days() == {} and store.bars("A") == [] and store.fetch_log() == []

        store.save_query(record, [good], {date(2026, 11, 19): 1})
        assert store.bars("A") == [good] and store.fetched_days() == {"A": {date(2026, 11, 19): 1}}
        assert store.fetch_log() == [record]


def test_readonly_store_requires_existing_db(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="先執行 rt-backfill"):
        IntradayStore(tmp_path / "intraday.db", readonly=True)
    assert not (tmp_path / "intraday.db").exists()


def test_day_bars_round_trip_with_prior_day_helper(tmp_path: Path) -> None:
    # bars(code, start, end) 依時間排序，日期含頭尾
    from screener.realtime.intraday import FetchRecord

    bars = [Bar(datetime.combine(day, time(9, 0)) + timedelta(minutes=m), 1, 10.0, 10.0, 10.0, 10.0, 1, 10_000.0)
            for day in DAYS[:3] for m in (1, 0)]
    with IntradayStore(tmp_path / "intraday.db") as store:
        store.save_query(FetchRecord("A", DAYS[0], DAYS[2], datetime(2026, 11, 20), 3, 6, 0, None, "ok"), bars,
                         {day: 2 for day in DAYS[:3]})
        result = store.bars("A", DAYS[1], DAYS[2])
    assert [bar.start for bar in result] == sorted(bar.start for bar in bars if bar.start.date() >= DAYS[1])
