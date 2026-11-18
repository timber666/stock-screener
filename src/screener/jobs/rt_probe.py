"""Shioaji 實測：python -m screener rt-probe（REALTIME_PLAN.md 7.1 第 3 點，L15）。

依序執行 T1–T7 的盤後部分，結果寫成一份報告（report.md、report.json 與原始資料的 CSV），存在
realtime.yaml 的 probe.output_dir 下以執行時間命名的目錄。T1（登入、拿到行情）失敗時停止，後面的項目不執行：
是否簽署文件、做 API 測試由使用者決定並自行操作（REALTIME_PLAN.md 第 3 節）。

| 項目 | 內容 |
|---|---|
| T1 | 正式環境登入、帳戶的簽署狀態、快照與歷史分 K |
| T2 | 流量：每次查詢前後的 usage |
| T3 | 合約：觀察清單是否都查得到、市場別、可否現股當沖、漲跌停價 |
| T4 | 快照：一次最多幾檔 |
| T5 | 歷史分 K：對照股 × 1 天、第一檔 × kbars_days 天；時間標示、量為 0 的 K 棒、金額、與日 K 比對 |
| T6 | 歷史逐筆成交：對照股 × 1 天 |
| T7 | 訂閱上限的盤後部分：逐一訂閱到 subscribe_limit 檔的成交，記錄錯誤與事件，最後全部取消 |

只讀取行情，不呼叫任何委託、帳務或憑證的功能。報告不寫入身分證字號、帳號、姓名與金鑰。
讀取 screener.db 的日 K 時以唯讀開啟（REALTIME_PLAN.md 4.2 規則 3）。
"""

import csv
import json
import logging
import sqlite3
import sys
import time as clock
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, TextIO, TypeVar

from screener.config import AppConfig
from screener.jobs import JobFailed
from screener.models import StockInfo
from screener.realtime.config import DaytradeConfig, RealtimeConfig, ShioajiKeys
from screener.realtime.history import parse_kbars, parse_ticks, summarize_kbars, summarize_ticks
from screener.realtime.shioaji_source import TICK, BIDASK, ShioajiClient, ShioajiRequestError, Usage
from screener.realtime.timestamps import from_shioaji_ts

logger = logging.getLogger(__name__)

PROBE_TESTS = ("T1", "T2", "T3", "T4", "T5", "T6", "T7")
SESSION_END = time(14, 0)        # 這個時間以後，預設的查詢日期才是今天（當天的歷史分 K 與逐筆成交已完整）
EVENT_WAIT_SEC = 3               # T7：訂閱與取消後，等待事件回傳的秒數

ClientFactory = Callable[[], ShioajiClient]
T = TypeVar("T")


@dataclass
class ProbeReport:
    started_at: datetime
    day: date
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)

    def section(self, test_id: str, title: str) -> dict[str, Any]:
        self.lines += ["", f"## {test_id} {title}", ""]
        return self.sections.setdefault(test_id, {"title": title})

    def say(self, text: str) -> None:
        self.lines.append(text)


@dataclass
class Probe:
    """一次實測的共用狀態。"""

    config: AppConfig
    realtime: RealtimeConfig
    daytrade: DaytradeConfig
    client: ShioajiClient
    report: ProbeReport
    out_dir: Path
    sleep: Callable[[float], None]
    traffic: list[dict[str, Any]] = field(default_factory=list)
    contracts: dict[str, Any] = field(default_factory=dict)       # 代號 → 合約（T3 之後）
    kbars_volume: dict[str, int] = field(default_factory=dict)    # 對照股查詢日的分 K 總量（T5），供 T6 比對

    def measure(self, label: str, call: Callable[[], T]) -> T:
        """執行一次查詢，記錄前後 usage 的差（T2）。

        usage 約每 5 秒才更新一次，所以查詢後每秒讀一次，到數字改變或 usage_settle_sec 為止。
        差值包含這段期間 usage 本身的流量（每次約 0.7 KB）；本機快取回應的重複查詢不佔流量，差值接近 0。
        """
        before = self.client.usage()
        result = call()
        after = before
        for _ in range(self.realtime.probe.usage_settle_sec):
            self.sleep(1)
            after = self.client.usage()
            if after.bytes != before.bytes:
                break
        self.traffic.append({"query": label, "bytes": after.bytes - before.bytes})
        return result

    def contract(self, code: str) -> Any | None:
        if code not in self.contracts:
            self.contracts[code] = self.client.stock(code)
        return self.contracts[code]


