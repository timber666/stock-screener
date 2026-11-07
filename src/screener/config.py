"""載入並驗證 config/ 下的設定檔與 .env（PROJECT_PLAN.md 第 9 節）。

所有可調整的數字都來自設定檔，程式中不提供預設值：缺少欄位、型別不符或出現
未知欄位時一律報錯，避免打錯字造成設定被悄悄忽略。

每個指令都會載入 settings.yaml、watchlist.yaml、rules.yaml；研究專用的 backtest.yaml、research.yaml
只有研究指令會載入，寫錯時不影響每日的 update、scan（PROJECT_PLAN.md 10.4）。
"""

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from screener.models import Market, StockInfo


class ConfigError(Exception):
    """設定檔內容不正確。"""


@dataclass(frozen=True)
class RequestSettings:
    """HTTP 請求的逾時、重試與間隔，FinMind、MIS、Discord、證交所 ISIN 共用。"""

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
class IsinSettings(RequestSettings):
    pass


@dataclass(frozen=True)
class EventsSettings:
    """events 指令（PROJECT_PLAN.md 14.2）。"""

    refetch_days: int               # 已查過的股票，從上次查詢的迄日往前重疊幾天再查
    reference_tolerance_pct: float  # 交叉驗證：以股利金額計算的參考價與官方差異超過此值（%）時列出
    dividend_lookahead_days: int    # 股利資料的查詢迄日往後放寬的天數（以權利分派基準日篩選，14.2）


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_dir: Path
    snapshot_dir: Path              # 盤中掃描推播後保存的 MIS 原始資料（D46）
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
    trigger_rules: tuple[RuleConfig, ...]
    exit_rules: tuple[RuleConfig, ...]


@dataclass(frozen=True)
class Watchlist:
    industries: dict[str, tuple[str, ...]]     # 族群 → 子分類（上游到下游）
    stocks: tuple[StockInfo, ...]


@dataclass(frozen=True)
class CostConfig:
    commission_pct: float           # 手續費率（%），買賣各收一次
    commission_discount: float      # 手續費折扣，例 0.28 = 2.8 折；1 = 不打折
    tax_pct: float                  # 證交稅（%），賣出時收

    @property
    def round_trip_pct(self) -> float:
        """一買一賣的總成本（%）。"""
        return self.commission_pct * self.commission_discount * 2 + self.tax_pct


@dataclass(frozen=True)
class BacktestConfig:
    """訊號回測（PROJECT_PLAN.md 14.1）。"""

    signal_start: date              # 訊號從這天起算
    validation_start: date          # 驗證段的第一天；之前為觀察段
    horizons: tuple[int, ...]       # 持有交易日數，由小到大
    summary_horizon: int            # 終端摘要與單一天數圖表使用的持有天數
    dedupe_days: int                # 同檔同規則距離上次計入不到幾個交易日的訊號不計（去重）
    gap_flag_pct: float             # 計算區間內單次價格調整幅度達此值（%）的事件標記為跳空事件
    cost: CostConfig
    output_dir: Path


@dataclass(frozen=True)
class SignalReportConfig:
    """訊號成績單（RESEARCH_PLAN.md 4.1）。"""

    horizons: tuple[int, ...]       # 「累積」一段的持有交易日數，由小到大
    top_n: int                      # 本週最好、最差各列出幾個
    output_dir: Path                # 明細 CSV 的輸出目錄
    backtest_summary: Path          # M6 回測的 summary.csv；不存在時不顯示「M6 驗證段」


@dataclass(frozen=True)
class ResearchConfig:
    """research.yaml：只有研究指令（events、signal-report）會載入（PROJECT_PLAN.md 10.4）。"""

    events: EventsSettings
    isin: IsinSettings
    signal_report: SignalReportConfig


@dataclass(frozen=True)
class AppConfig:
    settings: Settings
    watchlist: Watchlist
    rules: RulesConfig


@dataclass(frozen=True)
class Secrets:
    finmind_token: str | None
    discord_webhook_url: str | None
    discord_research_webhook_url: str | None    # 研究的定期報告（D43）


def load_config(config_dir: Path) -> AppConfig:
    return AppConfig(
        settings=_load_settings(config_dir / "settings.yaml"),
        watchlist=_load_watchlist(config_dir / "watchlist.yaml"),
        rules=load_rules(config_dir / "rules.yaml"),
    )


def load_secrets(env_file: Path) -> Secrets:
    """讀取機密資訊。環境變數優先於 .env；空字串視為未設定。"""
    file_values = dotenv_values(env_file) if env_file.is_file() else {}

    def lookup(name: str) -> str | None:
        return os.environ.get(name) or file_values.get(name) or None

    return Secrets(
        finmind_token=lookup("FINMIND_TOKEN"),
        discord_webhook_url=lookup("DISCORD_WEBHOOK_URL"),
        discord_research_webhook_url=lookup("DISCORD_RESEARCH_WEBHOOK_URL"),
    )


def rules_fingerprint(rules: RulesConfig) -> str:
    """規則檔的指紋：解析後內容的雜湊（sha256 前 12 碼）。只改註解或排版不會改變指紋。

    訊號紀錄保存推播時的指紋，成績單以它判斷收盤重算時的規則是否與推播時相同（RESEARCH_PLAN.md 4.1）。
    雜湊的內容沿用改名前的欄位名稱 entry_rules（D49）：名稱改動不算規則變更，已記錄的推播不會被誤判。
    """
    content = asdict(rules)
    content["entry_rules"] = content.pop("trigger_rules")
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


