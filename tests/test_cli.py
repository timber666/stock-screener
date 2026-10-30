import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from screener.__main__ import EXIT_CONFIG_ERROR, EXIT_FAILED, main


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


@pytest.mark.parametrize("argv", [["update"], ["scan", "--dry-run"], ["scan", "--replay", "2026-10-29"]])
def test_unimplemented_command_fails_and_writes_log(config_dir: Path, argv: list[str]) -> None:
    assert main(["--config-dir", str(config_dir), *argv]) == EXIT_FAILED

    log_files = list((config_dir.parent / "logs").glob("screener-*.log"))
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert "觀察清單" in log_text
    assert "尚未實作" in log_text