def run(config: AppConfig, realtime: RealtimeConfig, daytrade: DaytradeConfig, keys: ShioajiKeys, *,
        tests: Sequence[str] | None = None, day: date | None = None, client_factory: ClientFactory | None = None,
        out: TextIO | None = None, now: datetime | None = None,
        sleep: Callable[[float], None] = clock.sleep) -> Path:
    """回傳報告的目錄。tests 為 None 時執行全部；T1 一定會執行（後面的項目都需要登入）。"""
    now = now or datetime.now()
    day = day or default_probe_day(now)
    selected = list(PROBE_TESTS) if tests is None else ["T1", *(t for t in PROBE_TESTS if t in tests and t != "T1")]
    unknown = [t for t in tests or () if t not in PROBE_TESTS]
    if unknown:
        raise JobFailed(f"未知的實測項目：{', '.join(unknown)}（可用：{', '.join(PROBE_TESTS)}）")

    report = ProbeReport(started_at=now, day=day)
    report.lines += [f"# Shioaji 實測（{now:%Y-%m-%d %H:%M}）", "",
                     f"查詢日期：{day}；對照股：{', '.join(realtime.compare_codes)}；項目：{', '.join(selected)}"]
    out_dir = realtime.probe.output_dir / f"{now:%Y%m%d_%H%M%S}"
    client = (client_factory or (lambda: ShioajiClient(realtime.shioaji, keys)))()
    probe = Probe(config=config, realtime=realtime, daytrade=daytrade, client=client, report=report,
                  out_dir=out_dir, sleep=sleep)
    failed: str | None = None
    try:
        failed = _t1(probe)
        if failed is None:
            steps = {"T3": _t3, "T4": _t4, "T5": _t5, "T6": _t6, "T7": _t7}
            for test_id in selected:
                if test_id in steps:
                    steps[test_id](probe)
            if "T2" in selected:
                _t2(probe)
    finally:
        client.logout()
        _write(report, out_dir)
    (out or sys.stdout).write("\n".join(report.lines) + f"\n\n報告：{out_dir}\n")
    if failed:
        raise JobFailed(f"T1 未通過：{failed}（報告：{out_dir}）")
    return out_dir


def default_probe_day(now: datetime) -> date:
    """最近一個已收盤的平日：平日 14:00 以後為今天，否則為前一個平日。國定假日沒有資料，報告會顯示為空。"""
    day = now.date()
    if day.weekday() < 5 and now.time() >= SESSION_END:
        return day
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


# --- T1 -----------------------------------------------------------------------

