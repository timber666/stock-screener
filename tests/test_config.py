import copy
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_CONFIG_DIR, read_yaml, write_yaml
from screener.config import (ConfigError, RuleConfig, RulesConfig, TrendConfig, load_config, load_research_config,
                             load_rules, load_secrets, rules_fingerprint)
from screener.models import Market

BASE_WATCHLIST: dict[str, Any] = {
    "industries": {"PCB": ["CCL", "載板"], "記憶體": []},
    "stocks": [
        {"code": "2383", "name": "台光電", "market": "tse", "industry": "PCB/CCL"},
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
    assert config.settings.snapshot_dir == Path("data/snapshots")
    assert config.rules.trend.pivot_ma == 5
    assert [r.id for r in config.rules.trigger_rules] == [
        "bull_ma5_breakout",
        "range_breakout_volume",
        "ma_bull_alignment",
        "bear_ma5_breakdown",
        "range_breakdown",
        "ma_bear_alignment",
    ]
    assert [r.id for r in config.rules.entry_rules] == ["bull_ma5_entry"]
    assert [r.id for r in config.rules.exit_rules] == ["below_ma_exit", "max_hold_exit"]
    assert config.watchlist.stocks


# --- watchlist.yaml ---------------------------------------------------------

def test_watchlist_parses_stock(config_dir: Path) -> None:
    config = load_with_watchlist(config_dir, BASE_WATCHLIST)

    stock = config.watchlist.stocks[0]
    assert stock.code == "2383"
    assert stock.market is Market.TSE
    assert (stock.group, stock.subcategory, stock.industry) == ("PCB", "CCL", "PCB/CCL")
    assert config.watchlist.industries["PCB"] == ("CCL", "載板")


def test_group_without_subcategories_accepts_group_only(config_dir: Path) -> None:
    stock = load_with_watchlist(config_dir, with_stock(industry="記憶體")).watchlist.stocks[0]

    assert (stock.group, stock.subcategory, stock.industry) == ("記憶體", None, "記憶體")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"industry": "PCV/CCL"}, "未知的族群「PCV」"),
        ({"industry": "PCB/HDI"}, "沒有子分類「HDI」"),
        ({"industry": "PCB"}, "必須寫到子分類"),
        ({"industry": "記憶體/DRAM"}, "未列子分類"),
        ({"market": "twse"}, "應為 tse（上市）或 otc（上櫃）"),
        ({"code": 2383}, "請加引號"),
        ({"themes": ["CPO"]}, "未知的欄位 `themes`"),     # 題材已移除（D31）
    ],
)
def test_invalid_stock_is_rejected(config_dir: Path, overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_with_watchlist(config_dir, with_stock(**overrides))


def test_themes_list_is_rejected(config_dir: Path) -> None:
    # 題材已移除（D31）：舊的設定檔留著 themes 時要報錯，而不是默默忽略
    with pytest.raises(ConfigError, match="未知的欄位 `themes`"):
        load_with_watchlist(config_dir, {**BASE_WATCHLIST, "themes": ["CPO"]})


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
        {"code": "2222", "name": "乙", "market": "twse", "industry": "PCB/CCL"},
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


@pytest.mark.parametrize("section", ["isin", "events"])
def test_research_settings_are_not_accepted_in_settings_yaml(config_dir: Path, section: str) -> None:
    # 研究的設定只放 research.yaml（PROJECT_PLAN.md 10.4 規則 4）：settings.yaml 每個指令都會載入
    edit_yaml(config_dir / "settings.yaml", lambda d: d.update({section: {}}))

    with pytest.raises(ConfigError, match=f"未知的欄位 `{section}`"):
        load_config(config_dir)


def test_unknown_setting_is_rejected(config_dir: Path) -> None:
    edit_yaml(config_dir / "settings.yaml", lambda d: d["mis"].update(batchsize=10))

    with pytest.raises(ConfigError, match="未知的欄位 `batchsize`"):
        load_config(config_dir)


def test_missing_config_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="找不到設定檔"):
        load_config(tmp_path)


# --- research.yaml ----------------------------------------------------------

def test_repo_research_config_loads() -> None:
    research = load_research_config(REPO_CONFIG_DIR / "research.yaml")

    events = research.events
    assert (events.refetch_days, events.reference_tolerance_pct, events.dividend_lookahead_days) == (7, 0.5, 30)
    assert research.isin.timeout_sec == 60.0
    assert research.signal_report.horizons == (1, 5)
    assert research.signal_report.top_n == 3
    assert research.signal_report.output_dir == Path("data/research/signal_report")
    assert research.signal_report.backtest_summary == Path("data/backtest/summary.csv")
    market = research.market
    assert (market.db_path, market.start, market.signal_start, market.compare_start) == (
        Path("data/market.db"), date(2015, 1, 1), date(2015, 7, 1), date(2021, 1, 1))
    assert (market.universe.size, market.universe.lookback_days) == (500, 60)       # R7
    assert market.exchange.request_interval_sec == 5.0


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("events", "dividend_lookahead_days", -1, "events > dividend_lookahead_days：不可為負數"),
        ("events", "dividend_lookahead_days", 1.5, "events > dividend_lookahead_days：應為整數"),
        ("events", "refetch_days", -1, "events > refetch_days：不可為負數"),
        ("events", "reference_tolerance_pct", 0, "events > reference_tolerance_pct：必須大於 0"),
        ("isin", "retry", -1, "isin > retry：不可為負數"),
        ("signal_report", "horizons", [5, 1], "signal_report > horizons：應由小到大且不重複"),
        ("signal_report", "horizons", [], "signal_report > horizons：應為正整數陣列"),
        ("signal_report", "top_n", -1, "signal_report > top_n：不可為負數"),
        ("market", "signal_start", date(2014, 12, 31), "market > signal_start：不可早於 start"),
        ("market", "compare_start", date(2015, 6, 30), "market > compare_start：不可早於 signal_start"),
        ("market", "universe", {"size": 0, "lookback_days": 60}, "market > universe > size：必須大於 0"),
        ("market", "universe", {"size": 500}, "market > universe：缺少必要欄位 `lookback_days`"),
        ("market", "exchange", {"timeout_sec": 30}, "market > exchange：缺少必要欄位"),
        ("market", "start", "2015-01-01x", "market > start：應為日期"),
    ],
)
def test_invalid_research_setting_is_rejected(config_dir: Path, section: str, key: str, value: Any,
                                              message: str) -> None:
    edit_yaml(config_dir / "research.yaml", lambda d: d[section].update({key: value}))

    with pytest.raises(ConfigError, match=message):
        load_research_config(config_dir / "research.yaml")


