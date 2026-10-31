import copy
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_CONFIG_DIR, read_yaml, write_yaml
from screener.config import ConfigError, load_config, load_secrets
from screener.models import Market

BASE_WATCHLIST: dict[str, Any] = {
    "industries": {"PCB": ["CCL", "載板"], "記憶體": []},
    "themes": ["AI伺服器", "CPO"],
    "stocks": [
        {"code": "2383", "name": "台光電", "market": "tse", "industry": "PCB/CCL", "themes": ["AI伺服器"]},
    ],
}


def load_with_watchlist(config_dir: Path, watchlist: dict[str, Any]):
    write_yaml(config_dir / "watchlist.yaml", watchlist)
    return load_config(config_dir)


def with_stock(**overrides: Any) -> dict[str, Any]:
    watchlist = copy.deepcopy(BASE_WATCHLIST)
    watchlist["stocks"][0].update(overrides)
    return watchlist


def edit_yaml(path: Path, edit) -> None:
    data = read_yaml(path)
    edit(data)
    write_yaml(path, data)


# --- repo 內的設定檔 --------------------------------------------------------

def test_repo_config_loads() -> None:
    config = load_config(REPO_CONFIG_DIR)

    assert config.settings.mis.request_interval_sec == 3.0
    assert config.settings.mis.batch_size == 50
    assert config.settings.mis.missing_price_retry == 1
    assert config.settings.finmind.timeout_sec == 30.0
    assert config.rules.trend.pivot_ma == 5
    assert [r.id for r in config.rules.entry_rules] == [
        "bull_ma5_breakout",
        "range_breakout_volume",
        "ma_bull_alignment",
        "bear_ma5_breakdown",
        "range_breakdown",
        "ma_bear_alignment",
    ]
    assert config.rules.exit_rules == ()
    assert config.watchlist.stocks


# --- watchlist.yaml ---------------------------------------------------------

def test_watchlist_parses_stock(config_dir: Path) -> None:
    config = load_with_watchlist(config_dir, BASE_WATCHLIST)

    stock = config.watchlist.stocks[0]
    assert stock.code == "2383"
    assert stock.market is Market.TSE
    assert (stock.group, stock.subcategory, stock.industry) == ("PCB", "CCL", "PCB/CCL")
    assert stock.themes == ("AI伺服器",)
    assert config.watchlist.industries["PCB"] == ("CCL", "載板")