def _t1(probe: Probe) -> str | None:
    """正式環境能否登入、拿到行情。回傳未通過的原因；通過時為 None。"""
    client, report, realtime = probe.client, probe.report, probe.realtime
    result = report.section("T1", "正式環境能否登入、拿到行情")
    result["simulation"] = realtime.shioaji.simulation
    try:
        accounts = client.connect()
    except ShioajiRequestError as exc:
        return _fail(result, report, str(exc))
    result["accounts"] = [asdict(account) for account in accounts]
    report.say(f"- 登入成功（simulation={realtime.shioaji.simulation}）；帳戶 {len(accounts)} 個："
               + "、".join(f"{a.account_type}（signed={a.signed}）" for a in accounts))
    try:
        usage = client.usage()
        result["usage_after_login"] = asdict(usage)
        report.say(f"- 登入後的流量：{_usage_text(usage)}")
        result["stock_contracts"] = client.contract_count()
        report.say(f"- 合約：登入時自動下載，股票 {result['stock_contracts']} 檔（含興櫃）")

        code = realtime.compare_codes[0]
        contract = probe.contract(code)
        if contract is None:
            return _fail(result, report, f"合約查不到 {code}")
        result["contract"] = _public_fields(contract)
        snapshots = probe.measure(f"T1 snapshots {code}", lambda: client.snapshots([contract]))
        if not snapshots:
            return _fail(result, report, f"快照沒有回傳 {code} 的資料")
        snap = result["snapshot"] = snapshots[0]
        report.say(f"- 快照 {code}：收盤 {snap.get('close')}、總量 {snap.get('total_volume')}、"
                   f"時間 {_ts_text(snap.get('ts'))}")
        kbars = probe.measure(f"T1 kbars {code} 1 天", lambda: client.kbars(contract, report.day, report.day))
        count = result["kbars_count"] = len(kbars.get("ts", []))
        if count == 0:
            return _fail(result, report, f"歷史分 K 沒有資料（{code} {report.day}，可能是休市日）")
        report.say(f"- 歷史分 K {code} {report.day}：{count} 根，"
                   f"{_ts_text(kbars['ts'][0])}～{_ts_text(kbars['ts'][-1])}")
    except ShioajiRequestError as exc:
        return _fail(result, report, str(exc))
    result["passed"] = True
    report.say("- ✅ 通過：正式環境可以拿到快照與歷史分 K")
    return None


def _fail(result: dict[str, Any], report: ProbeReport, reason: str) -> str:
    result["passed"] = False
    result["error"] = reason
    report.say(f"- ❌ {reason}")
    return reason


# --- T2 -----------------------------------------------------------------------

def _t2(probe: Probe) -> None:
    result = probe.report.section("T2", "流量（每次查詢前後的 usage）")
    usage = probe.client.usage()
    result["usage_end"] = asdict(usage)
    result["queries"] = probe.traffic
    probe.report.say(f"- 實測結束時：{_usage_text(usage)}")
    if not probe.traffic:
        probe.report.say("- 沒有執行任何查詢")
        return
    probe.report.say("")
    probe.report.say("| 查詢 | 流量 |")
    probe.report.say("|---|---|")
    for item in probe.traffic:
        probe.report.say(f"| {item['query']} | {_kb(item['bytes'])} |")


# --- T3 -----------------------------------------------------------------------

def _t3(probe: Probe) -> None:
    report = probe.report
    result = report.section("T3", "合約：觀察清單、市場別、可否現股當沖、漲跌停價")
    rows = [_contract_row(stock, probe.contract(stock.code)) for stock in probe.config.watchlist.stocks]
    _write_rows(probe.out_dir / "T3_contracts.csv", rows)
    found = [row for row in rows if row["found"]]
    missing = [row["code"] for row in rows if not row["found"]]
    market_mismatch = [row["code"] for row in found if row["exchange"] != row["market"].upper()]
    name_mismatch = [f"{row['code']}（{row['name']}／{row['sj_name']}）" for row in found
                     if row["sj_name"] != row["name"]]
    day_trade = Counter(row["day_trade"] for row in found)
    not_yes = [f"{row['code']}（{row['day_trade']}）" for row in found if row["day_trade"] != "Yes"]
    with_limits = sum(1 for row in found if row["limit_up"] and row["limit_down"] and row["reference"])
    suspended = [row["code"] for row in found if row["trading_suspended"] == "True"]
    result.update(watchlist=len(rows), found=len(found), missing=missing, market_mismatch=market_mismatch,
                  name_mismatch=name_mismatch, day_trade=dict(day_trade), not_day_tradable=not_yes,
                  with_limits=with_limits, suspended=suspended,
                  fields=sorted(_public_fields(found and probe.contract(found[0]["code"]) or object()).keys()))
    report.say(f"- 觀察清單 {len(rows)} 檔，查得到 {len(found)} 檔" + (f"；查不到：{', '.join(missing)}" if missing else ""))
    report.say(f"- 市場別與 watchlist.yaml 不同：{', '.join(market_mismatch) or '無'}")
    report.say(f"- 名稱不同：{', '.join(name_mismatch) or '無'}")
    report.say(f"- 可否現股當沖（day_trade）：{dict(day_trade)}；不是 Yes 的：{', '.join(not_yes) or '無'}")
    report.say(f"- 有參考價與漲跌停價（reference、limit_up、limit_down）：{with_limits} 檔")
    report.say(f"- 暫停交易（trading_suspended）：{', '.join(suspended) or '無'}")
    report.say(f"- 合約欄位：{', '.join(result['fields'])}")