def test_missing_research_section_is_rejected(config_dir: Path) -> None:
    edit_yaml(config_dir / "research.yaml", lambda d: d.pop("signal_report"))

    with pytest.raises(ConfigError, match="缺少必要欄位 `signal_report`"):
        load_research_config(config_dir / "research.yaml")


def test_broken_research_config_does_not_affect_daily_config(config_dir: Path) -> None:
    (config_dir / "research.yaml").write_text("events: [", encoding="utf-8")

    assert load_config(config_dir).watchlist.stocks


# --- rules.yaml -------------------------------------------------------------

def test_rules_fingerprint_ignores_comments_but_not_values(config_dir: Path) -> None:
    path = config_dir / "rules.yaml"
    original = rules_fingerprint(load_rules(path))

    path.write_text("# 只改註解\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert rules_fingerprint(load_rules(path)) == original

    edit_yaml(path, lambda d: d["trigger_rules"][1]["params"].update(volume_ratio=1.5))
    changed = rules_fingerprint(load_rules(path))
    assert changed != original and len(changed) == 12


def test_rules_fingerprint_is_unchanged_by_renaming() -> None:
    # 已記錄的推播存的是改名前算出的指紋；entry_rules 改名為 trigger_rules（D49）不可以讓成績單誤判規則有變更。
    # 預期值由改名前的程式算出
    rules = RulesConfig(trend=TrendConfig(pivot_ma=5, lookback_days=120, bull_break_to_range=True,
                                          bear_break_to_range=True),
                        filters=(), trigger_rules=(RuleConfig(id="a", enabled=True, params={"x": 1}),), exit_rules=())

    assert rules_fingerprint(rules) == "3f2828187c1e"


def test_rules_fingerprint_ignores_entry_rules(config_dir: Path) -> None:
    # 進場規則只影響推播標示，不影響訊號；改動時成績單不可以標註「規則有變更」（D54）
    path = config_dir / "rules.yaml"
    original = rules_fingerprint(load_rules(path))

    edit_yaml(path, lambda d: d["entry_rules"][0]["params"].update(ma=10))
    assert rules_fingerprint(load_rules(path)) == original

    edit_yaml(path, lambda d: d.update(entry_rules=[]))
    assert rules_fingerprint(load_rules(path)) == original


def test_rules_fingerprint_ignores_exit_rules(config_dir: Path) -> None:
    # 出場規則只用於交易回測；加入或修改都不可以讓成績單標註「規則有變更」（D56）
    path = config_dir / "rules.yaml"
    original = rules_fingerprint(load_rules(path))

    edit_yaml(path, lambda d: d["exit_rules"][1]["params"].update(days=10))
    assert rules_fingerprint(load_rules(path)) == original

    edit_yaml(path, lambda d: d.update(exit_rules=[]))
    assert rules_fingerprint(load_rules(path)) == original


def test_old_layout_with_triggers_under_entry_rules_is_rejected(config_dir: Path) -> None:
    edit_yaml(config_dir / "rules.yaml", lambda d: d.update(entry_rules=d.pop("trigger_rules")))

    with pytest.raises(ConfigError, match="缺少必要欄位 `trigger_rules`"):
        load_config(config_dir)


def test_duplicate_rule_id_is_rejected(config_dir: Path) -> None:
    edit_yaml(
        config_dir / "rules.yaml",
        lambda d: d["trigger_rules"].append(copy.deepcopy(d["trigger_rules"][0])),
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
    monkeypatch.delenv("DISCORD_RESEARCH_WEBHOOK_URL", raising=False)


@pytest.mark.usefixtures("clean_env")
def test_secrets_are_read_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FINMIND_TOKEN=token-from-file\nDISCORD_WEBHOOK_URL=\n"
                        "DISCORD_RESEARCH_WEBHOOK_URL=https://discord.com/api/webhooks/9/research\n", encoding="utf-8")

    secrets = load_secrets(env_file)

    assert secrets.finmind_token == "token-from-file"
    assert secrets.discord_webhook_url is None
    assert secrets.discord_research_webhook_url == "https://discord.com/api/webhooks/9/research"


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
    assert secrets.discord_research_webhook_url is None
