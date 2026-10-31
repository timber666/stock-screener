import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_ROOT, read_yaml, write_yaml
from screener.__main__ import EXIT_CONFIG_ERROR, EXIT_FAILED, EXIT_OK, main
from screener.data.base import DataSourceError
from screener.data.db import SqlitePriceStore
from screener.jobs import daily_update
from screener.models import DailyBar


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


def test_invalid_replay_date_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["scan", "--replay", "2026-13-01"])

    assert exc_info.value.code == 2


def test_config_error_exits_with_config_error_code(tmp_path: Path) -> None:
    assert main(["--config-dir", str(tmp_path / "missing"), "update"]) == EXIT_CONFIG_ERROR


@pytest.mark.parametrize("argv", [["scan", "--dry-run"], ["scan", "--replay", "2026-10-29"]])
def test_unimplemented_command_fails_and_writes_log(config_dir: Path, argv: list[str]) -> None:
    assert main(["--config-dir", str(config_dir), *argv]) == EXIT_FAILED

    assert "尚未實作" in read_log(config_dir)


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


def read_log(config_dir: Path) -> str:
    log_files = list((config_dir.parent / "logs").glob("screener-*.log"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert "觀察清單" in log_text
    return log_text
