"""即時系統的設定：realtime.yaml、daytrade.yaml、.env 的 Shioaji 金鑰（REALTIME_PLAN.md 4.2）。"""

from datetime import time
from pathlib import Path

import pytest

from conftest import REPO_CONFIG_DIR, read_yaml, write_yaml
from screener.config import ConfigError
from screener.realtime.config import load_daytrade_config, load_realtime_config, load_shioaji_keys


def test_repo_config_loads() -> None:
    realtime = load_realtime_config(REPO_CONFIG_DIR / "realtime.yaml")
    daytrade = load_daytrade_config(REPO_CONFIG_DIR / "daytrade.yaml")

    assert realtime.shioaji.simulation is False
    assert realtime.shioaji.subscribe_trade is False
    assert realtime.record.end_time == time(13, 35)
    assert daytrade.session.open == time(9, 0) and daytrade.session.close == time(13, 30)
    assert [(spec.id, spec.bar_minutes, spec.period) for spec in daytrade.moving_averages] == [("ma3", 3, 3),
                                                                                              ("ma5", 5, 5)]
    assert daytrade.bar_minutes == (1, 3, 5)


def test_compare_codes_are_in_watchlist() -> None:
    realtime = load_realtime_config(REPO_CONFIG_DIR / "realtime.yaml")
    watchlist = {item["code"] for item in read_yaml(REPO_CONFIG_DIR / "watchlist.yaml")["stocks"]}

    assert set(realtime.compare_codes) <= watchlist


@pytest.mark.parametrize("change, message", [
    (lambda d: d["shioaji"].update(unknown=1), "未知的欄位"),
    (lambda d: d["shioaji"].pop("timeout_ms"), "缺少必要欄位"),
    (lambda d: d["record"].update(end_time="1:35pm"), "時間格式"),
    (lambda d: d.update(compare_codes=[4977]), "代號應為字串"),
    (lambda d: d["probe"].update(snapshot_batches=[50, 0]), "正整數"),
    (lambda d: d["record"].update(connections=0), "必須大於 0"),
])
def test_realtime_config_errors(tmp_path: Path, change, message: str) -> None:
    data = read_yaml(REPO_CONFIG_DIR / "realtime.yaml")
    change(data)
    path = tmp_path / "realtime.yaml"
    write_yaml(path, data)

    with pytest.raises(ConfigError, match=message):
        load_realtime_config(path)


@pytest.mark.parametrize("change, message", [
    (lambda d: d["session"].update(closing_call="13:35"), "open < closing_call < close"),
    (lambda d: d["moving_averages"].append({"id": "ma3", "bar_minutes": 3, "period": 5}), "id 重複"),
    (lambda d: d["moving_averages"][0].update(period=0), "必須大於 0"),
])
def test_daytrade_config_errors(tmp_path: Path, change, message: str) -> None:
    data = read_yaml(REPO_CONFIG_DIR / "daytrade.yaml")
    change(data)
    path = tmp_path / "daytrade.yaml"
    write_yaml(path, data)

    with pytest.raises(ConfigError, match=message):
        load_daytrade_config(path)


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="找不到設定檔"):
        load_realtime_config(tmp_path / "realtime.yaml")


def test_shioaji_keys_from_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SHIOAJI_API_KEY=key-from-file\nSHIOAJI_SECRET_KEY=secret-from-file\n", encoding="utf-8")

    keys = load_shioaji_keys(env)

    assert (keys.api_key, keys.secret_key) == ("key-from-file", "secret-from-file")
    assert "key-from-file" not in repr(keys) and "secret" not in repr(keys)


def test_missing_shioaji_keys_names_them_without_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SHIOAJI_API_KEY=only-key\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="SHIOAJI_SECRET_KEY") as exc_info:
        load_shioaji_keys(env)
    assert "only-key" not in str(exc_info.value)
