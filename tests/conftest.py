import shutil
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import requests
import yaml

from screener.logging_setup import reset_logging
from screener.models import Market, StockContext, StockInfo, Trend

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG_DIR = REPO_ROOT / "config"


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> pd.DataFrame:
    """讀取 tests/fixtures/<name>.csv 的手工 K 棒資料（含說明用的額外欄位），索引為日期。"""
    frame = pd.read_csv(FIXTURES_DIR / f"{name}.csv", comment="#", dtype={"date": str})
    frame.index = pd.Index([date.fromisoformat(d) for d in frame.pop("date")], name="date")
    return frame


def to_bars(frame: pd.DataFrame, default_volume: int = 1000) -> pd.DataFrame:
    """取出 K 棒欄位；沒有開高低時等於收盤，沒有成交量時為 default_volume 張。"""
    close = frame["close"].astype(float)
    return pd.DataFrame(
        {
            "open": frame.get("open", close).astype(float),
            "high": frame.get("high", close).astype(float),
            "low": frame.get("low", close).astype(float),
            "close": close,
            "volume": frame.get("volume", pd.Series(default_volume, index=frame.index)).astype(int),
        },
        index=frame.index,
    )


def make_bars(closes: Sequence[float], start: date = date(2026, 1, 5)) -> pd.DataFrame:
    """以收盤價建立 start 起連續平日的 K 棒（開高低等於收盤）。"""
    index = pd.Index(pd.bdate_range(start, periods=len(closes)).date, name="date")
    return to_bars(pd.DataFrame({"close": closes}, index=index))


def set_bar(bars: pd.DataFrame, position: int, **values: float) -> pd.DataFrame:
    """回傳修改第 position 根（-1 為今日）K 棒欄位後的複本，例：set_bar(bars, -1, open=10, volume=1500)。"""
    bars = bars.copy()
    for column, value in values.items():
        bars.loc[bars.index[position], column] = value
    return bars


TEST_STOCK = StockInfo(code="2330", name="台積電", market=Market.TSE, group="半導體", subcategory="製造")


def make_context(bars: pd.DataFrame, trend: Trend = Trend.UNKNOWN, info: StockInfo = TEST_STOCK) -> StockContext:
    """直接建立 StockContext（不計算轉折點），供只看 K 棒與均線的規則測試使用。"""
    return StockContext(info=info, bars=bars, is_tentative=False, pivots=[], trend=trend)


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