def _contract_row(stock: StockInfo, contract: Any | None) -> dict[str, Any]:
    row: dict[str, Any] = {"code": stock.code, "name": stock.name, "market": stock.market.value,
                           "found": contract is not None}
    if contract is None:
        return row
    fields = _public_fields(contract)
    row.update(sj_name=fields.get("name"), exchange=_enum_text(getattr(contract, "exchange", "")),
               day_trade=_enum_text(getattr(contract, "day_trade", "")), reference=fields.get("reference"),
               limit_up=fields.get("limit_up"), limit_down=fields.get("limit_down"),
               trading_suspended=str(fields.get("trading_suspended")), category=fields.get("category"),
               disposition_level=fields.get("disposition_level"), attention_flag=fields.get("attention_flag"),
               update_date=fields.get("update_date"))
    return row


# --- T4 -----------------------------------------------------------------------

def _t4(probe: Probe) -> None:
    report = probe.report
    result = report.section("T4", "快照：一次最多幾檔")
    contracts = [contract for stock in probe.config.watchlist.stocks
                 if (contract := probe.contract(stock.code)) is not None]
    batches = []
    for size in probe.realtime.probe.snapshot_batches:
        size = min(size, len(contracts))
        batch = contracts[:size]
        try:
            snapshots = probe.measure(f"T4 snapshots {size} 檔", lambda: probe.client.snapshots(batch))
        except ShioajiRequestError as exc:
            batches.append({"size": size, "error": str(exc)})
            report.say(f"- {size} 檔：❌ {exc}")
            continue
        returned = {snap.get("code") for snap in snapshots}
        missing = [contract.code for contract in batch if contract.code not in returned]
        batches.append({"size": size, "returned": len(snapshots), "missing": missing})
        report.say(f"- {size} 檔：回傳 {len(snapshots)} 筆" + (f"，缺 {', '.join(missing)}" if missing else ""))
        if "fields" not in result and snapshots:
            result["fields"] = sorted(snapshots[0].keys())
            result["sample"] = snapshots[0]
    result["batches"] = batches
    if "fields" in result:
        report.say(f"- 快照欄位：{', '.join(result['fields'])}")


# --- T5 -----------------------------------------------------------------------

