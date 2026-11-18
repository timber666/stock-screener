"""錄製檔：每天一個 data/ticks/YYYY-MM-DD.jsonl.gz（REALTIME_PLAN.md 7.1 第 4 點）。

每行一筆 JSON：

    {"type": "tick", "received_at": "2026-11-19T09:00:00.123456", "conn": 0, "data": {...}}

- type：tick（成交）、bidask（五檔）、event（連線事件）、session_down、usage（流量）、subscribe（訂閱的結果）、
  meta（開始與結束的摘要）。
- received_at：本機收到的時間；conn：第幾個連線（從 0 起算）。
- data：Shioaji 回傳的原始欄位，只做 JSON 需要的轉換（Decimal、時間、列舉轉成字串）。解析在讀取時做，
  之後改解析方式不必重錄。

Shioaji 從自己的執行緒呼叫 callback：callback 只把資料放進佇列（put），由主執行緒寫入（pump）。
同一天重新啟動時附加在原本的檔案後面（gzip 允許多段串接）；程式被強制結束時，最多遺失 flush_interval_sec 秒的資料，
讀取時截斷的結尾會被略過。
"""

import gzip
import json
import queue
import zlib
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

TICK = "tick"
BIDASK = "bidask"
EVENT = "event"
SESSION_DOWN = "session_down"
USAGE = "usage"
SUBSCRIBE = "subscribe"
META = "meta"


def recording_path(tick_dir: Path, day: date) -> Path:
    return tick_dir / f"{day.isoformat()}.jsonl.gz"


@dataclass
class Recorder:
    path: Path
    flush_interval_sec: float
    clock: Callable[[], datetime] = datetime.now
    counts: Counter[str] = field(default_factory=Counter)
    _queue: "queue.SimpleQueue[tuple[str, int, datetime, Any]]" = field(default_factory=queue.SimpleQueue)
    _file: gzip.GzipFile | None = None
    _last_flush: datetime | None = None

    def open(self) -> "Recorder":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = gzip.open(self.path, "ab")
        self._last_flush = self.clock()
        return self

    def put(self, kind: str, conn: int, data: Any) -> None:
        """可以從任何執行緒呼叫。收到的時間在這裡記下，寫入時才轉成 JSON。"""
        self._queue.put((kind, conn, self.clock(), data))

    def pump(self) -> int:
        """把佇列中的資料寫入檔案，必要時 flush。回傳寫入的筆數。只能從同一個執行緒呼叫。"""
        if self._file is None:
            raise RuntimeError("錄製檔尚未開啟")
        written = 0
        while True:
            try:
                kind, conn, received_at, data = self._queue.get_nowait()
            except queue.Empty:
                break
            line = {"type": kind, "received_at": received_at.isoformat(timespec="microseconds"), "conn": conn,
                    "data": jsonable(data)}
            self._file.write(json.dumps(line, ensure_ascii=False).encode("utf-8") + b"\n")
            self.counts[kind] += 1
            written += 1
        now = self.clock()
        if self._last_flush is None or (now - self._last_flush).total_seconds() >= self.flush_interval_sec:
            self.flush()
        return written

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush(zlib.Z_SYNC_FLUSH)     # 之後就算程式被強制結束，到這裡為止的資料也讀得到
            self._last_flush = self.clock()

    def close(self) -> None:
        if self._file is None:
            return
        self.pump()
        self._file.close()
        self._file = None


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """依序讀出錄製檔的每一筆，可以讀多段串接的 gzip。

    程式被強制結束時檔案的結尾不完整：讀到損壞或截斷的地方為止，之前完整的每一行都會讀出（gzip 模組遇到損壞時會丟掉
    整個緩衝區，所以這裡直接用 zlib 逐段解壓縮）。
    """
    decompressor = zlib.decompressobj(_GZIP_WBITS)
    pending = b""
    with path.open("rb") as f:
        while chunk := f.read(_READ_CHUNK):
            data = chunk
            while data:
                output, data, decompressor, broken = _decompress(decompressor, data)
                pending += output
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    if line:
                        yield json.loads(line)
                if broken:
                    return


_GZIP_WBITS = zlib.MAX_WBITS | 16
_READ_CHUNK = 1 << 16
_SALVAGE_CHUNK = 1                  # 遇到損壞時逐位元組重新解壓縮，保留損壞之前的全部資料（只發生在檔案結尾）


def _decompress(decompressor: Any, data: bytes) -> tuple[bytes, bytes, Any, bool]:
    """解壓縮 data。回傳（輸出, 還沒處理的資料, 之後用的解壓縮器, 是否遇到損壞）。一段 gzip 結束時換新的解壓縮器。"""
    backup = decompressor.copy()
    try:
        output = decompressor.decompress(data)
    except zlib.error:
        decompressor = backup
        output = b""
        for offset in range(0, len(data), _SALVAGE_CHUNK):
            try:
                output += decompressor.decompress(data[offset:offset + _SALVAGE_CHUNK])
            except zlib.error:
                return output, b"", decompressor, True
            if decompressor.eof:
                rest = decompressor.unused_data + data[offset + _SALVAGE_CHUNK:]
                return output, rest, zlib.decompressobj(_GZIP_WBITS), False
        return output, b"", decompressor, False
    if decompressor.eof:
        return output, decompressor.unused_data, zlib.decompressobj(_GZIP_WBITS), False
    return output, b"", decompressor, False


def jsonable(value: Any) -> Any:
    """Shioaji 的資料轉成 JSON 能表示的值：Decimal 保留成字串（不損失精度），時間轉成 ISO 格式，列舉轉成名稱。"""
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)
