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
class FactorSnapshotSettings:
    """研究端每週產生的營收年增快照，scan 唯讀（D57）。"""

    path: Path
    max_age_days: int               # 產生日到今天超過這麼多天就不顯示，並在訊息底部提醒


@dataclass(frozen=True)
class Settings:
    db_path: Path
    log_dir: Path
    snapshot_dir: Path              # 盤中掃描推播後保存的 MIS 原始資料（D46）
    backfill_days: int
    finmind: FinMindSettings
    mis: MisSettings
    discord: DiscordSettings
    factor_snapshot: FactorSnapshotSettings


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
    entry_rules: tuple[RuleConfig, ...] = ()


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


TRADE_PICKS = ("nearest_ma", "volume_ratio")      # 交易回測的挑選方式；隨機一定會跑，作為基準


@dataclass(frozen=True)
class TradeConfig:
    """交易回測（RESEARCH_PLAN.md 4.2）。"""

    initial_capital: float          # 初始資金（元）
    max_positions: tuple[int, ...]  # 同時最多幾檔，各跑一次
    picks: tuple[str, ...]          # 訊號超過空位時的挑選方式（TRADE_PICKS）
    factor_picks: tuple[str, ...]   # 因子值高的優先（RESEARCH_PLAN.md 4.7）：research.yaml 的因子 id，執行時檢查
    pick_ma: int                    # nearest_ma：距離這條均線最近的優先
    random_runs: int                # 隨機挑選跑幾次
    random_seed: int
    min_commission: float           # 每筆最低手續費（元）
    output_dir: Path


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
    trade: TradeConfig


@dataclass(frozen=True)
class SignalReportConfig:
    """訊號成績單（RESEARCH_PLAN.md 4.1）。"""

    horizons: tuple[int, ...]       # 「累積」一段的持有交易日數，由小到大
    top_n: int                      # 本週最好、最差各列出幾個
    output_dir: Path                # 明細 CSV 的輸出目錄
    backtest_summary: Path          # M6 回測的 summary.csv；不存在時不顯示「M6 驗證段」


@dataclass(frozen=True)
class UniverseConfig:
    """全市場的股票池（RESEARCH_PLAN.md 4.3、R7）。"""

    size: int                       # 每月第一個交易日，取平均成交金額前幾名
    lookback_days: int              # 平均成交金額的交易日數（不含當天）


@dataclass(frozen=True)
class MarketConfig:
    """全市場資料（RESEARCH_PLAN.md 4.3）：market-update、market-check 與 --universe market。"""

    db_path: Path                   # market.db，每日系統不讀取（PROJECT_PLAN.md 10.4）
    raw_dir: Path                   # 證交所、櫃買中心的原始回應（壓縮檔）
    start: date                     # 回補的起點（R8）
    signal_start: date              # --universe market 的訊號起點
    compare_start: date             # 與觀察清單並列的起點
    universe: UniverseConfig
    output_dir: Path                # backtest、trade-backtest --universe market 的輸出
    check_dir: Path                 # market-check 的明細
    exchange: RequestSettings       # 證交所、櫃買中心的請求，兩邊各自計算間隔
    mops: RequestSettings           # 公開資訊觀測站的請求（月營收，RESEARCH_PLAN.md 4.4）
    revenue_available_day: int      # M 月的營收在 M+1 月這一天之後的第一個交易日才能用（R26）
    revenue_settle_days: int        # 上一個日子再過幾天視為公布完成，之前抓到的下次再抓


@dataclass(frozen=True)
class FactorDefinition:
    """research.yaml 的一個因子：id 為報告上的名稱，kind 為程式註冊的計算方式，params 為它的參數。"""

    id: str
    kind: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class FactorsConfig:
    """單因子研究（RESEARCH_PLAN.md 4.4、4.5）。"""

    output_dir: Path
    horizons: tuple[int, ...]       # 持有交易日數：完整列出 IC、分位數、換手
    decay_horizons: tuple[int, ...]  # 只列 IC 的衰減
    summary_horizon: int            # 終端摘要與單一天數圖表使用的持有天數
    quantiles: int                  # 分成幾組
    min_stocks: int                 # 當天有因子值的股票少於此數時不計算
    min_industry_stocks: int        # 當天同產業的股票少於此數時併到「其他」再中性化
    definitions: tuple[FactorDefinition, ...]


