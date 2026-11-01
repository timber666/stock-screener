import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_ROOT, load_fixture, read_yaml, to_bars, write_yaml
import screener.__main__ as cli
from screener.__main__ import EXIT_CONFIG_ERROR, EXIT_FAILED, EXIT_OK, main
from screener.data.base import DataSourceError
from screener.data.db import SqlitePriceStore
from screener.jobs import daily_update, intraday_scan
from screener.models import DailyBar, Quote
from screener.notify.base import NotifyError


def test_help_runs_as_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "screener", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "update" in result.stdout
    assert "scan" in result.stdout


def test_daily_commands_do_not_need_plotly() -> None:
    # 每日排程只執行 update 與 scan；plotly 只有 backtest 需要，未安裝時 CLI 仍要能啟動
    result = subprocess.run(
        [sys.executable, "-c", "import sys, screener.__main__; print('plotly' in sys.modules)"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )

    assert result.stdout.strip() == "False"


def test_invalid_replay_date_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["scan", "--replay", "2026-13-01"])

    assert exc_info.value.code == 2


def test_config_error_exits_with_config_error_code(tmp_path: Path) -> None:
    assert main(["--config-dir", str(tmp_path / "missing"), "update"]) == EXIT_CONFIG_ERROR


@pytest.mark.parametrize("argv", [["scan"], ["scan", "--replay", "2026-10-29"]])
def test_push_without_webhook_exits_with_config_error(config_dir: Path, argv: list[str]) -> None:
    assert main(["--config-dir", str(config_dir), *argv]) == EXIT_CONFIG_ERROR

    assert "未設定 DISCORD_WEBHOOK_URL" in read_log(config_dir)


# --- update -----------------------------------------------------------------

class FakeFinMindClient:
    """取代 FinMindClient：每檔回傳今天一根 K 棒，或在指定代號拋出錯誤。"""

    instances: list["FakeFinMindClient"] = []
    fail_on: str | None = None

    def __init__(self, settings: Any, token: str | None) -> None:
        self.token = token
        FakeFinMindClient.instances.append(self)

    def fetch_daily_prices(self, code: str, start: date, end: date) -> list[DailyBar]:
        if code == self.fail_on:
            raise DataSourceError("已達 FinMind 每小時請求上限")
        return [DailyBar(code=code, date=end, open=10.0, high=11.0, low=9.0, close=10.5, volume=100)]


