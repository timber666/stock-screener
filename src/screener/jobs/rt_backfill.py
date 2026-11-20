"""回補歷史 1 分 K：python -m screener rt-backfill（REALTIME_PLAN.md 7.2 第 3 點，L18–L20、L24）。

觀察清單每檔從 backfill.start（2020-06-01）或上市櫃日起（較晚者；上市櫃日之前是興櫃交易，轉市場的股票例外，L25），
到 screener.db 的最後一天：要查的股日 = 該檔在 screener.db 有日 K 的日子，減去 intraday.db 已經查過的股日。
上市櫃日取 screener.db 的 listings（events 指令寫入）。由新到舊查詢，每次一檔、最多 max_query_days 個日曆天。

- 每次查詢前讀 usage()，當天的已用量到 traffic_stop_mb 就停止，下次從缺的股日繼續。超過上限時 Shioaji 回傳空值、
  不報錯，所以只有在用量低於停止點時拿到的空值才記為「沒有資料」。
- 轉換：history.kbars_to_bars（標示減 1 分鐘、13:30 維持、刪除量為 0 的 K 棒，M12 驗證過）。
- 每次查詢的結果在同一個交易內寫入 intraday.db（realtime.intraday）。
- 連續 max_failures 次查詢失敗或登入失敗時停止，結束碼 1；不推播（Q7 未定）。
- --case：只回補不在觀察清單的案例股（backfill.cases，L20），交易日以 screener.db 的日期為準。
- --dry-run：只列出要查的區間與預估流量，不登入、不查詢、不寫入。
"""

import logging
import math
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

from screener.config import AppConfig
from screener.jobs import JobFailed
from screener.realtime.backfill import Chunk, estimate, first_day, missing_days, plan_chunks, select_chunk
from screener.realtime.config import DaytradeConfig, RealtimeConfig, ShioajiKeys
from screener.realtime.history import kbars_to_bars, parse_kbars
from screener.realtime.intraday import EMPTY, FAILED, OK, FetchRecord, IntradayStore
from screener.realtime.shioaji_source import ShioajiClient, ShioajiRequestError

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], ShioajiClient]

LISTED_CHUNKS = 5                # --dry-run 列出最先與最後查的幾次
EXTRA_DAYS_LISTED = 5            # 查詢紀錄的 note 最多列出幾個不在日 K 的日子

DONE = "done"                    # 要查的股日都查完了
TRAFFIC = "traffic"              # 流量到停止點
FAILURES = "failures"            # 連續查詢失敗
_STOP_TEXT = {DONE: "要查的股日都查完了", TRAFFIC: "流量到停止點", FAILURES: "連續查詢失敗"}


@dataclass
class BackfillSummary:
    stocks: int
    first_day: date | None
    last_day: date | None
    wanted: int                                  # 範圍內 screener.db 的股日
    missing: int                                 # 開始時還沒查過的股日
    before_listing: int = 0                      # 上市櫃日之前（興櫃）、不回補的股日（L25）
    no_listing: list[str] = field(default_factory=list)      # listings 沒有上市櫃日、從 start 起的股票
    queries: int = 0
    ok: int = 0
    empty: int = 0
    failed: int = 0
    recorded: int = 0                            # 寫入的股日（含沒有資料）
    no_data: int = 0                             # 寫入的股日中沒有資料的
    bars: int = 0
    usage_start: int | None = None
    usage_end: int | None = None
    stopped: str = DONE
    skipped_codes: list[str] = field(default_factory=list)   # 合約查不到
    last_error: str | None = None

    @property
    def remaining(self) -> int:
        return self.missing - self.recorded

    @property
    def used_bytes(self) -> int | None:
        if self.usage_start is None or self.usage_end is None:
            return None
        return max(self.usage_end - self.usage_start, 0)