TOP_FACTOR = "top_factor"           # 因子值最高的 N 檔
TOP_VALUE = "top_value"             # 最高組內平均成交金額最大的 N 檔
PORTFOLIO_PICKS = (TOP_FACTOR, TOP_VALUE)


@dataclass(frozen=True)
class PaperPortfolio:
    """模擬組合追蹤的一組（RESEARCH_PLAN.md 4.6、R36）。"""

    factor: str
    top_n: int
    pick: str


@dataclass(frozen=True)
class PortfolioConfig:
    """組合回測與模擬組合（RESEARCH_PLAN.md 4.6）。"""

    initial_capital: float          # 初始資金（元）；觀察段、驗證段與模擬組合各自從這個金額開始
    factors: tuple[str, ...]        # 候選因子（R30），必須是 factors.definitions 的 id
    top_n: tuple[int, ...]          # 持股檔數（R32），各跑一次
    picks: tuple[str, ...]          # 挑法（PORTFOLIO_PICKS）
    liquidity_factor: str           # top_value 排序用的因子
    random_runs: int                # 最高組隨機 N 檔跑幾次
    random_seed: int
    output_dir: Path
    paper_db_path: Path             # 模擬組合的狀態，每日系統不讀取（PROJECT_PLAN.md 10.4）
    track: PaperPortfolio | None    # 使用者看完回測後選定；None 時 portfolio-track 無法執行


