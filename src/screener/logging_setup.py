"""logging 設定：同時輸出到終端與 log 目錄下的每日 log 檔。"""

import logging
import sys
from datetime import date
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_FILE_HANDLER = "screener.file"
_CONSOLE_HANDLER = "screener.console"


def setup_logging(log_dir: Path, *, verbose: bool = False) -> Path:
    """設定 root logger，回傳本次寫入的 log 檔路徑。"""
    reset_logging()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"screener-{date.today().isoformat()}.log"
    formatter = logging.Formatter(_FORMAT)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.set_name(_FILE_HANDLER)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.set_name(_CONSOLE_HANDLER)

    root = logging.getLogger()
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    return log_file


def reset_logging() -> None:
    """移除並關閉 setup_logging 加入的 handler，其他 handler 不動。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() in (_FILE_HANDLER, _CONSOLE_HANDLER):
            root.removeHandler(handler)
            handler.close()
