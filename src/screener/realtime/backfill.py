"""回補的規劃：要查的股日、查詢的區間與順序、流量的預估（REALTIME_PLAN.md 7.2 第 3 點，L18）。純函式：不碰網路與資料庫。

- 要查的股日 = 該檔在 screener.db 有日 K 的日子，減去 intraday.db 已經查過的股日。
- 每次查詢一檔、最多 max_query_days 個日曆天（kbars 的限制）。每檔缺的股日由新到舊切成區間，全部區間依迄日由新到舊
  排列（同一個迄日依觀察清單的順序）：先補近期與驗證段的資料，回補期間就能用來開發與研究（L18）。
"""

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from screener.realtime.bars import Bar


def first_day(code: str, start: date, listed_on: date | None, market_transfers: Collection[str]) -> date:
    """回補與品質檢查的第一天：start 或上市櫃日，取較晚者（L25）。

    screener.db 的日 K 含上市櫃前的興櫃交易（PROJECT_PLAN.md D38）：Shioaji 也有這段的分 K，但交易到 15:00、量以股計、
    開盤價和日 K 不同，不是當沖研究的對象。轉市場的股票（例：上櫃轉上市）的上市櫃日是轉到目前市場的日子，
    之前是正常的上市櫃交易，從 start 起。沒有上市櫃日（listings 還沒有這一檔）時從 start 起。
    """
    if listed_on is None or code in market_transfers:
        return start
    return max(start, listed_on)


@dataclass(frozen=True)
class Chunk:
    """一次查詢：code 在 start～end（含）之間要查的股日 days（由舊到新）。start、end 就是第一個與最後一個要查的股日。"""

    code: str
    start: date
    end: date
    days: tuple[date, ...]


@dataclass(frozen=True)
class Estimate:
    stock_days: int
    queries: int
    bytes: int
    trading_days: int            # 每天用到停止點就停止，要幾個交易日
    queries_per_day: int


def missing_days(wanted: Mapping[str, Iterable[date]],
                 fetched: Mapping[str, Mapping[date, int]]) -> dict[str, list[date]]:
    """每檔還沒查過的股日（由舊到新）；沒有缺的股票不列入。"""
    result = {}
    for code, days in wanted.items():
        done = fetched.get(code, {})
        missing = sorted(day for day in set(days) if day not in done)
        if missing:
            result[code] = missing
    return result


def plan_chunks(missing: Mapping[str, Sequence[date]], max_query_days: int, order: Sequence[str]) -> list[Chunk]:
    """把每檔缺的股日切成查詢的區間，依迄日由新到舊排列；同一個迄日依 order（觀察清單的順序）。"""
    if max_query_days <= 0:
        raise ValueError("max_query_days 必須大於 0")
    rank = {code: index for index, code in enumerate(order)}
    chunks = []
    for code, days in missing.items():
        newest_first = sorted(set(days), reverse=True)
        i = 0
        while i < len(newest_first):
            end = newest_first[i]
            floor = end - timedelta(days=max_query_days - 1)
            j = i
            while j < len(newest_first) and newest_first[j] >= floor:
                j += 1
            group = newest_first[i:j]
            chunks.append(Chunk(code=code, start=group[-1], end=end, days=tuple(reversed(group))))
            i = j
    chunks.sort(key=lambda chunk: (-chunk.end.toordinal(), rank.get(chunk.code, len(rank)), chunk.code))
    return chunks


def estimate(chunks: Sequence[Chunk], bytes_per_stock_day: int, stop_bytes: int) -> Estimate:
    stock_days = sum(len(chunk.days) for chunk in chunks)
    total = stock_days * bytes_per_stock_day
    per_query = total / len(chunks) if chunks else 0
    return Estimate(stock_days=stock_days, queries=len(chunks), bytes=total,
                    trading_days=math.ceil(total / stop_bytes) if total else 0,
                    queries_per_day=int(stop_bytes // per_query) if per_query else 0)


@dataclass(frozen=True)
class ChunkResult:
    bars: list[Bar]              # 要查的股日的 K 棒（其他日子的不寫入）
    counts: dict[date, int]      # 每個要查的股日的 K 棒數；0 = 沒有資料
    extra_days: list[date]       # 回傳了 K 棒、但不在要查的股日裡的日子


def select_chunk(bars: Sequence[Bar], chunk: Chunk) -> ChunkResult:
    """查詢結果中只留下要查的股日（7.2 第 3 點：查詢結果中不在要查的股日裡的資料不寫入）。"""
    wanted = set(chunk.days)
    counts = {day: 0 for day in chunk.days}
    kept = []
    extra = set()
    for bar in bars:
        day = bar.start.date()
        if day in wanted:
            kept.append(bar)
            counts[day] += 1
        else:
            extra.add(day)
    return ChunkResult(bars=kept, counts=counts, extra_days=sorted(extra))
