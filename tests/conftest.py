import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml

from screener.logging_setup import reset_logging

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG_DIR = REPO_ROOT / "config"


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_yaml(path: Path, data: Any) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """複製 repo 的設定檔到暫存目錄，並把資料庫與 log 指向暫存目錄。"""
    target = tmp_path / "config"
    shutil.copytree(REPO_CONFIG_DIR, target)
    settings_path = target / "settings.yaml"
    settings = read_yaml(settings_path)
    settings["db_path"] = str(tmp_path / "data" / "screener.db")
    settings["log_dir"] = str(tmp_path / "logs")
    write_yaml(settings_path, settings)
    return target


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    # 關閉 log 檔 handler，避免 Windows 上暫存目錄的檔案被鎖住
    yield
    reset_logging()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """測試不可依賴網路（PROJECT_PLAN.md 第 13 節）：任何真的送出的 HTTP 請求都會失敗。"""

    def blocked(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("測試不可連線到網路；請注入假的 session 或資料來源")

    monkeypatch.setattr(requests.Session, "send", blocked)