@pytest.fixture
def fake_finmind(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> type[FakeFinMindClient]:
    """縮小觀察清單、換掉 FinMind，並在暫存目錄執行，讓 .env 從暫存目錄讀取。"""
    watchlist = read_yaml(config_dir / "watchlist.yaml")
    watchlist["stocks"] = watchlist["stocks"][:2]
    write_yaml(config_dir / "watchlist.yaml", watchlist)

    monkeypatch.setattr(daily_update, "FinMindClient", FakeFinMindClient)
    monkeypatch.setattr(FakeFinMindClient, "instances", [])
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    monkeypatch.chdir(config_dir.parent)
    (config_dir.parent / ".env").write_text("FINMIND_TOKEN=token-from-env-file\n", encoding="utf-8")
    return FakeFinMindClient


def test_update_creates_database(config_dir: Path, fake_finmind: type[FakeFinMindClient]) -> None:
    assert main(["--config-dir", str(config_dir), "update"]) == EXIT_OK

    stocks = read_yaml(config_dir / "watchlist.yaml")["stocks"]
    with SqlitePriceStore(config_dir.parent / "data" / "screener.db") as store:
        for item in stocks:
            assert store.latest_date(item["code"]) == date.today()
    assert fake_finmind.instances[0].token == "token-from-env-file"
    assert "寫入 2 筆" in read_log(config_dir)


def test_update_failure_exits_with_failed_code(
    config_dir: Path, fake_finmind: type[FakeFinMindClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    second_code = read_yaml(config_dir / "watchlist.yaml")["stocks"][1]["code"]
    monkeypatch.setattr(fake_finmind, "fail_on", second_code)

    assert main(["--config-dir", str(config_dir), "update"]) == EXIT_FAILED

    log_text = read_log(config_dir)
    assert f"{second_code}" in log_text and "抓取失敗" in log_text
    assert "Traceback" not in log_text


# --- analyze ----------------------------------------------------------------

@pytest.fixture
def analysis_db(config_dir: Path) -> str:
    """把 tests/fixtures/bull_zigzag.csv 寫入暫存資料庫，回傳使用的股票代號（觀察清單第一檔）。"""
    code = read_yaml(config_dir / "watchlist.yaml")["stocks"][0]["code"]
    bars = to_bars(load_fixture("bull_zigzag"))
    with SqlitePriceStore(config_dir.parent / "data" / "screener.db") as store:
        store.upsert_bars([DailyBar(code, day, r.open, r.high, r.low, r.close, int(r.volume))
                           for day, r in zip(bars.index, bars.itertuples())])
    return code


def test_analyze_prints_pivots_and_trend(
    config_dir: Path, analysis_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config-dir", str(config_dir), "analyze", analysis_db]) == EXIT_OK

    out = capsys.readouterr().out
    assert "今日 2026-02-05：收盤 114.00，MA5 114.80" in out
    assert "轉折點（2026-01-05 ~ 2026-02-05，24 根 K 棒）" in out
    assert "2026-01-12  頭  110.00" in out and "2026-01-30  頭  123.00" in out
    assert "基本趨勢：多頭（頭 116.00 → 123.00，底 98.00 → 104.00）" in out
    assert "  趨勢：多頭\n" in out


@pytest.mark.parametrize(("day", "confirmed"), [("2026-02-02", False), ("2026-02-03", True)])
def test_analyze_with_date_shows_state_after_that_close(
    config_dir: Path, analysis_db: str, capsys: pytest.CaptureFixture[str], day: str, confirmed: bool
) -> None:
    # 01-30 的頭在 02-03 跌破 MA5 時確認；以 02-03 為「今日」就會計入
    assert main(["--config-dir", str(config_dir), "analyze", analysis_db, "--date", day]) == EXIT_OK

    out = capsys.readouterr().out
    assert f"今日 {day}" in out
    assert ("2026-01-30  頭  123.00" in out) is confirmed


def test_analyze_explains_break_correction(
    config_dir: Path, analysis_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with SqlitePriceStore(config_dir.parent / "data" / "screener.db") as store:
        store.upsert_bars([DailyBar(analysis_db, date(2026, 2, 5), 103.0, 103.0, 103.0, 103.0, 1000)])

    assert main(["--config-dir", str(config_dir), "analyze", analysis_db]) == EXIT_OK

    assert "趨勢：盤整（破位修正：2026-01-30 之後最低收盤 103.00 < 前底 104.00）" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["9999"], "觀察清單沒有這些代號：9999"),
        (["{code}", "--date", "2026-01-10"], "資料庫沒有這些股票 2026-01-10 的日 K"),
    ],
    ids=["unknown-code", "no-bar-on-date"],
)
def test_analyze_invalid_request_fails(config_dir: Path, analysis_db: str, args: list[str], message: str) -> None:
    argv = [a.format(code=analysis_db) for a in args]

    assert main(["--config-dir", str(config_dir), "analyze", *argv]) == EXIT_FAILED

    assert message in read_log(config_dir)


def test_analyze_without_database_fails_without_creating_it(config_dir: Path) -> None:
    code = read_yaml(config_dir / "watchlist.yaml")["stocks"][0]["code"]

    assert main(["--config-dir", str(config_dir), "analyze", code]) == EXIT_FAILED

    assert "找不到資料庫" in read_log(config_dir)
    assert not (config_dir.parent / "data" / "screener.db").exists()


# --- scan --replay -----------------------------------------------------------

def test_replay_dry_run_prints_report(
    config_dir: Path, analysis_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config-dir", str(config_dir), "scan", "--replay", "2026-02-05", "--dry-run"]) == EXIT_OK

    out = capsys.readouterr().out
    assert out.startswith("【回放】2026-02-05（以收盤 K 棒計算）")
    assert f"⚠️ {len(read_yaml(config_dir / 'watchlist.yaml')['stocks']) - 1} 檔當日沒有日 K" in out


def test_unknown_rule_id_exits_with_config_error(config_dir: Path, analysis_db: str) -> None:
    rules = read_yaml(config_dir / "rules.yaml")
    rules["entry_rules"][0]["id"] = "bull_ma5_breakuot"
    write_yaml(config_dir / "rules.yaml", rules)

    assert main(["--config-dir", str(config_dir), "scan", "--replay", "2026-02-05", "--dry-run"]) == EXIT_CONFIG_ERROR

    assert "設定檔錯誤：rules.yaml > entry_rules > bull_ma5_breakuot：未知的規則 id" in read_log(config_dir)


def read_log(config_dir: Path) -> str:
    log_files = list((config_dir.parent / "logs").glob("screener-*.log"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert "觀察清單" in log_text
    return log_text


# --- scan --dry-run（盤中）---------------------------------------------------

class FakeMisClient:
    """取代 MisClient：回傳 2026-01-02 的報價，因此今天一定是「非交易日」。"""

    def __init__(self, settings: Any) -> None:
        pass

    def fetch_quotes(self, stocks: Any) -> dict[str, Quote]:
        return {stock.code: Quote(stock.code, date(2026, 1, 2), 10.0, 11.0, 9.0, 10.5, 10.0, 100, 10.4, 10.5)
                for stock in stocks}


def test_intraday_dry_run_on_non_trading_day_exits_quietly(
    config_dir: Path, analysis_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(intraday_scan, "MisClient", FakeMisClient)

    assert main(["--config-dir", str(config_dir), "scan", "--dry-run"]) == EXIT_OK

    assert capsys.readouterr().out == ""
    assert "非交易日，不掃描" in read_log(config_dir)


# --- Discord 推播與錯誤通知 --------------------------------------------------

WEBHOOK_URL = "https://discord.com/api/webhooks/123/test-token"


class FakeWebhook:
    """取代 DiscordWebhook：記錄送出的訊息；fail 為 True 時拋出 NotifyError。"""

    sent: list[str] = []
    fail = False

    def __init__(self, settings: Any, url: str) -> None:
        assert url == WEBHOOK_URL

    def send(self, content: str) -> None:
        if self.fail:
            raise NotifyError("Discord 拒絕訊息（HTTP 404：Unknown Webhook）")
        self.sent.append(content)


@pytest.fixture
def webhook(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> type[FakeWebhook]:
    """在 .env 加上 webhook（conftest 已把 .env 指向暫存目錄），並換掉 DiscordWebhook。"""
    with (config_dir.parent / ".env").open("a", encoding="utf-8") as env:
        env.write(f"DISCORD_WEBHOOK_URL={WEBHOOK_URL}\n")
    monkeypatch.setattr(cli, "DiscordWebhook", FakeWebhook)
    monkeypatch.setattr(FakeWebhook, "sent", [])
    return FakeWebhook


def test_scan_failure_sends_error_notification(config_dir: Path, webhook: type[FakeWebhook]) -> None:
    assert main(["--config-dir", str(config_dir), "scan", "--replay", "2026-02-05"]) == EXIT_FAILED

    (message,) = webhook.sent
    lines = message.splitlines()
    assert lines[0].startswith("❌ 回放 2026-02-05執行失敗（")
    assert "找不到資料庫" in lines[1]
    assert lines[2].startswith("詳細內容請看 log：") and lines[2].endswith(".log")


def test_rules_error_also_sends_error_notification(config_dir: Path, webhook: type[FakeWebhook]) -> None:
    rules = read_yaml(config_dir / "rules.yaml")
    rules["entry_rules"][0]["id"] = "bull_ma5_breakuot"
    write_yaml(config_dir / "rules.yaml", rules)

    assert main(["--config-dir", str(config_dir), "scan"]) == EXIT_CONFIG_ERROR

    (message,) = webhook.sent
    assert message.startswith("❌ 盤中選股執行失敗（") and "設定檔錯誤" in message


def test_unexpected_error_notification_names_the_exception(
    config_dir: Path, webhook: type[FakeWebhook], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise KeyError("close")

    monkeypatch.setattr(intraday_scan, "run", broken)

    assert main(["--config-dir", str(config_dir), "scan"]) == EXIT_FAILED

    assert "KeyError: 'close'" in webhook.sent[0]
    assert "Traceback" in read_log(config_dir)


def test_failed_error_notification_is_only_logged(
    config_dir: Path, webhook: type[FakeWebhook], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webhook, "fail", True)

    assert main(["--config-dir", str(config_dir), "scan", "--replay", "2026-02-05"]) == EXIT_FAILED

    assert "錯誤通知推送失敗：Discord 拒絕訊息" in read_log(config_dir)


def test_replay_is_pushed(config_dir: Path, analysis_db: str, webhook: type[FakeWebhook]) -> None:
    assert main(["--config-dir", str(config_dir), "scan", "--replay", "2026-02-05"]) == EXIT_OK

    assert webhook.sent[0].startswith("【回放】2026-02-05（以收盤 K 棒計算）")
    assert "已推播 Discord" in read_log(config_dir)


def test_update_failure_sends_error_notification(
    config_dir: Path, fake_finmind: type[FakeFinMindClient], webhook: type[FakeWebhook],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fake_finmind, "fail_on", read_yaml(config_dir / "watchlist.yaml")["stocks"][1]["code"])

    assert main(["--config-dir", str(config_dir), "update"]) == EXIT_FAILED

    (message,) = webhook.sent
    assert message.startswith("❌ 盤後更新執行失敗（") and "抓取失敗" in message


def test_successful_update_sends_nothing(
    config_dir: Path, fake_finmind: type[FakeFinMindClient], webhook: type[FakeWebhook]
) -> None:
    assert main(["--config-dir", str(config_dir), "update"]) == EXIT_OK

    assert webhook.sent == []


def test_update_without_webhook_still_runs(config_dir: Path, fake_finmind: type[FakeFinMindClient]) -> None:
    assert main(["--config-dir", str(config_dir), "update"]) == EXIT_OK

    assert "未設定 DISCORD_WEBHOOK_URL（.env），失敗時不會推送錯誤通知" in read_log(config_dir)


def test_dry_run_never_uses_webhook(config_dir: Path, webhook: type[FakeWebhook]) -> None:
    assert main(["--config-dir", str(config_dir), "scan", "--replay", "2026-02-05", "--dry-run"]) == EXIT_FAILED

    assert webhook.sent == []
