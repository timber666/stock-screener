"""營收年增的因子快照（PROJECT_PLAN.md D57、RESEARCH_PLAN.md 4.7）。

研究指令 factor-snapshot 每週產生，每日的 scan 唯讀。寫入與讀取共用這個模組，格式只定義一次；
這裡不匯入研究程式（PROJECT_PLAN.md 10.4 規則 1）。

檔案是 CSV，每列一家公司（只列有值的）：代號、營收年增（小數）、由高到低的名次、排名的檔數、營收月份、產生日。
每列的檔數、月份與產生日相同，列數等於檔數。
"""

import csv
import math
import os
import re
from datetime import date
from pathlib import Path

from screener.models import FactorSnapshot, SnapshotRead, SnapshotStatus

COLUMNS = ("code", "rev_yoy", "rank", "count", "month", "snapshot_date")
_CODE = re.compile(r"[0-9]{4}")
_MONTH = re.compile(r"[0-9]{4}-[0-9]{2}")


class SnapshotFormatError(ValueError):
    """快照的內容不符合格式。"""


def write_snapshot(path: Path, snapshot: FactorSnapshot) -> None:
    """先寫到同一個資料夾的暫存檔再改名取代：讀取的一方不會讀到寫一半的檔案。"""
    rows = sorted(snapshot.values.items(), key=lambda item: (item[1][1], item[0]))
    if len(rows) != snapshot.count:
        raise SnapshotFormatError(f"列數 {len(rows)} 與檔數 {snapshot.count} 不同")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for code, (value, rank) in rows:
            writer.writerow([code, f"{value:.6f}", rank, snapshot.count, f"{snapshot.month:%Y-%m}",
                             snapshot.snapshot_date.isoformat()])
    os.replace(temporary, path)


def read_snapshot(path: Path, *, today: date, max_age_days: int) -> SnapshotRead:
    """讀取並檢查快照；任何錯誤都不拋出例外，以狀態回傳（scan 不會因為快照而失敗，D57）。"""
    if not path.is_file():
        return SnapshotRead(SnapshotStatus.MISSING, max_age_days, reason=f"找不到 {path}")
    try:
        snapshot = parse_snapshot(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, csv.Error, SnapshotFormatError) as exc:
        return SnapshotRead(SnapshotStatus.INVALID, max_age_days, reason=f"{path}：{exc}")
    age = (today - snapshot.snapshot_date).days
    if age > max_age_days:
        return SnapshotRead(SnapshotStatus.STALE, max_age_days, snapshot,
                            reason=f"快照 {snapshot.snapshot_date} 已經 {age} 天，超過 {max_age_days} 天")
    return SnapshotRead(SnapshotStatus.OK, max_age_days, snapshot)


def parse_snapshot(text: str) -> FactorSnapshot:
    rows = list(csv.reader(text.splitlines()))
    if not rows or tuple(rows[0]) != COLUMNS:
        raise SnapshotFormatError(f"欄位應為 {','.join(COLUMNS)}")
    body = rows[1:]
    if not body:
        raise SnapshotFormatError("沒有任何資料列")
    values: dict[str, tuple[float, int]] = {}
    shared: set[tuple[str, str, str]] = set()
    for line, row in enumerate(body, start=2):
        if len(row) != len(COLUMNS):
            raise SnapshotFormatError(f"第 {line} 列的欄位數為 {len(row)}")
        code, value_text, rank_text, count_text, month_text, date_text = row
        if not _CODE.fullmatch(code):
            raise SnapshotFormatError(f"第 {line} 列的代號不是 4 碼數字：{code!r}")
        if code in values:
            raise SnapshotFormatError(f"第 {line} 列的代號重複：{code}")
        value, rank = _number(value_text, float, line), _number(rank_text, int, line)
        values[code] = (value, rank)
        shared.add((count_text, month_text, date_text))
    if len(shared) != 1:
        raise SnapshotFormatError("各列的 count、month、snapshot_date 不同")
    count_text, month_text, date_text = shared.pop()
    count = _number(count_text, int, 2)
    if count != len(values):
        raise SnapshotFormatError(f"列數 {len(values)} 與 count {count} 不同")
    if not _MONTH.fullmatch(month_text):
        raise SnapshotFormatError(f"month 應為 YYYY-MM：{month_text!r}")
    try:
        month, snapshot_date = date.fromisoformat(f"{month_text}-01"), date.fromisoformat(date_text)
    except ValueError as exc:
        raise SnapshotFormatError(f"日期讀不出來：{exc}") from None
    for code, (_, rank) in values.items():
        if not 1 <= rank <= count:
            raise SnapshotFormatError(f"{code} 的名次 {rank} 不在 1～{count}")
    return FactorSnapshot(month=month, snapshot_date=snapshot_date, count=count, values=values)


def _number(text: str, kind: type, line: int):
    try:
        value = kind(text)
    except ValueError:
        raise SnapshotFormatError(f"第 {line} 列的數字讀不出來：{text!r}") from None
    if isinstance(value, float) and not math.isfinite(value):
        raise SnapshotFormatError(f"第 {line} 列的數字不是有限值：{text!r}")
    return value