def run(config: AppConfig, realtime: RealtimeConfig, daytrade: DaytradeConfig, keys: ShioajiKeys | None, *,
        codes: Sequence[str] | None = None, start: date | None = None, end: date | None = None, case: bool = False,
        dry_run: bool = False, client_factory: ClientFactory | None = None, out: TextIO | None = None,
        now: Callable[[], datetime] = datetime.now) -> BackfillSummary:
    out = out or sys.stdout
    settings = realtime.backfill
    daily, listings = read_screener(config.settings.db_path)
    targets = _targets(config, realtime, daily, listings, codes=codes, start=start, end=end, case=case)
    fetched = _fetched(settings.db_path, create=not dry_run)
    missing = missing_days(targets.days, fetched)
    chunks = plan_chunks(missing, settings.max_query_days, targets.order)
    all_days = sorted({day for days in targets.days.values() for day in days})
    summary = BackfillSummary(stocks=len(targets.days), first_day=all_days[0] if all_days else None,
                              last_day=all_days[-1] if all_days else None,
                              wanted=sum(len(days) for days in targets.days.values()),
                              missing=sum(len(days) for days in missing.values()),
                              before_listing=targets.before_listing, no_listing=targets.no_listing)
    if targets.no_listing:
        logger.warning("rt-backfill：screener.db 的 listings 沒有上市櫃日（先執行 events），從 %s 起：%s",
                       settings.start, ", ".join(targets.no_listing))
    scope = "案例股" if case else f"觀察清單 {len(targets.days)} 檔"
    if dry_run:
        text = _dry_run_text(summary, scope, chunks, realtime)
        out.write(text)
        logger.info("rt-backfill --dry-run：%s", text.replace("\n", "；"))
        return summary
    if not chunks:
        out.write(f"rt-backfill（{scope}）：要查的股日都查過了\n")
        logger.info("rt-backfill：要查的股日都查過了")
        return summary
    started = now()
    blocked_from, blocked_to = settings.blocked_hours
    if started.weekday() < 5 and blocked_from <= started.time() < blocked_to:
        raise JobFailed(f"平日 {blocked_from:%H:%M}～{blocked_to:%H:%M} 不回補：盤中查 kbars 最多 270 次，多次超過可能暫停帳號"
                        f"（REALTIME_PLAN.md 第 3 節）；請在 {blocked_to:%H:%M} 以後執行，或先用 --dry-run 查看")
    if keys is None:
        raise JobFailed("沒有 Shioaji 的金鑰")

    known_days = daily if not case else {code: set().union(*daily.values()) for code in targets.days}
    client = (client_factory or (lambda: ShioajiClient(realtime.shioaji, keys)))()
    with IntradayStore(settings.db_path) as store:
        try:
            client.connect()
        except ShioajiRequestError as exc:
            raise JobFailed(str(exc)) from exc
        try:
            _fetch(client, store, chunks, summary, realtime, daytrade, known_days, now)
        finally:
            client.logout()

    text = _summary_text(summary, scope, realtime)
    out.write(text)
    logger.info("rt-backfill：%s", text.replace("\n", "；"))
    if summary.stopped == FAILURES:
        raise JobFailed(f"連續 {settings.max_failures} 次查詢失敗，停止：{summary.last_error}")
    return summary


# --- 要查的股日 ------------------------------------------------------------------

def read_screener(db_path: Path) -> tuple[dict[str, set[date]], dict[str, date]]:
    """screener.db（唯讀）：每檔有日 K 的日子（日 K 與交易日都以它為準，L18），以及 listings 的上市櫃日（L25）。"""
    if not db_path.is_file():
        raise JobFailed(f"找不到 {db_path}：回補要查的股日以 screener.db 的日 K 為準")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    daily: dict[str, set[date]] = {}
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        for code, day in conn.execute("SELECT code, date FROM daily_prices"):
            daily.setdefault(code, set()).add(date.fromisoformat(day))
        listings = {code: date.fromisoformat(day) for code, day in conn.execute("SELECT code, listed_on FROM listings")}
    return daily, listings


@dataclass(frozen=True)
class Targets:
    days: dict[str, list[date]]     # 每檔要回補的日子（還沒扣掉已查過的）
    order: list[str]                # 查詢的順序（同一個迄日）
    before_listing: int             # 上市櫃日之前、不回補的股日
    no_listing: list[str]           # 沒有上市櫃日、從 start 起的股票


def _targets(config: AppConfig, realtime: RealtimeConfig, daily: Mapping[str, set[date]],
             listings: Mapping[str, date], *, codes: Sequence[str] | None, start: date | None, end: date | None,
             case: bool) -> Targets:
    settings = realtime.backfill
    if case:
        cases = {item.code: item for item in settings.cases}
        selected = list(codes or cases)
        unknown = [code for code in selected if code not in cases]
        if unknown:
            raise JobFailed(f"不在 backfill.cases 的代號：{', '.join(unknown)}")
        trading_days = set().union(*daily.values()) if daily else set()
        days = {code: _within(trading_days, max(cases[code].start, start or date.min),
                              min(cases[code].end, end or date.max)) for code in selected}
        return Targets(days=days, order=selected, before_listing=0, no_listing=[])

    watchlist = [stock.code for stock in config.watchlist.stocks]
    selected = list(codes or watchlist)
    unknown = [code for code in selected if code not in watchlist]
    if unknown:
        raise JobFailed(f"不在觀察清單的代號：{', '.join(unknown)}")
    first = max(settings.start, start or settings.start)
    last = end or date.max
    days = {}
    before_listing = 0
    for code in selected:
        begin = max(first, first_day(code, settings.start, listings.get(code), settings.market_transfers))
        days[code] = _within(daily.get(code, set()), begin, last)
        before_listing += len(_within(daily.get(code, set()), first, last)) - len(days[code])
    no_listing = [code for code in selected if code not in listings and daily.get(code)]
    return Targets(days={code: d for code, d in days.items() if d}, order=selected, before_listing=before_listing,
                   no_listing=no_listing)


