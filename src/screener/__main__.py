"""CLI 入口：python -m screener <command>（PROJECT_PLAN.md 第 12 節）。"""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

from screener.config import ConfigError, DiscordSettings, load_backtest_config, load_config, load_rules, load_secrets
from screener.jobs import JobFailed, analyze, corporate_events, daily_update, intraday_scan
from screener.logging_setup import setup_logging
from screener.notify.base import Notifier, NotifyError
from screener.notify.discord import MESSAGE_LIMIT, DiscordWebhook
from screener.notify.message import format_failure

logger = logging.getLogger("screener.cli")     # 以 -m 執行時 __name__ 是 "__main__"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG_ERROR = 2

ENV_FILE = Path(".env")                         # 與 config/ 一樣相對於目前目錄


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

    update = commands.add_parser(
        "update",
        help="盤後更新資料庫",
        description="向 FinMind 補抓觀察清單的日 K 並寫入資料庫。",
    )
    update.add_argument(
        "--since",
        type=_parse_date,
        metavar="YYYY-MM-DD",
        help="把日 K 往前回補到這一天（供回測使用）；不指定時只補抓資料庫最後一筆之後的資料",
    )

    commands.add_parser(
        "events",
        help="抓取除權息、減資與面額變更（回測使用）",
        description="向 FinMind 抓取觀察清單的除權息、減資與面額變更並寫入資料庫，期間涵蓋資料庫中的日 K；"
                    "完成後以官方除權息參考價交叉驗證。",
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

    backtest_parser = commands.add_parser(
        "backtest",
        help="訊號回測",
        description="以資料庫的歷史日 K 逐日回放規則，統計訊號後的報酬並與同日觀察清單的平均比較，"
                    "輸出終端摘要、CSV 與圖表報表（設定在 config/backtest.yaml）。需先執行 update --since 與 events。",
    )
    backtest_parser.add_argument("--start", type=_parse_date, metavar="YYYY-MM-DD",
                                 help="訊號的起始日（預設為 backtest.yaml 的 signal_start）")
    backtest_parser.add_argument("--end", type=_parse_date, metavar="YYYY-MM-DD",
                                 help="訊號的結束日（預設為資料庫最後一天）")
    backtest_parser.add_argument("--horizon", type=int, metavar="N",
                                 help="終端摘要與單一天數圖表使用的持有天數（預設為 backtest.yaml 的 summary_horizon）")
    backtest_parser.add_argument("--rules-file", type=Path, metavar="PATH",
                                 help="改用另一份規則檔回測，不影響每日推播（預設為 config/rules.yaml）")
    backtest_parser.add_argument("--rules", type=_parse_ids, metavar="ID,ID",
                                 help="只回測指定的進場規則 id，以逗號分隔")

    analyze_parser = commands.add_parser(
        "analyze",
        help="輸出指定股票的轉折點與趨勢",
        description="以資料庫的日 K 計算轉折點與趨勢並印出，供檢查分析結果。",
    )
    analyze_parser.add_argument("codes", nargs="+", metavar="CODE", help="股票代號，可多個")
    analyze_parser.add_argument(
        "--date",
        type=_parse_date,
        metavar="YYYY-MM-DD",
        help="以該日日 K 當作「今日」（預設為資料庫最後一筆）",
    )
    return parser


def _parse_ids(value: str) -> list[str]:
    ids = [part.strip() for part in value.split(",") if part.strip()]
    if not ids:
        raise argparse.ArgumentTypeError("至少要有一個規則 id")
    return ids


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
    started_at = datetime.now()

    secrets = load_secrets(ENV_FILE)
    try:
        notifier = _notifier(args, config.settings.discord, secrets.discord_webhook_url)
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        return EXIT_CONFIG_ERROR

    try:
        if args.command == "update":
            daily_update.run(config, secrets, since=args.since)
        elif args.command == "events":
            corporate_events.run(config, secrets)
        elif args.command == "analyze":
            analyze.run(config, args.codes, as_of=args.date)
        elif args.command == "backtest":
            # 回測需要 plotly，延後到這裡才匯入：每日排程的 update、scan 不依賴它，少裝套件也不會失敗
            from screener.jobs import backtest

            rules_file = args.rules_file or args.config_dir / "rules.yaml"
            backtest.run(config, load_backtest_config(args.config_dir / "backtest.yaml"),
                         rules=load_rules(rules_file), rules_label=str(rules_file), rule_ids=args.rules,
                         start=args.start, end=args.end, horizon=args.horizon)
        else:
            intraday_scan.run(config, replay=args.replay, notifier=notifier)
    except ConfigError as exc:                 # 例：rules.yaml 的規則 id 或參數錯誤，建立規則時才會發現
        logger.error("設定檔錯誤：%s", exc)
        _notify_failure(notifier, args, started_at, f"設定檔錯誤：{exc}", log_file)
        return EXIT_CONFIG_ERROR
    except JobFailed as exc:
        logger.error("%s", exc)
        _notify_failure(notifier, args, started_at, str(exc), log_file)
        return EXIT_FAILED
    except Exception as exc:
        logger.exception("執行 %s 失敗", args.command)
        _notify_failure(notifier, args, started_at, f"{type(exc).__name__}: {exc}", log_file)
        return EXIT_FAILED
    return EXIT_OK


def _notifier(args: argparse.Namespace, settings: DiscordSettings, webhook_url: str | None) -> Notifier | None:
    """scan 推播結果與錯誤通知；update 只推錯誤通知（D30）；analyze 與 --dry-run 不推播。"""
    pushes_results = args.command == "scan" and not args.dry_run
    if not (pushes_results or args.command == "update"):
        return None
    if webhook_url:
        return DiscordWebhook(settings, webhook_url)
    if pushes_results:
        raise ConfigError("未設定 DISCORD_WEBHOOK_URL（.env），無法推播；只要在終端查看結果請加上 --dry-run")
    logger.warning("未設定 DISCORD_WEBHOOK_URL（.env），失敗時不會推送錯誤通知")
    return None


def _notify_failure(notifier: Notifier | None, args: argparse.Namespace, started_at: datetime, detail: str,
                    log_file: Path) -> None:
    """推送錯誤通知，避免使用者誤以為只是沒有訊號（PROJECT_PLAN.md 7.2）。推送失敗時只記錄在 log。"""
    if notifier is None:
        return
    if args.command == "update":
        job = "盤後更新"
    else:
        job = f"回放 {args.replay}" if args.replay else "盤中選股"
    title = f"{job}執行失敗（{started_at:%Y-%m-%d %H:%M}）"
    try:
        notifier.send(format_failure(title, detail, log_file, MESSAGE_LIMIT))
    except NotifyError as exc:
        logger.error("錯誤通知推送失敗：%s", exc)
    else:
        logger.info("已推送錯誤通知")


if __name__ == "__main__":
    sys.exit(main())
