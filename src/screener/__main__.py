"""CLI 入口：python -m screener <update|scan>（PROJECT_PLAN.md 第 12 節）。"""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from screener.config import ConfigError, load_config
from screener.jobs import daily_update, intraday_scan
from screener.logging_setup import setup_logging

logger = logging.getLogger("screener.cli")     # 以 -m 執行時 __name__ 是 "__main__"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screener",
        description="台股技術面盤中選股系統：盤後更新日 K，盤中依技術面規則篩選觀察清單並推播 Discord。",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="設定檔目錄（預設：%(default)s）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="輸出 DEBUG 等級的 log")
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    commands.add_parser(
        "update",
        help="盤後更新資料庫",
        description="向 FinMind 補抓觀察清單的日 K 並寫入資料庫。",
    )

    scan = commands.add_parser(
        "scan",
        help="盤中掃描並推播",
        description="以盤中暫定 K 棒執行規則引擎，依族群分組推播到 Discord。",
    )
    scan.add_argument("--dry-run", action="store_true", help="只印在終端，不推播")
    scan.add_argument(
        "--replay",
        type=_parse_date,
        metavar="YYYY-MM-DD",
        help="以該日日 K 當作「今日」重跑，供非交易時段測試與展示；可與 --dry-run 併用",
    )
    return parser


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"日期格式應為 YYYY-MM-DD：{value}") from None


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config_dir)
    except ConfigError as exc:
        print(f"設定檔錯誤：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    log_file = setup_logging(config.settings.log_dir, verbose=args.verbose)
    logger.info("執行 %s，觀察清單 %d 檔，log 寫入 %s", args.command, len(config.watchlist.stocks), log_file)

    try:
        if args.command == "update":
            daily_update.run(config)
        else:
            intraday_scan.run(config, dry_run=args.dry_run, replay=args.replay)
    except NotImplementedError as exc:
        logger.error("%s", exc)
        return EXIT_FAILED
    except Exception:
        logger.exception("執行 %s 失敗", args.command)
        return EXIT_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
