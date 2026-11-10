"""營收年增的因子快照：格式、讀寫與檢查（PROJECT_PLAN.md D57、RESEARCH_PLAN.md 4.7）。"""

from datetime import date
from pathlib import Path

import pytest

from screener.data.factor_snapshot import COLUMNS, read_snapshot, write_snapshot
from screener.models import FactorSnapshot, SnapshotStatus

SATURDAY = date(2026, 11, 14)
HEADER = ",".join(COLUMNS)


def snapshot(**values: tuple[float, int]) -> FactorSnapshot:
    values = values or {"2330": (0.5465, 1), "2317": (0.3842, 2), "6488": (-0.1313, 3)}
    return FactorSnapshot(month=date(2026, 10, 1), snapshot_date=SATURDAY, count=len(values), values=values)


def test_written_snapshot_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "data" / "factor_snapshot.csv"
    write_snapshot(path, snapshot())

    result = read_snapshot(path, today=date(2026, 11, 16), max_age_days=10)

    assert result.status is SnapshotStatus.OK
    assert result.snapshot == snapshot()
    assert result.snapshot.values == snapshot().values
    assert path.read_text(encoding="utf-8").splitlines() == [
        HEADER,
        "2330,0.546500,1,3,2026-10,2026-11-14",
        "2317,0.384200,2,3,2026-10,2026-11-14",
        "6488,-0.131300,3,3,2026-10,2026-11-14",
    ]


def test_write_replaces_the_file_without_leaving_a_temporary(tmp_path: Path) -> None:
    path = tmp_path / "factor_snapshot.csv"
    path.write_text("old", encoding="utf-8")

    write_snapshot(path, snapshot())

    assert path.read_text(encoding="utf-8").startswith(HEADER)
    assert [p.name for p in tmp_path.iterdir()] == ["factor_snapshot.csv"]


@pytest.mark.parametrize(("rank", "count", "percentile"), [(1, 1977, 1), (352, 1977, 18), (1977, 1977, 100),
                                                           (20, 100, 20), (21, 100, 21), (1, 1, 100)])
def test_percentile_rounds_up(rank: int, count: int, percentile: int) -> None:
    item = FactorSnapshot(month=date(2026, 9, 1), snapshot_date=SATURDAY, count=count, values={})

    assert item.percentile(rank) == percentile


def test_missing_file(tmp_path: Path) -> None:
    result = read_snapshot(tmp_path / "factor_snapshot.csv", today=SATURDAY, max_age_days=10)

    assert (result.status, result.snapshot, result.max_age_days) == (SnapshotStatus.MISSING, None, 10)
    assert "找不到" in result.reason


@pytest.mark.parametrize(("today", "status"), [(date(2026, 11, 24), SnapshotStatus.OK),     # 10 天：還會顯示
                                               (date(2026, 11, 25), SnapshotStatus.STALE)])  # 11 天
def test_snapshot_older_than_max_age_is_stale(tmp_path: Path, today: date, status: SnapshotStatus) -> None:
    path = tmp_path / "factor_snapshot.csv"
    write_snapshot(path, snapshot())

    result = read_snapshot(path, today=today, max_age_days=10)

    assert result.status is status
    assert result.snapshot is not None and result.snapshot.snapshot_date == SATURDAY


ROW = "2330,0.546500,1,1,2026-10,2026-11-14"


@pytest.mark.parametrize(("content", "message"), [
    ("", "欄位應為"),
    ("code,yoy,rank,count,month,snapshot_date\n" + ROW, "欄位應為"),
    (HEADER, "沒有任何資料列"),
    (HEADER + "\n2330,0.5,1,1,2026-10", "欄位數"),
    (HEADER + "\n0050,0.5,1,1,2026-10,2026-11-14", None),            # 4 碼數字的格式本身沒問題（普通股篩選在研究端）
    (HEADER + "\n233A,0.5,1,1,2026-10,2026-11-14", "不是 4 碼數字"),
    (HEADER + "\n2330,abc,1,1,2026-10,2026-11-14", "數字讀不出來"),
    (HEADER + "\n2330,inf,1,1,2026-10,2026-11-14", "不是有限值"),
    (HEADER + "\n2330,0.5,1.5,1,2026-10,2026-11-14", "數字讀不出來"),
    (HEADER + "\n2330,0.5,2,1,2026-10,2026-11-14", "不在 1～1"),
    (HEADER + "\n2330,0.5,1,2,2026-10,2026-11-14", "與 count 2 不同"),
    (HEADER + "\n2330,0.5,1,2,2026-10,2026-11-14\n2330,0.4,2,2,2026-10,2026-11-14", "代號重複"),
    (HEADER + "\n2330,0.5,1,2,2026-10,2026-11-14\n2317,0.4,2,2,2026-09,2026-11-14", "不同"),
    (HEADER + "\n2330,0.5,1,1,2026/10,2026-11-14", "YYYY-MM"),
    (HEADER + "\n2330,0.5,1,1,2026-13,2026-11-14", "日期讀不出來"),
    (HEADER + "\n2330,0.5,1,1,2026-10,2026-11-31", "日期讀不出來"),
])
def test_format_errors_are_reported_as_invalid(tmp_path: Path, content: str, message: str | None) -> None:
    path = tmp_path / "factor_snapshot.csv"
    path.write_text(content, encoding="utf-8")

    result = read_snapshot(path, today=SATURDAY, max_age_days=10)

    if message is None:
        assert result.status is SnapshotStatus.OK
    else:
        assert result.status is SnapshotStatus.INVALID and result.snapshot is None
        assert message in result.reason


def test_undecodable_file_is_invalid(tmp_path: Path) -> None:
    path = tmp_path / "factor_snapshot.csv"
    path.write_bytes(b"\xff\xfe\x00broken")

    assert read_snapshot(path, today=SATURDAY, max_age_days=10).status is SnapshotStatus.INVALID


def test_ties_share_a_rank(tmp_path: Path) -> None:
    path = tmp_path / "factor_snapshot.csv"
    path.write_text("\n".join([HEADER, "2330,0.5,1,3,2026-10,2026-11-14", "2317,0.5,1,3,2026-10,2026-11-14",
                               "6488,0.1,3,3,2026-10,2026-11-14"]), encoding="utf-8")

    result = read_snapshot(path, today=SATURDAY, max_age_days=10)

    assert result.status is SnapshotStatus.OK
    assert result.snapshot.values == {"2330": (0.5, 1), "2317": (0.5, 1), "6488": (0.1, 3)}