@dataclass(frozen=True)
class ResearchConfig:
    """research.yaml：只有研究指令會載入（PROJECT_PLAN.md 10.4）。"""

    events: EventsSettings
    isin: IsinSettings
    signal_report: SignalReportConfig
    market: MarketConfig
    factors: FactorsConfig
    portfolio: PortfolioConfig


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
    進場規則（5.5）只影響推播標示，出場規則（5.4）只用於交易回測，兩者都不影響訊號，不列入雜湊（D54、D56）。
    """
    content = asdict(rules)
    content["entry_rules"] = content.pop("trigger_rules")      # 觸發規則覆蓋真正的進場規則，後者不列入雜湊
    content["exit_rules"] = []                                  # 加入出場規則之前的指紋是以空陣列計算
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


# --- settings.yaml ----------------------------------------------------------

def _load_settings(path: Path) -> Settings:
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"db_path", "log_dir", "snapshot_dir", "backfill_days", "finmind", "mis", "discord",
                      "factor_snapshot"}, where)

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

    snapshot_where = f"{where} > factor_snapshot"
    snapshot_data = get_field(data, "factor_snapshot", dict, where)
    check_keys(snapshot_data, {"path", "max_age_days"}, snapshot_where)
    factor_snapshot = FactorSnapshotSettings(path=Path(get_field(snapshot_data, "path", str, snapshot_where)),
                                             max_age_days=get_field(snapshot_data, "max_age_days", int, snapshot_where))
    require(factor_snapshot.max_age_days >= 0, f"{snapshot_where} > max_age_days：不可為負數")

    settings = Settings(
        db_path=Path(get_field(data, "db_path", str, where)),
        log_dir=Path(get_field(data, "log_dir", str, where)),
        snapshot_dir=Path(get_field(data, "snapshot_dir", str, where)),
        backfill_days=get_field(data, "backfill_days", int, where),
        finmind=finmind,
        mis=mis,
        discord=discord,
        factor_snapshot=factor_snapshot,
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
    check_keys(data, {"trend", "filters", "trigger_rules", "entry_rules", "exit_rules"}, where)

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
        entry_rules=_parse_rule_list(data, "entry_rules", where),
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
    """只有 backtest、trade-backtest 指令會載入，寫錯時不影響每日的掃描與更新。"""
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"signal_start", "validation_start", "horizons", "summary_horizon", "dedupe_days",
                      "gap_flag_pct", "cost", "output_dir", "trade"}, where)

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
        trade=_parse_trade(get_field(data, "trade", dict, where), f"{where} > trade"),
    )
    require(config.validation_start > config.signal_start, f"{where} > validation_start：應晚於 signal_start")
    require(config.summary_horizon in config.horizons, f"{where} > summary_horizon：應為 horizons 其中之一")
    require(config.dedupe_days >= 0, f"{where} > dedupe_days：不可為負數")
    require(config.gap_flag_pct > 0, f"{where} > gap_flag_pct：必須大於 0")
    return config


def _parse_trade(data: Mapping[str, Any], where: str) -> TradeConfig:
    check_keys(data, {"initial_capital", "max_positions", "picks", "factor_picks", "pick_ma", "random_runs",
                      "random_seed", "min_commission", "output_dir"}, where)
    max_positions = get_field(data, "max_positions", list, where)
    require(bool(max_positions) and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in max_positions),
            f"{where} > max_positions：應為正整數陣列")
    require(max_positions == sorted(set(max_positions)), f"{where} > max_positions：應由小到大且不重複")
    picks = get_field(data, "picks", list, where)
    for pick in picks:
        require(pick in TRADE_PICKS, f"{where} > picks：應為 {'、'.join(TRADE_PICKS)} 其中之一，實際為 {pick!r}")
    require(len(picks) == len(set(picks)), f"{where} > picks：不可重複")
    factor_picks = get_field(data, "factor_picks", list, where)
    require(all(isinstance(p, str) and p for p in factor_picks), f"{where} > factor_picks：應為因子 id 的陣列")
    require(len(factor_picks) == len(set(factor_picks)), f"{where} > factor_picks：不可重複")
    for pick in factor_picks:
        require(pick not in (*TRADE_PICKS, "random"), f"{where} > factor_picks：{pick} 是 picks 的名稱，不是因子 id")
    trade = TradeConfig(
        initial_capital=get_field(data, "initial_capital", float, where),
        max_positions=tuple(max_positions),
        picks=tuple(picks),
        factor_picks=tuple(factor_picks),
        pick_ma=get_field(data, "pick_ma", int, where),
        random_runs=get_field(data, "random_runs", int, where),
        random_seed=get_field(data, "random_seed", int, where),
        min_commission=get_field(data, "min_commission", float, where),
        output_dir=Path(get_field(data, "output_dir", str, where)),
    )
    require(trade.initial_capital > 0, f"{where} > initial_capital：必須大於 0")
    require(trade.pick_ma > 0, f"{where} > pick_ma：必須大於 0")
    require(trade.random_runs >= 0, f"{where} > random_runs：不可為負數")
    require(trade.min_commission >= 0, f"{where} > min_commission：不可為負數")
    return trade


def _parse_horizons(data: Mapping[str, Any], where: str) -> list[int]:
    """持有交易日數：由小到大、不重複的正整數陣列。"""
    horizons = get_field(data, "horizons", list, where)
    require(bool(horizons) and all(isinstance(h, int) and not isinstance(h, bool) and h > 0 for h in horizons),
            f"{where} > horizons：應為正整數陣列")
    require(horizons == sorted(set(horizons)), f"{where} > horizons：應由小到大且不重複")
    return horizons


# --- research.yaml ----------------------------------------------------------

def load_research_config(path: Path) -> ResearchConfig:
    """只有研究指令會載入，寫錯時不影響每日的掃描與更新（PROJECT_PLAN.md 10.4）。"""
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"events", "isin", "signal_report", "market", "factors", "portfolio"}, where)

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
    market = _parse_market(get_field(data, "market", dict, where), f"{where} > market")
    factors = _parse_factors(get_field(data, "factors", dict, where), f"{where} > factors")
    portfolio = _parse_portfolio(get_field(data, "portfolio", dict, where), f"{where} > portfolio", factors)
    return ResearchConfig(events=events, isin=isin, signal_report=signal_report, market=market, factors=factors,
                          portfolio=portfolio)


def _parse_portfolio(data: Mapping[str, Any], where: str, factors: FactorsConfig) -> PortfolioConfig:
    check_keys(data, {"initial_capital", "factors", "top_n", "picks", "liquidity_factor", "random_runs", "random_seed",
                      "output_dir", "paper_db_path", "track"}, where)
    defined = {definition.id for definition in factors.definitions}
    candidates = get_field(data, "factors", list, where)
    require(bool(candidates), f"{where} > factors：至少要有一個因子")
    require(len(set(candidates)) == len(candidates), f"{where} > factors：不可重複")
    for factor in candidates:
        require(factor in defined, f"{where} > factors：{factor!r} 不在 factors.definitions")
    top_n = get_field(data, "top_n", list, where)
    require(bool(top_n) and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in top_n),
            f"{where} > top_n：應為正整數陣列")
    require(top_n == sorted(set(top_n)), f"{where} > top_n：應由小到大且不重複")
    picks = get_field(data, "picks", list, where)
    require(bool(picks) and all(pick in PORTFOLIO_PICKS for pick in picks) and len(set(picks)) == len(picks),
            f"{where} > picks：應為 {'、'.join(PORTFOLIO_PICKS)} 其中之一且不重複")
    liquidity = get_field(data, "liquidity_factor", str, where)
    require(liquidity in defined, f"{where} > liquidity_factor：{liquidity!r} 不在 factors.definitions")
    portfolio = PortfolioConfig(
        initial_capital=get_field(data, "initial_capital", float, where),
        factors=tuple(candidates),
        top_n=tuple(top_n),
        picks=tuple(picks),
        liquidity_factor=liquidity,
        random_runs=get_field(data, "random_runs", int, where),
        random_seed=get_field(data, "random_seed", int, where),
        output_dir=Path(get_field(data, "output_dir", str, where)),
        paper_db_path=Path(get_field(data, "paper_db_path", str, where)),
        track=_parse_track(data, f"{where} > track", candidates, top_n, picks),
    )
    require(portfolio.initial_capital > 0, f"{where} > initial_capital：必須大於 0")
    require(portfolio.random_runs >= 0, f"{where} > random_runs：不可為負數")
    return portfolio


def _parse_track(data: Mapping[str, Any], where: str, factors: list[str], top_n: list[int],
                 picks: list[str]) -> PaperPortfolio | None:
    """追蹤的一組必須是回測比較過的組合（D32）；尚未選定時寫 null。"""
    if "track" not in data:
        raise ConfigError(f"{where}：缺少必要欄位；尚未選定時寫 null")
    if data["track"] is None:
        return None
    require(isinstance(data["track"], dict), f"{where}：應為 key: value 結構或 null")
    check_keys(data["track"], {"factor", "top_n", "pick"}, where)
    track = PaperPortfolio(factor=get_field(data["track"], "factor", str, where),
                           top_n=get_field(data["track"], "top_n", int, where),
                           pick=get_field(data["track"], "pick", str, where))
    require(track.factor in factors, f"{where} > factor：應為 portfolio.factors 其中之一")
    require(track.top_n in top_n, f"{where} > top_n：應為 portfolio.top_n 其中之一")
    require(track.pick in picks, f"{where} > pick：應為 portfolio.picks 其中之一")
    return track


def _parse_factors(data: Mapping[str, Any], where: str) -> FactorsConfig:
    check_keys(data, {"output_dir", "horizons", "decay_horizons", "summary_horizon", "quantiles", "min_stocks",
                      "min_industry_stocks", "definitions"}, where)
    horizons = _parse_horizons(data, where)
    decay = _parse_horizons({"horizons": get_field(data, "decay_horizons", list, where)}, f"{where} > decay_horizons")
    summary_horizon = get_field(data, "summary_horizon", int, where)
    require(summary_horizon in horizons, f"{where} > summary_horizon：必須在 horizons 之中")
    quantiles = get_field(data, "quantiles", int, where)
    require(quantiles >= 2, f"{where} > quantiles：至少 2 組")
    min_stocks = get_field(data, "min_stocks", int, where)
    require(min_stocks >= quantiles, f"{where} > min_stocks：不可少於 quantiles")
    min_industry = get_field(data, "min_industry_stocks", int, where)
    require(min_industry >= 1, f"{where} > min_industry_stocks：必須大於 0")
    definitions = []
    for i, item in enumerate(get_field(data, "definitions", list, where), start=1):
        item_where = f"{where} > definitions 第 {i} 項"
        require(isinstance(item, dict), f"{item_where}：應為物件")
        check_keys(item, {"id", "kind", "params"}, item_where)
        definitions.append(FactorDefinition(id=get_field(item, "id", str, item_where),
                                            kind=get_field(item, "kind", str, item_where),
                                            params=get_field(item, "params", dict, item_where)))
    require(bool(definitions), f"{where} > definitions：至少要有一個因子")
    ids = [d.id for d in definitions]
    require(len(set(ids)) == len(ids), f"{where} > definitions：id 重複")
    return FactorsConfig(output_dir=Path(get_field(data, "output_dir", str, where)), horizons=tuple(horizons),
                         decay_horizons=tuple(decay), summary_horizon=summary_horizon, quantiles=quantiles,
                         min_stocks=min_stocks, min_industry_stocks=min_industry, definitions=tuple(definitions))


def _parse_market(data: Mapping[str, Any], where: str) -> MarketConfig:
    check_keys(data, {"db_path", "raw_dir", "start", "signal_start", "compare_start", "universe", "output_dir",
                      "check_dir", "exchange", "mops", "revenue_available_day", "revenue_settle_days"}, where)
    universe_where = f"{where} > universe"
    universe_data = get_field(data, "universe", dict, where)
    check_keys(universe_data, {"size", "lookback_days"}, universe_where)
    universe = UniverseConfig(size=get_field(universe_data, "size", int, universe_where),
                              lookback_days=get_field(universe_data, "lookback_days", int, universe_where))
    require(universe.size > 0, f"{universe_where} > size：必須大於 0")
    require(universe.lookback_days > 0, f"{universe_where} > lookback_days：必須大於 0")

    exchange_where = f"{where} > exchange"
    exchange_data = get_field(data, "exchange", dict, where)
    check_keys(exchange_data, _REQUEST_KEYS, exchange_where)
    mops_where = f"{where} > mops"
    mops_data = get_field(data, "mops", dict, where)
    check_keys(mops_data, _REQUEST_KEYS, mops_where)
    market = MarketConfig(
        db_path=Path(get_field(data, "db_path", str, where)),
        raw_dir=Path(get_field(data, "raw_dir", str, where)),
        start=get_field(data, "start", date, where),
        signal_start=get_field(data, "signal_start", date, where),
        compare_start=get_field(data, "compare_start", date, where),
        universe=universe,
        output_dir=Path(get_field(data, "output_dir", str, where)),
        check_dir=Path(get_field(data, "check_dir", str, where)),
        exchange=RequestSettings(**_parse_request_settings(exchange_data, exchange_where)),
        mops=RequestSettings(**_parse_request_settings(mops_data, mops_where)),
        revenue_available_day=get_field(data, "revenue_available_day", int, where),
        revenue_settle_days=get_field(data, "revenue_settle_days", int, where),
    )
    require(market.signal_start >= market.start, f"{where} > signal_start：不可早於 start")
    require(1 <= market.revenue_available_day <= 28, f"{where} > revenue_available_day：應在 1～28 之間")
    require(market.revenue_settle_days >= 0, f"{where} > revenue_settle_days：不可為負數")
    require(market.compare_start >= market.signal_start, f"{where} > compare_start：不可早於 signal_start")
    return market


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
