from pathlib import Path

from screener.data.db import SqlitePriceStore


class JobFailed(Exception):
    """job 未能完成。訊息會寫入 log，CLI 以結束碼 1 結束。"""


def open_existing_store(db_path: Path) -> SqlitePriceStore:
    """開啟已存在的資料庫；不存在時不建立空的資料庫，而是提示先執行 update。"""
    if not db_path.is_file():
        raise JobFailed(f"找不到資料庫 {db_path}，請先執行 update")
    return SqlitePriceStore(db_path)