def _t5(probe: Probe) -> None:
    report, day, session = probe.report, probe.report.day, probe.daytrade.session
    result = report.section("T5", "歷史分 K")
    closing_labels = (_add_minutes(session.closing_call, 1), _add_minutes(session.close, -1))
    one_day: dict[str, Any] = {}
    for code in probe.realtime.compare_codes:
        contract = probe.contract(code)
        if contract is None:
            report.say(f"- {code}：合約查不到")
            continue
        try:
            columns = probe.measure(f"T5 kbars {code} 1 天", lambda: probe.client.kbars(contract, day, day))
        except ShioajiRequestError as exc:
            report.say(f"- {code}：❌ {exc}")
            continue
        _write_columns(probe.out_dir / f"T5_kbars_{code}_{day}.csv", columns)
        summary = summarize_kbars(parse_kbars(columns), closing_labels).get(day)
        if summary is None:
            report.say(f"- {code} {day}：沒有資料")
            continue
        summary["daily_k"] = _compare_daily(probe, code, day, summary)
        one_day[code] = summary
        probe.kbars_volume[code] = summary["volume"]
        report.say(f"- {code} {day}：{summary['count']} 根，{summary['first']}～{summary['last']}；量為 0 的 "
                   f"{summary['zero_volume']} 根（收盤集合競價 {summary['zero_volume_closing_call']} 根，"
                   f"盤中 {', '.join(summary['zero_volume_intraday']) or '無'}）；最後一根 {summary['last']} 的量 "
                   f"{summary['last_bar_volume']} 張；總量 {summary['volume']:,} 張、金額 {summary['amount']:,.0f} 元；"
                   f"日 K：{summary['daily_k']}")
    result["one_day"] = one_day

    code = probe.realtime.compare_codes[0]
    contract = probe.contract(code)
    days = probe.realtime.probe.kbars_days
    start = day - timedelta(days=days - 1)
    if contract is None:
        return
    try:
        columns = probe.measure(f"T5 kbars {code} {days} 天", lambda: probe.client.kbars(contract, start, day))
    except ShioajiRequestError as exc:
        report.say(f"- {code} {start}～{day}：❌ {exc}")
        return
    _write_columns(probe.out_dir / f"T5_kbars_{code}_{start}_{day}.csv", columns)
    per_day = summarize_kbars(parse_kbars(columns), closing_labels)
    rows = []
    for each_day, summary in per_day.items():
        summary["daily_k"] = _compare_daily(probe, code, each_day, summary)
        rows.append({"day": each_day.isoformat(), **summary})
    result["multi_day"] = {"code": code, "start": start.isoformat(), "end": day.isoformat(), "days": rows,
                           "bytes": probe.traffic[-1]["bytes"]}
    counts = Counter(row["count"] for row in rows)
    report.say(f"- {code} {start}～{day}（{days} 個日曆天）：{len(rows)} 個交易日，每天的根數 {dict(counts)}；"
               f"第一根 {sorted({row['first'] for row in rows})}、最後一根 {sorted({row['last'] for row in rows})}；"
               f"流量 {_kb(probe.traffic[-1]['bytes'])}")
    report.say("")
    report.say("| 日期 | 根數 | 量為 0（盤中） | 分 K 總量 | 日 K |")
    report.say("|---|---|---|---|---|")
    for row in rows:
        report.say(f"| {row['day']} | {row['count']} | {len(row['zero_volume_intraday'])} | {row['volume']:,} | "
                   f"{row['daily_k']} |")


def _compare_daily(probe: Probe, code: str, day: date, summary: dict[str, Any]) -> str:
    """分 K 合起來和 screener.db 的日 K 比對（唯讀）。"""
    daily = _daily_k(probe.config.settings.db_path, code, day)
    if daily is None:
        return "資料庫沒有這天"
    o, h, lo, c, volume = daily
    same = [name for name, a, b in (("開", summary["open"], o), ("高", summary["high"], h),
                                    ("低", summary["low"], lo), ("收", summary["close"], c)) if a == b]
    different = [f"{name} {a}／{b}" for name, a, b in (("開", summary["open"], o), ("高", summary["high"], h),
                                                       ("低", summary["low"], lo), ("收", summary["close"], c))
                 if a != b]
    ratio = summary["volume"] / volume if volume else float("nan")
    text = "開高低收相同" if len(same) == 4 else f"不同：{'、'.join(different)}"
    return f"{text}；量 {summary['volume']:,}／{volume:,} 張（{ratio:.1%}）"


def _daily_k(db_path: Path, code: str, day: date) -> tuple[float, float, float, float, int] | None:
    if not db_path.is_file():
        return None
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute("SELECT open, high, low, close, volume FROM daily_prices WHERE code = ? AND date = ?",
                           (code, day.isoformat())).fetchone()
    return tuple(row) if row else None


# --- T6 -----------------------------------------------------------------------

