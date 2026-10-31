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
class FinMindSettings:
    timeout_sec: float
    retry: int
    retry_backoff_sec: float
    request_interval_sec: float


@dataclass(frozen=True)
class MisSettings:
    batch_size: int
    request_interval_sec: float
    retry: int


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_dir: Path
    backfill_days: int
    finmind: FinMindSettings
    mis: MisSettings


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
    themes: tuple[str, ...]
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
    _check_keys(data, {"db_path", "log_dir", "backfill_days", "finmind", "mis"}, where)

    finmind_where = f"{where} > finmind"
    finmind_data = _get(data, "finmind", dict, where)
    _check_keys(finmind_data, {"timeout_sec", "retry", "retry_backoff_sec", "request_interval_sec"}, finmind_where)
    finmind = FinMindSettings(
        timeout_sec=_get(finmind_data, "timeout_sec", float, finmind_where),
        retry=_get(finmind_data, "retry", int, finmind_where),
        retry_backoff_sec=_get(finmind_data, "retry_backoff_sec", float, finmind_where),
        request_interval_sec=_get(finmind_data, "request_interval_sec", float, finmind_where),
    )
    _require(finmind.timeout_sec > 0, f"{finmind_where} > timeout_sec：必須大於 0")
    _require(finmind.retry >= 0, f"{finmind_where} > retry：不可為負數")
    _require(finmind.retry_backoff_sec >= 0, f"{finmind_where} > retry_backoff_sec：不可為負數")
    _require(finmind.request_interval_sec >= 0, f"{finmind_where} > request_interval_sec：不可為負數")

    mis_where = f"{where} > mis"
    mis_data = _get(data, "mis", dict, where)
    _check_keys(mis_data, {"batch_size", "request_interval_sec", "retry"}, mis_where)
    mis = MisSettings(
        batch_size=_get(mis_data, "batch_size", int, mis_where),
        request_interval_sec=_get(mis_data, "request_interval_sec", float, mis_where),
        retry=_get(mis_data, "retry", int, mis_where),
    )
    _require(mis.batch_size > 0, f"{mis_where} > batch_size：必須大於 0")
    _require(mis.request_interval_sec >= 0, f"{mis_where} > request_interval_sec：不可為負數")
    _require(mis.retry >= 0, f"{mis_where} > retry：不可為負數")

    settings = Settings(
        db_path=Path(_get(data, "db_path", str, where)),
        log_dir=Path(_get(data, "log_dir", str, where)),
        backfill_days=_get(data, "backfill_days", int, where),
        finmind=finmind,
        mis=mis,
    )
    _require(settings.backfill_days > 0, f"{where} > backfill_days：必須大於 0")
    return settings


# --- rules.yaml -------------------------------------------------------------

def _load_rules(path: Path) -> RulesConfig:
    data = _read_yaml(path)
    where = path.name
    _check_keys(data, {"trend", "filters", "entry_rules", "exit_rules"}, where)

    trend_where = f"{where} > trend"
    trend_data = _get(data, "trend", dict, where)
    _check_keys(
        trend_data,
        {"pivot_ma", "lookback_days", "bull_break_to_range", "bear_break_to_range"},
        trend_where,
    )
    trend = TrendConfig(
        pivot_ma=_get(trend_data, "pivot_ma", int, trend_where),
        lookback_days=_get(trend_data, "lookback_days", int, trend_where),
        bull_break_to_range=_get(trend_data, "bull_break_to_range", bool, trend_where),
        bear_break_to_range=_get(trend_data, "bear_break_to_range", bool, trend_where),
    )
    _require(trend.pivot_ma > 0, f"{trend_where} > pivot_ma：必須大於 0")
    _require(trend.lookback_days > 0, f"{trend_where} > lookback_days：必須大於 0")

    return RulesConfig(
        trend=trend,
        filters=_parse_rule_list(data, "filters", where),
        entry_rules=_parse_rule_list(data, "entry_rules", where),
        exit_rules=_parse_rule_list(data, "exit_rules", where),
    )