# --- settings.yaml ----------------------------------------------------------

def _load_settings(path: Path) -> Settings:
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"db_path", "log_dir", "snapshot_dir", "backfill_days", "finmind", "mis", "discord"}, where)

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
        snapshot_dir=Path(get_field(data, "snapshot_dir", str, where)),
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

def load_rules(path: Path) -> RulesConfig:
    """rules.yaml 格式的規則檔；回測可另外指定一份（D35）。"""
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"trend", "filters", "trigger_rules", "exit_rules"}, where)

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
        trigger_rules=_parse_rule_list(data, "trigger_rules", where),
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


# --- backtest.yaml ----------------------------------------------------------

def load_backtest_config(path: Path) -> BacktestConfig:
    """只有 backtest 指令會載入，寫錯時不影響每日的掃描與更新。"""
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"signal_start", "validation_start", "horizons", "summary_horizon", "dedupe_days",
                      "gap_flag_pct", "cost", "output_dir"}, where)

    horizons = _parse_horizons(data, where)

    cost_where = f"{where} > cost"
    cost_data = get_field(data, "cost", dict, where)
    check_keys(cost_data, {"commission_pct", "commission_discount", "tax_pct"}, cost_where)
    cost = CostConfig(
        commission_pct=get_field(cost_data, "commission_pct", float, cost_where),
        commission_discount=get_field(cost_data, "commission_discount", float, cost_where),
        tax_pct=get_field(cost_data, "tax_pct", float, cost_where),
    )
    require(cost.commission_pct >= 0, f"{cost_where} > commission_pct：不可為負數")
    require(0 < cost.commission_discount <= 1, f"{cost_where} > commission_discount：應大於 0 且不超過 1")
    require(cost.tax_pct >= 0, f"{cost_where} > tax_pct：不可為負數")

    config = BacktestConfig(
        signal_start=get_field(data, "signal_start", date, where),
        validation_start=get_field(data, "validation_start", date, where),
        horizons=tuple(horizons),
        summary_horizon=get_field(data, "summary_horizon", int, where),
        dedupe_days=get_field(data, "dedupe_days", int, where),
        gap_flag_pct=get_field(data, "gap_flag_pct", float, where),
        cost=cost,
        output_dir=Path(get_field(data, "output_dir", str, where)),
    )
    require(config.validation_start > config.signal_start, f"{where} > validation_start：應晚於 signal_start")
    require(config.summary_horizon in config.horizons, f"{where} > summary_horizon：應為 horizons 其中之一")
    require(config.dedupe_days >= 0, f"{where} > dedupe_days：不可為負數")
    require(config.gap_flag_pct > 0, f"{where} > gap_flag_pct：必須大於 0")
    return config


def _parse_horizons(data: Mapping[str, Any], where: str) -> list[int]:
    """持有交易日數：由小到大、不重複的正整數陣列。"""
    horizons = get_field(data, "horizons", list, where)
    require(bool(horizons) and all(isinstance(h, int) and not isinstance(h, bool) and h > 0 for h in horizons),
            f"{where} > horizons：應為正整數陣列")
    require(horizons == sorted(set(horizons)), f"{where} > horizons：應由小到大且不重複")
    return horizons


# --- research.yaml ----------------------------------------------------------

def load_research_config(path: Path) -> ResearchConfig:
    """只有研究指令（events、signal-report）會載入，寫錯時不影響每日的掃描與更新（PROJECT_PLAN.md 10.4）。"""
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"events", "isin", "signal_report"}, where)

    events_where = f"{where} > events"
    events_data = get_field(data, "events", dict, where)
    check_keys(events_data, {"refetch_days", "reference_tolerance_pct", "dividend_lookahead_days"}, events_where)
    events = EventsSettings(
        refetch_days=get_field(events_data, "refetch_days", int, events_where),
        reference_tolerance_pct=get_field(events_data, "reference_tolerance_pct", float, events_where),
        dividend_lookahead_days=get_field(events_data, "dividend_lookahead_days", int, events_where),
    )
    require(events.refetch_days >= 0, f"{events_where} > refetch_days：不可為負數")
    require(events.reference_tolerance_pct > 0, f"{events_where} > reference_tolerance_pct：必須大於 0")
    require(events.dividend_lookahead_days >= 0, f"{events_where} > dividend_lookahead_days：不可為負數")

    isin_where = f"{where} > isin"
    isin_data = get_field(data, "isin", dict, where)
    check_keys(isin_data, _REQUEST_KEYS, isin_where)
    isin = IsinSettings(**_parse_request_settings(isin_data, isin_where))

    report_where = f"{where} > signal_report"
    report_data = get_field(data, "signal_report", dict, where)
    check_keys(report_data, {"horizons", "top_n", "output_dir", "backtest_summary"}, report_where)
    signal_report = SignalReportConfig(
        horizons=tuple(_parse_horizons(report_data, report_where)),
        top_n=get_field(report_data, "top_n", int, report_where),
        output_dir=Path(get_field(report_data, "output_dir", str, report_where)),
        backtest_summary=Path(get_field(report_data, "backtest_summary", str, report_where)),
    )
    require(signal_report.top_n >= 0, f"{report_where} > top_n：不可為負數")
    return ResearchConfig(events=events, isin=isin, signal_report=signal_report)


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
    date: "日期（YYYY-MM-DD）",
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