def _t6(probe: Probe) -> None:
    report, day, session = probe.report, probe.report.day, probe.daytrade.session
    result = report.section("T6", "歷史逐筆成交")
    for code in probe.realtime.compare_codes:
        contract = probe.contract(code)
        if contract is None:
            report.say(f"- {code}：合約查不到")
            continue
        try:
            columns = probe.measure(f"T6 ticks {code} 1 天", lambda: probe.client.ticks(contract, day))
        except ShioajiRequestError as exc:
            report.say(f"- {code}：❌ {exc}")
            continue
        _write_columns(probe.out_dir / f"T6_ticks_{code}_{day}.csv", columns)
        summary = summarize_ticks(parse_ticks(columns), session.open, session.closing_call, session.close)
        summary["fields"] = sorted(columns.keys())
        summary["bytes"] = probe.traffic[-1]["bytes"]
        daily = _daily_k(probe.config.settings.db_path, code, day)
        summary["kbars_volume"] = probe.kbars_volume.get(code)
        summary["daily_volume"] = daily[4] if daily else None
        result[code] = summary
        if summary["count"] == 0:
            report.say(f"- {code} {day}：沒有資料")
            continue
        report.say(f"- {code} {day}：{summary['count']:,} 筆，{summary['first']}～{summary['last']}（有微秒的 "
                   f"{summary['with_microseconds']:,} 筆）；開盤前 {summary['before_open']}、開盤那一秒 "
                   f"{summary['at_open_second']}、收盤集合競價期間 {summary['closing_call']}、收盤時間以後 "
                   f"{summary['at_or_after_close']} 筆；量為 0 的 {summary['zero_volume']} 筆；總量 "
                   f"{summary['volume']:,} 張（分 K {summary['kbars_volume'] or '-'}、日 K {summary['daily_volume'] or '-'}）"
                   f"；內外盤 {summary['tick_type']}；流量 {_kb(summary['bytes'])}")
        report.say(f"  - 最前 3 筆 {summary['first_ticks']}；最後 3 筆 {summary['last_ticks']}")
    fields = next((item["fields"] for item in result.values() if isinstance(item, dict) and "fields" in item), None)
    if fields:
        report.say(f"- 欄位：{', '.join(fields)}（沒有試撮的標示）" if "simtrade" not in fields
                   else f"- 欄位：{', '.join(fields)}")


# --- T7 -----------------------------------------------------------------------

def _t7(probe: Probe) -> None:
    report, client = probe.report, probe.client
    settings = probe.realtime.probe
    result = report.section("T7", f"訂閱上限（盤後：逐一訂閱到 {settings.subscribe_limit} 檔的成交）")
    events: list[dict[str, Any]] = []
    client.on_event(lambda code, event_code, info, event: events.append(
        {"at": datetime.now().isoformat(timespec="milliseconds"), "code": code, "event_code": event_code,
         "event": event, "info": info}))
    contracts = [contract for stock in probe.config.watchlist.stocks
                 if (contract := probe.contract(stock.code)) is not None][:settings.subscribe_limit]
    subscribed: list[tuple[Any, str]] = []
    errors: list[dict[str, Any]] = []
    try:
        for index, contract in enumerate(contracts, start=1):
            try:
                client.subscribe(contract, TICK)
                subscribed.append((contract, TICK))
            except Exception as exc:
                errors.append({"n": index, "code": contract.code, "kind": TICK, "error": f"{type(exc).__name__}: {exc}"})
            probe.sleep(settings.subscribe_interval_sec)
        if contracts:                           # 同一檔再訂五檔：是否另算一個
            try:
                client.subscribe(contracts[0], BIDASK)
                subscribed.append((contracts[0], BIDASK))
            except Exception as exc:
                errors.append({"n": len(contracts) + 1, "code": contracts[0].code, "kind": BIDASK,
                               "error": f"{type(exc).__name__}: {exc}"})
        probe.sleep(EVENT_WAIT_SEC)
        subscribe_events = list(events)
    finally:
        unsubscribe_errors = 0
        for contract, kind in reversed(subscribed):
            try:
                client.unsubscribe(contract, kind)
            except Exception:
                unsubscribe_errors += 1
        probe.sleep(EVENT_WAIT_SEC)
    kinds = Counter((item["code"], item["event"]) for item in subscribe_events)
    result.update(attempted=len(contracts) + (1 if contracts else 0), subscribed=len(subscribed), errors=errors,
                  unsubscribe_errors=unsubscribe_errors, events=events)
    report.say(f"- 嘗試 {len(contracts)} 檔成交＋1 檔五檔，成功 {len(subscribed)} 個；拋出錯誤的 {len(errors)} 個"
               + (f"，第一個在第 {errors[0]['n']} 個：{errors[0]['error']}" if errors else ""))
    report.say(f"- 訂閱期間的事件（回應碼, 事件）：{dict(kinds)}")
    failures = [item for item in subscribe_events if item["code"] not in (0, 200)]
    if failures:
        report.say(f"- 非成功的事件 {len(failures)} 個，第一個：{failures[0]['code']} {failures[0]['event']}"
                   f"（{failures[0]['info']}）")
    report.say(f"- 取消訂閱失敗：{unsubscribe_errors} 個；推送與 2 個連線的部分要盤中測試（rt-record）")