def _within(days: set[date], first: date, last: date) -> list[date]:
    return sorted(day for day in days if first <= day <= last)


def _fetched(db_path: Path, *, create: bool) -> dict[str, dict[date, int]]:
    if not create and not db_path.is_file():
        return {}
    with IntradayStore(db_path, readonly=not create) as store:
        return store.fetched_days()


# --- 查詢 -----------------------------------------------------------------------

def _fetch(client: ShioajiClient, store: IntradayStore, chunks: Sequence[Chunk], summary: BackfillSummary,
           realtime: RealtimeConfig, daytrade: DaytradeConfig, known_days: Mapping[str, set[date]],
           now: Callable[[], datetime]) -> None:
    settings = realtime.backfill
    stop_bytes = settings.traffic_stop_bytes
    failures = 0
    skipped: set[str] = set()
    for index, chunk in enumerate(chunks, 1):
        if chunk.code in skipped:
            continue
        usage = client.usage()
        if summary.usage_start is None:
            summary.usage_start = usage.bytes
        summary.usage_end = usage.bytes
        if usage.bytes >= stop_bytes:
            summary.stopped = TRAFFIC
            return
        if index > 1 and (index - 1) % settings.progress_every == 0:
            logger.info("rt-backfill：第 %d／%d 次查詢（%s %s～%s），今天已用 %.1f MB", index, len(chunks), chunk.code,
                        chunk.start, chunk.end, usage.bytes / 1024 / 1024)

        contract = client.stock(chunk.code)
        if contract is None:
            skipped.add(chunk.code)
            summary.skipped_codes.append(chunk.code)
            logger.warning("rt-backfill：合約查不到 %s，本次略過這一檔", chunk.code)
            store.log_failure(_record(chunk, now(), usage.bytes, FAILED, note="合約查不到"))
            continue

        fetched_at = now()
        summary.queries += 1
        try:
            columns = client.kbars(contract, chunk.start, chunk.end)
        except ShioajiRequestError as exc:
            failures += 1
            summary.failed += 1
            summary.last_error = str(exc)
            logger.warning("rt-backfill：%s %s～%s 查詢失敗（連續第 %d 次）：%s", chunk.code, chunk.start, chunk.end,
                           failures, exc)
            store.log_failure(_record(chunk, fetched_at, usage.bytes, FAILED, note=str(exc)))
            if failures >= settings.max_failures:
                summary.stopped = FAILURES
                return
            continue
        failures = 0

        rows = parse_kbars(columns)
        if not rows and client.usage().bytes >= stop_bytes:   # 超過上限時回傳空值：不能當成沒有資料
            summary.queries -= 1
            summary.stopped = TRAFFIC
            return
        result = select_chunk(kbars_to_bars(rows, daytrade.session), chunk)
        extra = [day for day in result.extra_days if day not in known_days.get(chunk.code, set())]
        note = None
        if extra:
            note = "不在日 K 的日子（沒有寫入）：" + "、".join(day.isoformat() for day in extra[:EXTRA_DAYS_LISTED]) + (
                f" 等 {len(extra)} 天" if len(extra) > EXTRA_DAYS_LISTED else "")
            logger.warning("rt-backfill：%s %s", chunk.code, note)
        status = OK if result.bars else EMPTY
        zero_bars = sum(1 for row in rows if row.volume == 0)
        store.save_query(_record(chunk, fetched_at, usage.bytes, status, bars=len(result.bars), zero_bars=zero_bars,
                                 note=note), result.bars, result.counts)
        if status == OK:
            summary.ok += 1
        else:
            summary.empty += 1
        summary.recorded += len(chunk.days)
        summary.no_data += sum(1 for count in result.counts.values() if count == 0)
        summary.bars += len(result.bars)
    summary.usage_end = client.usage().bytes


def _record(chunk: Chunk, fetched_at: datetime, usage_bytes: int, status: str, *, bars: int = 0, zero_bars: int = 0,
            note: str | None = None) -> FetchRecord:
    return FetchRecord(code=chunk.code, start=chunk.start, end=chunk.end, fetched_at=fetched_at, days=len(chunk.days),
                       bars=bars, zero_bars=zero_bars, usage_bytes=usage_bytes, status=status, note=note)


# --- 輸出 -----------------------------------------------------------------------

def _range_text(summary: BackfillSummary) -> str:
    if summary.first_day is None:
        return "screener.db 沒有範圍內的日 K"
    return f"{summary.first_day}～{summary.last_day}"