def test_group_without_subcategories_accepts_group_only(config_dir: Path) -> None:
    watchlist = with_stock(industry="記憶體")
    del watchlist["stocks"][0]["themes"]

    stock = load_with_watchlist(config_dir, watchlist).watchlist.stocks[0]

    assert (stock.group, stock.subcategory, stock.industry) == ("記憶體", None, "記憶體")
    assert stock.themes == ()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"industry": "PCV/CCL"}, "未知的族群「PCV」"),
        ({"industry": "PCB/HDI"}, "沒有子分類「HDI」"),
        ({"industry": "PCB"}, "必須寫到子分類"),
        ({"industry": "記憶體/DRAM"}, "未列子分類"),
        ({"themes": ["AI"]}, "未知的題材「AI」"),
        ({"market": "twse"}, "應為 tse（上市）或 otc（上櫃）"),
        ({"code": 2383}, "請加引號"),
        ({"theme": ["CPO"]}, "未知的欄位 `theme`"),
    ],
)
def test_invalid_stock_is_rejected(config_dir: Path, overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_with_watchlist(config_dir, with_stock(**overrides))


def test_missing_stock_field_is_rejected(config_dir: Path) -> None:
    watchlist = copy.deepcopy(BASE_WATCHLIST)
    del watchlist["stocks"][0]["industry"]

    with pytest.raises(ConfigError, match="缺少必要欄位 `industry`"):
        load_with_watchlist(config_dir, watchlist)


def test_duplicate_code_is_rejected(config_dir: Path) -> None:
    watchlist = copy.deepcopy(BASE_WATCHLIST)
    watchlist["stocks"].append(copy.deepcopy(watchlist["stocks"][0]))

    with pytest.raises(ConfigError, match="代號重複：2383"):
        load_with_watchlist(config_dir, watchlist)


def test_all_stock_errors_are_reported_together(config_dir: Path) -> None:
    watchlist = copy.deepcopy(BASE_WATCHLIST)
    watchlist["stocks"] = [
        {"code": "1111", "name": "甲", "market": "tse", "industry": "PCB/HDI"},
        {"code": "2222", "name": "乙", "market": "otc", "industry": "PCB/CCL", "themes": ["矽光子"]},
    ]

    with pytest.raises(ConfigError) as exc_info:
        load_with_watchlist(config_dir, watchlist)

    message = str(exc_info.value)
    assert "2 個錯誤" in message
    assert "1111" in message and "2222" in message


def test_duplicate_subcategory_is_rejected(config_dir: Path) -> None:
    watchlist = copy.deepcopy(BASE_WATCHLIST)
    watchlist["industries"]["PCB"] = ["CCL", "CCL"]

    with pytest.raises(ConfigError, match="子分類重複"):
        load_with_watchlist(config_dir, watchlist)


# --- settings.yaml ----------------------------------------------------------

def test_missing_setting_is_rejected(config_dir: Path) -> None:
    edit_yaml(config_dir / "settings.yaml", lambda d: d.pop("backfill_days"))

    with pytest.raises(ConfigError, match="缺少必要欄位 `backfill_days`"):
        load_config(config_dir)


def test_bool_is_not_accepted_as_int(config_dir: Path) -> None:
    edit_yaml(config_dir / "settings.yaml", lambda d: d["mis"].update(retry=True))

    with pytest.raises(ConfigError, match="retry：應為整數"):
        load_config(config_dir)


def test_non_positive_backfill_days_is_rejected(config_dir: Path) -> None:
    edit_yaml(config_dir / "settings.yaml", lambda d: d.update(backfill_days=0))

    with pytest.raises(ConfigError, match="backfill_days：必須大於 0"):
        load_config(config_dir)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("timeout_sec", 0, "timeout_sec：必須大於 0"),
        ("retry", -1, "retry：不可為負數"),
        ("retry_backoff_sec", -1, "retry_backoff_sec：不可為負數"),
        ("request_interval_sec", "1", "request_interval_sec：應為數字"),
    ],
)
@pytest.mark.parametrize("section", ["finmind", "mis", "discord"])
def test_invalid_request_setting_is_rejected(config_dir: Path, section: str, key: str, value: Any,
                                             message: str) -> None:
    edit_yaml(config_dir / "settings.yaml", lambda d: d[section].update({key: value}))

    with pytest.raises(ConfigError, match=f"{section} > {message}"):
        load_config(config_dir)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("batch_size", 0, "batch_size：必須大於 0"),
        ("missing_price_retry", -1, "missing_price_retry：不可為負數"),
        ("missing_price_wait_sec", -1, "missing_price_wait_sec：不可為負數"),
    ],
)
def test_invalid_mis_setting_is_rejected(config_dir: Path, key: str, value: Any, message: str) -> None:
    edit_yaml(config_dir / "settings.yaml", lambda d: d["mis"].update({key: value}))

    with pytest.raises(ConfigError, match=f"mis > {message}"):
        load_config(config_dir)


def test_unknown_setting_is_rejected(config_dir: Path) -> None:
    edit_yaml(config_dir / "settings.yaml", lambda d: d["mis"].update(batchsize=10))

    with pytest.raises(ConfigError, match="未知的欄位 `batchsize`"):
        load_config(config_dir)


def test_missing_config_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="找不到設定檔"):
        load_config(tmp_path)


# --- rules.yaml -------------------------------------------------------------

def test_duplicate_rule_id_is_rejected(config_dir: Path) -> None:
    edit_yaml(
        config_dir / "rules.yaml",
        lambda d: d["entry_rules"].append(copy.deepcopy(d["entry_rules"][0])),
    )

    with pytest.raises(ConfigError, match="規則 id 重複：bull_ma5_breakout"):
        load_config(config_dir)


def test_rule_without_params_is_rejected(config_dir: Path) -> None:
    edit_yaml(config_dir / "rules.yaml", lambda d: d["filters"][0].pop("params"))

    with pytest.raises(ConfigError, match="缺少必要欄位 `params`"):
        load_config(config_dir)


# --- .env -------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)


@pytest.mark.usefixtures("clean_env")
def test_secrets_are_read_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FINMIND_TOKEN=token-from-file\nDISCORD_WEBHOOK_URL=\n", encoding="utf-8")

    secrets = load_secrets(env_file)

    assert secrets.finmind_token == "token-from-file"
    assert secrets.discord_webhook_url is None


@pytest.mark.usefixtures("clean_env")
def test_environment_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FINMIND_TOKEN=token-from-file\n", encoding="utf-8")
    monkeypatch.setenv("FINMIND_TOKEN", "token-from-env")

    assert load_secrets(env_file).finmind_token == "token-from-env"


@pytest.mark.usefixtures("clean_env")
def test_missing_env_file_gives_empty_secrets(tmp_path: Path) -> None:
    secrets = load_secrets(tmp_path / ".env")

    assert secrets.finmind_token is None
    assert secrets.discord_webhook_url is None
