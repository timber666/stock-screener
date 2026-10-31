"""載入並驗證 config/ 下的設定檔與 .env（PROJECT_PLAN.md 第 9 節）。

所有可調整的數字都來自設定檔，程式中不提供預設值：缺少欄位、型別不符或出現
未知欄位時一律報錯，避免打錯字造成設定被悄悄忽略。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from screener.models import Market, StockInfo


class ConfigError(Exception):
    """設定檔內容不正確。"""


@dataclass(frozen=True)
class RequestSettings:
    """HTTP 請求的逾時、重試與間隔，FinMind 與 MIS 共用。"""

    timeout_sec: float
    retry: int                      # 連線失敗、逾時或伺服器錯誤時的重試次數
    retry_backoff_sec: float        # 第 n 次重試前等待 n × 此秒數
    request_interval_sec: float     # 兩次請求的最短間隔


@dataclass(frozen=True)
class FinMindSettings(RequestSettings):
    pass


@dataclass(frozen=True)
class MisSettings(RequestSettings):
    batch_size: int                 # 每次請求的股票數
    missing_price_retry: int        # 成交價為 "-" 時重新查詢的次數（8.2）
    missing_price_wait_sec: float   # 重新查詢前等待的秒數


@dataclass(frozen=True)
class DiscordSettings(RequestSettings):
    pass


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_dir: Path
    backfill_days: int
    finmind: FinMindSettings
    mis: MisSettings
    discord: DiscordSettings


@dataclass(frozen=True)
class TrendConfig:
    pivot_ma: int
    lookback_days: int
    bull_break_to_range: bool
    bear_break_to_range: bool


@dataclass(frozen=True)
class RuleConfig:
    id: str
    enabled: bool
    params: dict[str, Any]          # 各規則的參數由規則自行驗證


@dataclass(frozen=True)
class RulesConfig:
    trend: TrendConfig
    filters: tuple[RuleConfig, ...]
    entry_rules: tuple[RuleConfig, ...]
    exit_rules: tuple[RuleConfig, ...]


@dataclass(frozen=True)
class Watchlist:
    industries: dict[str, tuple[str, ...]]     # 族群 → 子分類（上游到下游）
    stocks: tuple[StockInfo, ...]


@dataclass(frozen=True)
class AppConfig:
    settings: Settings
    watchlist: Watchlist
    rules: RulesConfig


@dataclass(frozen=True)
class Secrets:
    finmind_token: str | None
    discord_webhook_url: str | None


def load_config(config_dir: Path) -> AppConfig:
    return AppConfig(
        settings=_load_settings(config_dir / "settings.yaml"),
        watchlist=_load_watchlist(config_dir / "watchlist.yaml"),
        rules=_load_rules(config_dir / "rules.yaml"),
    )


def load_secrets(env_file: Path) -> Secrets:
    """讀取機密資訊。環境變數優先於 .env；空字串視為未設定。"""
    file_values = dotenv_values(env_file) if env_file.is_file() else {}

    def lookup(name: str) -> str | None:
        return os.environ.get(name) or file_values.get(name) or None

    return Secrets(
        finmind_token=lookup("FINMIND_TOKEN"),
        discord_webhook_url=lookup("DISCORD_WEBHOOK_URL"),
    )


# --- settings.yaml ----------------------------------------------------------

def _load_settings(path: Path) -> Settings:
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"db_path", "log_dir", "backfill_days", "finmind", "mis", "discord"}, where)

    finmind_where = f"{where} > finmind"
    finmind_data = get_field(data, "finmind", dict, where)
    check_keys(finmind_data, _REQUEST_KEYS, finmind_where)
    finmind = FinMindSettings(**_parse_request_settings(finmind_data, finmind_where))

    mis_where = f"{where} > mis"
    mis_data = get_field(data, "mis", dict, where)
    check_keys(mis_data, _REQUEST_KEYS | {"batch_size", "missing_price_retry", "missing_price_wait_sec"}, mis_where)
    mis = MisSettings(
        **_parse_request_settings(mis_data, mis_where),
        batch_size=get_field(mis_data, "batch_size", int, mis_where),
        missing_price_retry=get_field(mis_data, "missing_price_retry", int, mis_where),
        missing_price_wait_sec=get_field(mis_data, "missing_price_wait_sec", float, mis_where),
    )
    require(mis.batch_size > 0, f"{mis_where} > batch_size：必須大於 0")
    require(mis.missing_price_retry >= 0, f"{mis_where} > missing_price_retry：不可為負數")
    require(mis.missing_price_wait_sec >= 0, f"{mis_where} > missing_price_wait_sec：不可為負數")

    discord_where = f"{where} > discord"
    discord_data = get_field(data, "discord", dict, where)
    check_keys(discord_data, _REQUEST_KEYS, discord_where)
    discord = DiscordSettings(**_parse_request_settings(discord_data, discord_where))

    settings = Settings(
        db_path=Path(get_field(data, "db_path", str, where)),
        log_dir=Path(get_field(data, "log_dir", str, where)),
        backfill_days=get_field(data, "backfill_days", int, where),
        finmind=finmind,
        mis=mis,
        discord=discord,
    )
    require(settings.backfill_days > 0, f"{where} > backfill_days：必須大於 0")
    return settings


_REQUEST_KEYS = {"timeout_sec", "retry", "retry_backoff_sec", "request_interval_sec"}


def _parse_request_settings(data: Mapping[str, Any], where: str) -> dict[str, Any]:
    """讀取 RequestSettings 的欄位，回傳建構參數。"""
    values = {
        "timeout_sec": get_field(data, "timeout_sec", float, where),
        "retry": get_field(data, "retry", int, where),
        "retry_backoff_sec": get_field(data, "retry_backoff_sec", float, where),
        "request_interval_sec": get_field(data, "request_interval_sec", float, where),
    }
    require(values["timeout_sec"] > 0, f"{where} > timeout_sec：必須大於 0")
    require(values["retry"] >= 0, f"{where} > retry：不可為負數")
    require(values["retry_backoff_sec"] >= 0, f"{where} > retry_backoff_sec：不可為負數")
    require(values["request_interval_sec"] >= 0, f"{where} > request_interval_sec：不可為負數")
    return values


# --- rules.yaml -------------------------------------------------------------

def _load_rules(path: Path) -> RulesConfig:
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"trend", "filters", "entry_rules", "exit_rules"}, where)

    trend_where = f"{where} > trend"
    trend_data = get_field(data, "trend", dict, where)
    check_keys(
        trend_data,
        {"pivot_ma", "lookback_days", "bull_break_to_range", "bear_break_to_range"},
        trend_where,
    )
    trend = TrendConfig(
        pivot_ma=get_field(trend_data, "pivot_ma", int, trend_where),
        lookback_days=get_field(trend_data, "lookback_days", int, trend_where),
        bull_break_to_range=get_field(trend_data, "bull_break_to_range", bool, trend_where),
        bear_break_to_range=get_field(trend_data, "bear_break_to_range", bool, trend_where),
    )
    require(trend.pivot_ma > 0, f"{trend_where} > pivot_ma：必須大於 0")
    require(trend.lookback_days > 0, f"{trend_where} > lookback_days：必須大於 0")

    return RulesConfig(
        trend=trend,
        filters=_parse_rule_list(data, "filters", where),
        entry_rules=_parse_rule_list(data, "entry_rules", where),
        exit_rules=_parse_rule_list(data, "exit_rules", where),
    )


def _parse_rule_list(data: Mapping[str, Any], key: str, where: str) -> tuple[RuleConfig, ...]:
    rules: list[RuleConfig] = []
    seen: set[str] = set()
    for i, item in enumerate(get_field(data, key, list, where)):
        item_where = f"{where} > {key}[{i}]"
        require(isinstance(item, dict), f"{item_where}：應為 key: value 結構")
        check_keys(item, {"id", "enabled", "params"}, item_where)
        rule = RuleConfig(
            id=get_field(item, "id", str, item_where),
            enabled=get_field(item, "enabled", bool, item_where),
            params=get_field(item, "params", dict, item_where),
        )
        require(rule.id not in seen, f"{item_where}：規則 id 重複：{rule.id}")
        seen.add(rule.id)
        rules.append(rule)
    return tuple(rules)


# --- watchlist.yaml ---------------------------------------------------------

_STOCK_KEYS = {"code", "name", "market", "industry"}


def _load_watchlist(path: Path) -> Watchlist:
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"industries", "stocks"}, where)

    industries: dict[str, tuple[str, ...]] = {}
    for group, subcategories in get_field(data, "industries", dict, where).items():
        group_where = f"{where} > industries > {group}"
        require(isinstance(group, str), f"{group_where}：族群名稱應為字串")
        require(
            isinstance(subcategories, list) and all(isinstance(s, str) for s in subcategories),
            f"{group_where}：子分類應為字串陣列；沒有子分類請寫 []",
        )
        require(len(set(subcategories)) == len(subcategories), f"{group_where}：子分類重複")
        industries[group] = tuple(subcategories)

    # 逐檔驗證並一次列出所有錯誤，方便一次修正
    stocks: list[StockInfo] = []
    errors: list[str] = []
    seen_codes: set[str] = set()
    for i, item in enumerate(get_field(data, "stocks", list, where)):
        try:
            stock = _parse_stock(item, industries, f"{where} > stocks[{i}]")
        except ConfigError as exc:
            errors.append(str(exc))
            continue
        if stock.code in seen_codes:
            errors.append(f"{where} > stocks[{i}]：代號重複：{stock.code}")
            continue
        seen_codes.add(stock.code)
        stocks.append(stock)

    if errors:
        raise ConfigError(f"觀察清單有 {len(errors)} 個錯誤：\n" + "\n".join(f"  - {e}" for e in errors))
    return Watchlist(industries=industries, stocks=tuple(stocks))


def _parse_stock(item: Any, industries: Mapping[str, tuple[str, ...]], where: str) -> StockInfo:
    require(isinstance(item, dict), f"{where}：應為 key: value 結構")
    require(
        not isinstance(item.get("code"), int),
        f"{where} > code：股票代號請加引號，例如 \"{item.get('code')}\"",
    )
    code = get_field(item, "code", str, where)
    where = f"{where}（{code}）"
    check_keys(item, _STOCK_KEYS, where)

    market_value = get_field(item, "market", str, where)
    try:
        market = Market(market_value)
    except ValueError:
        raise ConfigError(f"{where} > market：應為 tse（上市）或 otc（上櫃），實際為 {market_value!r}") from None

    industry = get_field(item, "industry", str, where)
    group, _, subcategory = industry.partition("/")
    require(group in industries, f"{where} > industry：未知的族群「{group}」，請先在 industries 定義")
    if industries[group]:
        require(bool(subcategory), f"{where} > industry：族群「{group}」有子分類，必須寫到子分類，例如「{group}/{industries[group][0]}」")
        require(subcategory in industries[group], f"{where} > industry：族群「{group}」沒有子分類「{subcategory}」")
    else:
        require(not subcategory, f"{where} > industry：族群「{group}」未列子分類，只能寫「{group}」")

    return StockInfo(
        code=code,
        name=get_field(item, "name", str, where),
        market=market,
        group=group,
        subcategory=subcategory or None,
    )


# --- 共用工具（get_field、check_keys、require 也供規則驗證自己的 params）------

_TYPE_NAMES: dict[type, str] = {
    int: "整數",
    float: "數字",
    bool: "true 或 false",
    str: "字串",
    list: "陣列",
    dict: "key: value 結構",
}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"找不到設定檔：{path}")
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} 不是合法的 YAML：{exc}") from exc
    require(isinstance(data, dict), f"{path}：最上層應為 key: value 結構")
    return data


def get_field(data: Mapping[str, Any], key: str, kind: type, where: str) -> Any:
    """取出必要欄位並檢查型別。"""
    if key not in data:
        raise ConfigError(f"{where}：缺少必要欄位 `{key}`")
    value = data[key]
    if not _is_type(value, kind):
        raise ConfigError(f"{where} > {key}：應為{_TYPE_NAMES[kind]}，實際為 {value!r}")
    return float(value) if kind is float else value


def _is_type(value: Any, kind: type) -> bool:
    # bool 是 int 的子類別，要另外排除，避免 `retry: true` 被當成 1
    if isinstance(value, bool):
        return kind is bool
    if kind is float:
        return isinstance(value, (int, float))
    return isinstance(value, kind)


def check_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(str(k) for k in data.keys() - allowed)
    require(not unknown, f"{where}：未知的欄位 {', '.join(f'`{k}`' for k in unknown)}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)