def _dry_run_text(summary: BackfillSummary, scope: str, chunks: Sequence[Chunk], realtime: RealtimeConfig) -> str:
    settings = realtime.backfill
    est = estimate(chunks, settings.bytes_per_stock_day, settings.traffic_stop_bytes)
    lines = [
        "rt-backfill --dry-run（不登入、不查詢、不寫入）",
        f"範圍：{scope}，{_range_text(summary)}",
        f"要查的股日 {_n(summary.missing)}（範圍內 screener.db 的股日 {_n(summary.wanted)}，"
        f"已查過 {_n(summary.wanted - summary.missing)}）",
        *_listing_lines(summary, realtime),
        f"查詢 {_n(est.queries)} 次：每次一檔、最多 {settings.max_query_days} 個日曆天，由新到舊",
        f"預估流量 {_size(est.bytes)}（每個股日 {settings.bytes_per_stock_day / 1000:.1f} KB）；每天用到 "
        f"{settings.traffic_stop_mb:g} MB 停止，約 {_n(est.queries_per_day)} 次查詢、"
        f"{est.trading_days} 個交易日補完",
    ]
    if chunks:
        lines.append(f"最先查的 {min(LISTED_CHUNKS, len(chunks))} 次：")
        lines += [f"  {_chunk_text(chunk)}" for chunk in chunks[:LISTED_CHUNKS]]
        if len(chunks) > LISTED_CHUNKS:
            lines.append(f"最後查的 {min(LISTED_CHUNKS, len(chunks) - LISTED_CHUNKS)} 次：")
            lines += [f"  {_chunk_text(chunk)}" for chunk in chunks[-min(LISTED_CHUNKS, len(chunks) - LISTED_CHUNKS):]]
    return "\n".join(lines) + "\n"


def _listing_lines(summary: BackfillSummary, realtime: RealtimeConfig) -> list[str]:
    lines = []
    if summary.before_listing:
        transfers = "、".join(realtime.backfill.market_transfers)
        lines.append(f"上市櫃日之前（興櫃）的 {_n(summary.before_listing)} 個股日不回補（L25"
                     + (f"；轉市場的 {transfers} 從 {realtime.backfill.start} 起" if transfers else "") + "）")
    if summary.no_listing:
        lines.append(f"⚠️ 沒有上市櫃日（screener.db 的 listings，先執行 events），從 {realtime.backfill.start} 起："
                     f"{', '.join(summary.no_listing)}")
    return lines


def _chunk_text(chunk: Chunk) -> str:
    return f"{chunk.code} {chunk.start}～{chunk.end}（{len(chunk.days)} 個股日）"


def _summary_text(summary: BackfillSummary, scope: str, realtime: RealtimeConfig) -> str:
    settings = realtime.backfill
    lines = [
        f"rt-backfill（{scope}，{_range_text(summary)}）",
        f"開始時要查的股日 {_n(summary.missing)}（範圍內 screener.db 的股日 {_n(summary.wanted)}）",
        *_listing_lines(summary, realtime),
        f"本次：查詢 {_n(summary.queries)} 次（有 K 棒 {_n(summary.ok)}、沒有資料 "
        f"{_n(summary.empty)}、失敗 {_n(summary.failed)}），寫入 {_n(summary.recorded)} 個股日"
        f"（其中沒有資料 {_n(summary.no_data)}）、K 棒 {_n(summary.bars)} 根",
    ]
    used = summary.used_bytes
    per_day = used / summary.recorded if used and summary.recorded else None
    if used is not None:
        lines.append(f"流量：今天已用 {_size(summary.usage_start or 0)} → {_size(summary.usage_end or 0)}，本次約 "
                     f"{_size(used)}" + (f"（每個股日約 {per_day / 1000:.1f} KB）" if per_day else "")
                     + "；usage() 約每 5 秒才更新，最後幾次查詢可能還沒算進去")
    if summary.skipped_codes:
        lines.append(f"合約查不到、本次略過：{', '.join(summary.skipped_codes)}")
    stop = _STOP_TEXT[summary.stopped]
    if summary.stopped == TRAFFIC:
        stop += f" {settings.traffic_stop_mb:g} MB"
    lines.append(f"停止原因：{stop}")
    remaining_days = math.ceil(summary.remaining * (per_day or settings.bytes_per_stock_day)
                               / settings.traffic_stop_bytes) if summary.remaining else 0
    lines.append(f"剩下 {_n(summary.remaining)} 個股日" +
                 (f"，預估還要 {remaining_days} 個交易日" if summary.remaining else ""))
    return "\n".join(lines) + "\n"


def _size(value: float) -> str:
    mb = value / 1024 / 1024
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.1f} MB"


def _n(value: int) -> str:
    return f"{value:,}"
