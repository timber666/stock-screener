"""CLI 入口：python -m screener <command>（PROJECT_PLAN.md 第 12 節）。

研究指令（events、backtest、trade-backtest、signal-report、market-update、market-check、factor-report）的程式在執行該指令時才匯入：每日排程的 update、scan
不載入任何研究程式、研究設定與研究套件（PROJECT_PLAN.md 10.4）。
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

from screener.config import (AppConfig, ConfigError, DiscordSettings, ResearchConfig, Secrets, load_backtest_config,
                             load_config, load_research_config, load_rules, load_secrets)
from screener.jobs import JobFailed, analyze, daily_update, intraday_scan
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
                                 help="只回測指定的觸發規則 id，以逗號分隔")
    _add_universe(backtest_parser)

    trade_parser = commands.add_parser(
        "trade-backtest",
        help="交易回測：照進出場規則模擬一個帳戶",
        description="照 rules.yaml 的進場與出場規則模擬一個帳戶，比較檔數上限與挑選方式（設定在 config/backtest.yaml "
                    "的 trade）。預設只跑觀察段；選定一組後加 --validate 檢查它在驗證段的結果。需先執行 update --since 與 events。",
    )
    trade_parser.add_argument("--validate", action="store_true",
                              help="只跑驗證段，需同時指定 --max-positions 與 --pick（只檢查觀察段選定的一組）")
    trade_parser.add_argument("--max-positions", type=int, metavar="N", help="驗證段使用的檔數上限")
    trade_parser.add_argument("--pick", metavar="PICK", help="驗證段使用的挑選方式，例：nearest_ma、volume_ratio")
    trade_parser.add_argument("--rules-file", type=Path, metavar="PATH",
                              help="改用另一份規則檔，不影響每日推播（預設為 config/rules.yaml）")
    _add_universe(trade_parser)

    report_parser = commands.add_parser(
        "signal-report",
        help="訊號成績單：一週內推播的訊號，收盤是否仍成立與之後的表現（推播到研究頻道）",
        description="讀取 scan 記錄的訊號與報價快照，以收盤 K 棒重算訊號比對一致性，並以 13:10 現價計算之後的報酬，"
                    "推播到研究頻道（DISCORD_RESEARCH_WEBHOOK_URL）並輸出明細 CSV（設定在 config/research.yaml）。",
    )
    report_parser.add_argument("--end", type=_parse_date, metavar="YYYY-MM-DD",
                               help="期間的最後一天，期間為該週的週一到這一天（預設為上一週的週五）")
    report_parser.add_argument("--dry-run", action="store_true", help="只印在終端，不推播")

    market_parser = commands.add_parser(
        "market-update",
        help="全市場資料：補抓證交所、櫃買中心的每日行情與事件到 market.db（研究使用）",
        description="補抓上市、上櫃普通股的每日行情、報酬指數、除權息與減資，以及月營收、本益比與發行股數到 market.db"
                    "（設定在 config/research.yaml 的 market）。第一次從期間起點開始回補，中斷後重新執行會從中斷處繼續；只抓到昨天。",
    )
    market_parser.add_argument("--start", type=_parse_date, metavar="YYYY-MM-DD",
                               help="只查詢這一天以後（預設為 research.yaml 的 market.start）")
    market_parser.add_argument("--end", type=_parse_date, metavar="YYYY-MM-DD", help="只查詢到這一天（預設為昨天）")

    commands.add_parser(
        "market-check",
        help="全市場資料的品質報告（研究使用）",
        description="檢查 market.db：與 screener.db 重疊的價格、除權息與減資、報酬指數、異常值、下市與轉上市，"
                    "終端印出摘要，明細寫成 CSV（research.yaml 的 market.check_dir）。",
    )

    factor_parser = commands.add_parser(
        "factor-report",
        help="單因子研究：Rank IC、分位數報酬、換手率、產業中性前後（研究使用）",
        description="以全市場的動態股票池評估 research.yaml 的因子：終端印出摘要，輸出 summary.csv、ic.csv、decay.csv、"
                    "groups.csv 與 report.html（research.yaml 的 factors.output_dir）。需先執行 market-update。",
    )
    factor_parser.add_argument("--factors", type=_parse_ids, metavar="ID,ID", help="只跑指定的因子 id，以逗號分隔")

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


def _add_universe(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--universe", choices=("watchlist", "market"), default="watchlist",
                        help="watchlist：觀察清單（預設）；market：全市場的動態股票池（market.db，"
                             "設定在 research.yaml 的 market），並與觀察清單並列")


def _parse_ids(value: str) -> list[str]:
    ids = [part.strip() for part in value.split(",") if part.strip()]
    if not ids:
        raise argparse.ArgumentTypeError("至少要有一個 id")
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
        notifier = _notifier(args, config.settings.discord, secrets)
    except ConfigError as exc:
        logger.error("設定錯誤：%s", exc)
        return EXIT_CONFIG_ERROR

    try:
        if args.command == "update":
            daily_update.run(config, secrets, since=args.since)
        elif args.command == "scan":
            intraday_scan.run(config, replay=args.replay, notifier=notifier)
        elif args.command == "analyze":
            analyze.run(config, args.codes, as_of=args.date)
        else:
            _run_research(args, config, secrets, notifier)
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


def _run_research(args: argparse.Namespace, config: AppConfig, secrets: Secrets, notifier: Notifier | None) -> None:
    """研究指令：程式與研究設定在這裡才匯入、載入（PROJECT_PLAN.md 10.4）。"""
    if args.command == "events":
        from screener.jobs import corporate_events

        corporate_events.run(config, load_research_config(args.config_dir / "research.yaml"), secrets)
    elif args.command == "signal-report":
        from screener.jobs import signal_report

        signal_report.run(config, load_research_config(args.config_dir / "research.yaml"), end=args.end,
                          notifier=notifier)
    elif args.command == "market-update":
        from screener.jobs import market_update

        market_update.run(config, load_research_config(args.config_dir / "research.yaml"), secrets,
                          start=args.start, end=args.end)
    elif args.command == "market-check":
        from screener.jobs import market_check

        market_check.run(config, load_research_config(args.config_dir / "research.yaml"))
    elif args.command == "factor-report":
        from screener.jobs import factor_report     # 需要研究套件 plotly

        factor_report.run(config, load_research_config(args.config_dir / "research.yaml"),
                          load_backtest_config(args.config_dir / "backtest.yaml"), factor_ids=args.factors)
    elif args.command == "trade-backtest":
        from screener.jobs import trade_backtest    # 需要研究套件 plotly

        rules_file = args.rules_file or args.config_dir / "rules.yaml"
        trade_backtest.run(config, load_backtest_config(args.config_dir / "backtest.yaml"),
                           rules=load_rules(rules_file), rules_label=str(rules_file), validate=args.validate,
                           max_positions=args.max_positions, pick=args.pick, universe=args.universe,
                           research=_market_research(args))
    else:
        from screener.jobs import backtest      # 需要研究套件 plotly

        rules_file = args.rules_file or args.config_dir / "rules.yaml"
        backtest.run(config, load_backtest_config(args.config_dir / "backtest.yaml"),
                     rules=load_rules(rules_file), rules_label=str(rules_file), rule_ids=args.rules,
                     start=args.start, end=args.end, horizon=args.horizon, universe=args.universe,
                     research=_market_research(args))


def _market_research(args: argparse.Namespace) -> ResearchConfig | None:
    """--universe market 才需要 research.yaml（market 區塊）。"""
    return load_research_config(args.config_dir / "research.yaml") if args.universe == "market" else None


def _notifier(args: argparse.Namespace, settings: DiscordSettings, secrets: Secrets) -> Notifier | None:
    """scan 推播結果與錯誤通知；update 只推錯誤通知（D30）；signal-report 的結果與錯誤通知都推到研究頻道（D43）；
    market-update（每週排程，R9）只把錯誤通知推到研究頻道，未設定時只記錄在 log；其餘指令與 --dry-run 不推播。"""
    if args.command == "signal-report":
        if args.dry_run:
            return None
        if not secrets.discord_research_webhook_url:
            raise ConfigError("未設定 DISCORD_RESEARCH_WEBHOOK_URL（.env），無法推播成績單；"
                              "只要在終端查看請加上 --dry-run")
        return DiscordWebhook(settings, secrets.discord_research_webhook_url, env_name="DISCORD_RESEARCH_WEBHOOK_URL")

    if args.command == "market-update":
        if secrets.discord_research_webhook_url:
            return DiscordWebhook(settings, secrets.discord_research_webhook_url,
                                  env_name="DISCORD_RESEARCH_WEBHOOK_URL")
        logger.warning("未設定 DISCORD_RESEARCH_WEBHOOK_URL（.env），失敗時不會推送錯誤通知")
        return None

    pushes_results = args.command == "scan" and not args.dry_run
    if not (pushes_results or args.command == "update"):
        return None
    if secrets.discord_webhook_url:
        return DiscordWebhook(settings, secrets.discord_webhook_url)
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
    elif args.command == "signal-report":
        job = "訊號成績單"
    elif args.command == "market-update":
        job = "全市場資料更新"
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