# --- 輸出 -----------------------------------------------------------------------

def _write(report: ProbeReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text("\n".join(report.lines) + "\n", encoding="utf-8")
    payload = {"started_at": report.started_at.isoformat(), "day": report.day.isoformat(), **report.sections}
    (out_dir / "report.json").write_text(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    logger.info("實測報告寫入 %s", out_dir)


def _write_columns(path: Path, columns: dict[str, list[Any]]) -> None:
    """kbars、ticks 這類「欄位 → 清單」的資料寫成 CSV，另加 time 欄（ts 換算成的本地時間）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(columns)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        has_ts = "ts" in columns
        writer.writerow(["time", *names] if has_ts else names)
        for row in zip(*(columns[name] for name in names)):
            writer.writerow([_ts_text(row[names.index("ts")]), *row] if has_ts else row)


def _write_rows(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for row in rows:
        names += [name for name in row if name not in names]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)


def _public_fields(obj: Any) -> dict[str, Any]:
    """合約等物件的公開欄位（不含方法）。"""
    if hasattr(obj, "dict"):
        try:
            return {key: _plain(value) for key, value in dict(obj.dict()).items()}
        except Exception:                      # 有些型別的 dict() 不能用，改逐欄讀取
            pass
    fields: dict[str, Any] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if not callable(value):
            fields[name] = _plain(value)
    return fields


def _plain(value: Any) -> Any:
    # 用 type() 判斷：Shioaji 的列舉（例：Currency）是 str 的子類別，json 無法直接輸出
    if value is None or type(value) in (str, int, float, bool):
        return value
    return str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return _plain(value)


def _enum_text(value: Any) -> str:
    """Shioaji 列舉的名稱，例：DayTrade.Yes → Yes、Exchange.TSE → TSE。"""
    value = getattr(value, "value", value)
    return str(value).split(".")[-1]


def _ts_text(ts: Any) -> str:
    if ts is None:
        return "-"
    try:
        return from_shioaji_ts(int(ts)).isoformat(sep=" ")
    except (TypeError, ValueError, OverflowError):
        return str(ts)


def _add_minutes(value: time, minutes: int) -> time:
    return (datetime.combine(date.min, value) + timedelta(minutes=minutes)).time()


def _usage_text(usage: Usage) -> str:
    return (f"已用 {_mb(usage.bytes)}／上限 {_mb(usage.limit_bytes)}（剩 {_mb(usage.remaining_bytes)}），"
            f"連線 {usage.connections} 個")


def _kb(value: int) -> str:
    return f"{value / 1024:,.1f} KB"


def _mb(value: int) -> str:
    return f"{value / 1024 / 1024:,.1f} MB"
