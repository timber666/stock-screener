"""盤中 job：合成暫定 K 棒、執行規則引擎並推播（PROJECT_PLAN.md 7.2）。"""

from datetime import date

from screener.config import AppConfig


def run(config: AppConfig, *, dry_run: bool, replay: date | None) -> None:
    raise NotImplementedError("scan 尚未實作（回放於 M3、盤中於 M4）")
