"""Shioaji 歷史資料的時間戳記（kbars、ticks、snapshots 的 ts）。"""

from datetime import datetime, timedelta

_EPOCH = datetime(1970, 1, 1)


def from_shioaji_ts(ts: int) -> datetime:
    """ts 是奈秒。Shioaji 把台灣的本地時間當成 UTC 編碼，以 UTC 解讀就得到台灣時間（不含時區的 datetime）。"""
    return _EPOCH + timedelta(microseconds=ts // 1000)