def _parse_rule_list(data: Mapping[str, Any], key: str, where: str) -> tuple[RuleConfig, ...]:
    rules: list[RuleConfig] = []
    seen: set[str] = set()
    for i, item in enumerate(_get(data, key, list, where)):
        item_where = f"{where} > {key}[{i}]"
        _require(isinstance(item, dict), f"{item_where}：應為 key: value 結構")
        _check_keys(item, {"id", "enabled", "params"}, item_where)
        rule = RuleConfig(
            id=_get(item, "id", str, item_where),
            enabled=_get(item, "enabled", bool, item_where),
            params=_get(item, "params", dict, item_where),
        )
        _require(rule.id not in seen, f"{item_where}：規則 id 重複：{rule.id}")
        seen.add(rule.id)
        rules.append(rule)
    return tuple(rules)


# --- watchlist.yaml ---------------------------------------------------------

_STOCK_KEYS = {"code", "name", "market", "industry", "themes"}


def _load_watchlist(path: Path) -> Watchlist:
    data = _read_yaml(path)
    where = path.name
    _check_keys(data, {"industries", "themes", "stocks"}, where)

    industries: dict[str, tuple[str, ...]] = {}
    for group, subcategories in _get(data, "industries", dict, where).items():
        group_where = f"{where} > industries > {group}"
        _require(isinstance(group, str), f"{group_where}：族群名稱應為字串")
        _require(
            isinstance(subcategories, list) and all(isinstance(s, str) for s in subcategories),
            f"{group_where}：子分類應為字串陣列；沒有子分類請寫 []",
        )
        _require(len(set(subcategories)) == len(subcategories), f"{group_where}：子分類重複")
        industries[group] = tuple(subcategories)

    themes = _get(data, "themes", list, where)
    _require(all(isinstance(t, str) for t in themes), f"{where} > themes：應為字串陣列")
    _require(len(set(themes)) == len(themes), f"{where} > themes：題材重複")

    # 逐檔驗證並一次列出所有錯誤，方便一次修正
    stocks: list[StockInfo] = []
    errors: list[str] = []
    seen_codes: set[str] = set()
    for i, item in enumerate(_get(data, "stocks", list, where)):
        try:
            stock = _parse_stock(item, industries, themes, f"{where} > stocks[{i}]")
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
    return Watchlist(industries=industries, themes=tuple(themes), stocks=tuple(stocks))


def _parse_stock(
    item: Any,
    industries: Mapping[str, tuple[str, ...]],
    themes: list[str],
    where: str,
) -> StockInfo:
    _require(isinstance(item, dict), f"{where}：應為 key: value 結構")
    _require(
        not isinstance(item.get("code"), int),
        f"{where} > code：股票代號請加引號，例如 \"{item.get('code')}\"",
    )
    code = _get(item, "code", str, where)
    where = f"{where}（{code}）"
    _check_keys(item, _STOCK_KEYS, where)

    market_value = _get(item, "market", str, where)
    try:
        market = Market(market_value)
    except ValueError:
        raise ConfigError(f"{where} > market：應為 tse（上市）或 otc（上櫃），實際為 {market_value!r}") from None

    industry = _get(item, "industry", str, where)
    group, _, subcategory = industry.partition("/")
    _require(group in industries, f"{where} > industry：未知的族群「{group}」，請先在 industries 定義")
    if industries[group]:
        _require(bool(subcategory), f"{where} > industry：族群「{group}」有子分類，必須寫到子分類，例如「{group}/{industries[group][0]}」")
        _require(subcategory in industries[group], f"{where} > industry：族群「{group}」沒有子分類「{subcategory}」")
    else:
        _require(not subcategory, f"{where} > industry：族群「{group}」未列子分類，只能寫「{group}」")

    stock_themes = item.get("themes", [])
    _require(
        isinstance(stock_themes, list) and all(isinstance(t, str) for t in stock_themes),
        f"{where} > themes：應為字串陣列",
    )
    for theme in stock_themes:
        _require(theme in themes, f"{where} > themes：未知的題材「{theme}」，請先在 themes 定義")

    return StockInfo(
        code=code,
        name=_get(item, "name", str, where),
        market=market,
        group=group,
        subcategory=subcategory or None,
        themes=tuple(stock_themes),
    )


# --- 共用工具 ---------------------------------------------------------------

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
    _require(isinstance(data, dict), f"{path}：最上層應為 key: value 結構")
    return data


def _get(data: Mapping[str, Any], key: str, kind: type, where: str) -> Any:
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


def _check_keys(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(str(k) for k in data.keys() - allowed)
    _require(not unknown, f"{where}：未知的欄位 {', '.join(f'`{k}`' for k in unknown)}")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)
