"""載入並驗證即時系統的設定：config/realtime.yaml、config/daytrade.yaml 與 .env 的 Shioaji 金鑰（REALTIME_PLAN.md 4.2）。

規則同 screener.config：缺少欄位、型別不符或出現未知欄位時一律報錯。這兩份設定只有即時系統的指令載入，
寫錯時不影響每日的 update、scan。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from screener.config import ConfigError, check_keys, get_field, require

SHIOAJI_ENV_NAMES = ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY")


@dataclass(frozen=True)
class ShioajiSettings:
    simulation: bool
    subscribe_trade: bool
    receive_window_ms: int
    timeout_ms: int
    contracts_timeout_ms: int
    query_interval_sec: float       # 兩次查詢的最短間隔（官方限制每 10 秒 50 次）
    retry: int
    retry_backoff_sec: float
    log_path: Path


@dataclass(frozen=True)
class ProbeSettings:
    output_dir: Path
    snapshot_batches: tuple[int, ...]
    kbars_days: int
    subscribe_limit: int
    subscribe_interval_sec: float
    usage_settle_sec: int


@dataclass(frozen=True)
class RecordSettings:
    tick_dir: Path
    end_time: time
    max_subscriptions: int          # 每個連線
    connections: int
    flush_interval_sec: float
    usage_interval_sec: float


@dataclass(frozen=True)
class BarsSettings:
    output_dir: Path
    prior_lookback_days: int


@dataclass(frozen=True)
class CaseStock:
    """不在觀察清單的案例股（L20）：只回補 start～end 的交易日。"""

    code: str
    name: str
    start: date
    end: date


@dataclass(frozen=True)
class BackfillSettings:
    db_path: Path
    start: date                     # 回補的起點（L18）
    traffic_stop_mb: float          # 當天的流量用到這裡就停止
    max_query_days: int             # 每次查詢最多幾個日曆天（kbars 的限制）
    max_failures: int               # 連續幾次查詢失敗就停止
    bytes_per_stock_day: int        # --dry-run 的流量預估
    progress_every: int
    blocked_hours: tuple[time, time]    # 平日這段時間（含開始、不含結束）不執行：盤中 kbars 有次數限制
    market_transfers: tuple[str, ...]   # 轉市場的股票：上市櫃日之前也是上市櫃交易，從 start 起（L25）
    cases: tuple[CaseStock, ...]

    @property
    def traffic_stop_bytes(self) -> int:
        return int(self.traffic_stop_mb * 1024 * 1024)     # usage() 的上限 500 MB = 524,288,000 bytes（T1）


@dataclass(frozen=True)
class CheckSettings:
    output_dir: Path
    volume_ratio_min: float
    listed: int                     # 報告中每一類最多列出幾筆


@dataclass(frozen=True)
class RealtimeConfig:
    compare_codes: tuple[str, ...]
    shioaji: ShioajiSettings
    probe: ProbeSettings
    record: RecordSettings
    bars: BarsSettings
    backfill: BackfillSettings
    check: CheckSettings


@dataclass(frozen=True)
class Session:
    open: time                      # 第一根 K 棒的開始時間
    closing_call: time              # 收盤集合競價開始（之後到 close 只有試撮）
    close: time                     # 收盤成交的時間


@dataclass(frozen=True)
class MovingAverageSpec:
    id: str
    bar_minutes: int
    period: int


@dataclass(frozen=True)
class DaytradeConfig:
    session: Session
    moving_averages: tuple[MovingAverageSpec, ...]

    @property
    def bar_minutes(self) -> tuple[int, ...]:
        """要組的 K 棒：1 分 K 加上均線用到的分鐘數，由小到大。"""
        return tuple(sorted({1, *(spec.bar_minutes for spec in self.moving_averages)}))


@dataclass(frozen=True)
class ShioajiKeys:
    api_key: str
    secret_key: str

    def __repr__(self) -> str:                 # 不讓金鑰出現在 log 或錯誤訊息
        return "ShioajiKeys(***)"


# --- realtime.yaml ----------------------------------------------------------

def load_realtime_config(path: Path) -> RealtimeConfig:
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"compare_codes", "shioaji", "probe", "record", "bars", "backfill", "check"}, where)
    return RealtimeConfig(
        compare_codes=_parse_codes(data, "compare_codes", where),
        shioaji=_parse_shioaji(get_field(data, "shioaji", dict, where), f"{where} > shioaji"),
        probe=_parse_probe(get_field(data, "probe", dict, where), f"{where} > probe"),
        record=_parse_record(get_field(data, "record", dict, where), f"{where} > record"),
        bars=_parse_bars(get_field(data, "bars", dict, where), f"{where} > bars"),
        backfill=_parse_backfill(get_field(data, "backfill", dict, where), f"{where} > backfill"),
        check=_parse_check(get_field(data, "check", dict, where), f"{where} > check"),
    )


def _parse_shioaji(data: Mapping[str, Any], where: str) -> ShioajiSettings:
    check_keys(data, {"simulation", "subscribe_trade", "receive_window_ms", "timeout_ms", "contracts_timeout_ms",
                      "query_interval_sec", "retry", "retry_backoff_sec", "log_path"}, where)
    settings = ShioajiSettings(
        simulation=get_field(data, "simulation", bool, where),
        subscribe_trade=get_field(data, "subscribe_trade", bool, where),
        receive_window_ms=get_field(data, "receive_window_ms", int, where),
        timeout_ms=get_field(data, "timeout_ms", int, where),
        contracts_timeout_ms=get_field(data, "contracts_timeout_ms", int, where),
        query_interval_sec=get_field(data, "query_interval_sec", float, where),
        retry=get_field(data, "retry", int, where),
        retry_backoff_sec=get_field(data, "retry_backoff_sec", float, where),
        log_path=Path(get_field(data, "log_path", str, where)),
    )
    for name in ("receive_window_ms", "timeout_ms", "contracts_timeout_ms"):
        require(getattr(settings, name) > 0, f"{where} > {name}：必須大於 0")
    require(settings.query_interval_sec >= 0, f"{where} > query_interval_sec：不可為負數")
    require(settings.retry >= 0, f"{where} > retry：不可為負數")
    require(settings.retry_backoff_sec >= 0, f"{where} > retry_backoff_sec：不可為負數")
    return settings


def _parse_probe(data: Mapping[str, Any], where: str) -> ProbeSettings:
    check_keys(data, {"output_dir", "snapshot_batches", "kbars_days", "subscribe_limit", "subscribe_interval_sec",
                      "usage_settle_sec"}, where)
    batches = get_field(data, "snapshot_batches", list, where)
    require(bool(batches) and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in batches),
            f"{where} > snapshot_batches：應為正整數的清單")
    settings = ProbeSettings(
        output_dir=Path(get_field(data, "output_dir", str, where)),
        snapshot_batches=tuple(batches),
        kbars_days=get_field(data, "kbars_days", int, where),
        subscribe_limit=get_field(data, "subscribe_limit", int, where),
        subscribe_interval_sec=get_field(data, "subscribe_interval_sec", float, where),
        usage_settle_sec=get_field(data, "usage_settle_sec", int, where),
    )
    require(settings.kbars_days > 0, f"{where} > kbars_days：必須大於 0")
    require(settings.subscribe_limit > 0, f"{where} > subscribe_limit：必須大於 0")
    require(settings.subscribe_interval_sec >= 0, f"{where} > subscribe_interval_sec：不可為負數")
    require(settings.usage_settle_sec >= 0, f"{where} > usage_settle_sec：不可為負數")
    return settings


def _parse_record(data: Mapping[str, Any], where: str) -> RecordSettings:
    check_keys(data, {"tick_dir", "end_time", "max_subscriptions", "connections", "flush_interval_sec",
                      "usage_interval_sec"}, where)
    settings = RecordSettings(
        tick_dir=Path(get_field(data, "tick_dir", str, where)),
        end_time=_parse_time(data, "end_time", where),
        max_subscriptions=get_field(data, "max_subscriptions", int, where),
        connections=get_field(data, "connections", int, where),
        flush_interval_sec=get_field(data, "flush_interval_sec", float, where),
        usage_interval_sec=get_field(data, "usage_interval_sec", float, where),
    )
    require(settings.max_subscriptions > 0, f"{where} > max_subscriptions：必須大於 0")
    require(settings.connections > 0, f"{where} > connections：必須大於 0")
    require(settings.flush_interval_sec > 0, f"{where} > flush_interval_sec：必須大於 0")
    require(settings.usage_interval_sec > 0, f"{where} > usage_interval_sec：必須大於 0")
    return settings


def _parse_bars(data: Mapping[str, Any], where: str) -> BarsSettings:
    check_keys(data, {"output_dir", "prior_lookback_days"}, where)
    settings = BarsSettings(output_dir=Path(get_field(data, "output_dir", str, where)),
                            prior_lookback_days=get_field(data, "prior_lookback_days", int, where))
    require(0 < settings.prior_lookback_days < 30, f"{where} > prior_lookback_days：必須在 1～29 之間"
            "（kbars 每次查詢不超過 30 天）")
    return settings


_KBARS_MAX_DAYS = 30                # Shioaji 的 kbars 每次查詢不超過 30 天（第 3 節）
_USAGE_LIMIT_MB = 500               # 沒有以 API 成交時每天的流量上限（T1）


def _parse_backfill(data: Mapping[str, Any], where: str) -> BackfillSettings:
    check_keys(data, {"db_path", "start", "traffic_stop_mb", "max_query_days", "max_failures", "bytes_per_stock_day",
                      "progress_every", "blocked_hours", "market_transfers", "cases"}, where)
    hours = get_field(data, "blocked_hours", list, where)
    require(len(hours) == 2, f"{where} > blocked_hours：應為 [開始, 結束] 兩個時間")
    blocked = (_parse_time({"start": hours[0]}, "start", f"{where} > blocked_hours"),
               _parse_time({"end": hours[1]}, "end", f"{where} > blocked_hours"))
    require(blocked[0] < blocked[1], f"{where} > blocked_hours：開始必須早於結束")
    cases = []
    for index, item in enumerate(get_field(data, "cases", list, where)):
        item_where = f"{where} > cases[{index}]"
        require(isinstance(item, dict), f"{item_where}：應為 key: value 結構")
        check_keys(item, {"code", "name", "start", "end"}, item_where)
        case = CaseStock(code=get_field(item, "code", str, item_where), name=get_field(item, "name", str, item_where),
                         start=_parse_date(item, "start", item_where), end=_parse_date(item, "end", item_where))
        require(case.start <= case.end, f"{item_where}：start 不可晚於 end")
        cases.append(case)
    require(len({case.code for case in cases}) == len(cases), f"{where} > cases：代號重複")
    settings = BackfillSettings(
        db_path=Path(get_field(data, "db_path", str, where)),
        start=_parse_date(data, "start", where),
        traffic_stop_mb=get_field(data, "traffic_stop_mb", float, where),
        max_query_days=get_field(data, "max_query_days", int, where),
        max_failures=get_field(data, "max_failures", int, where),
        bytes_per_stock_day=get_field(data, "bytes_per_stock_day", int, where),
        progress_every=get_field(data, "progress_every", int, where),
        blocked_hours=blocked,
        market_transfers=_parse_codes(data, "market_transfers", where),
        cases=tuple(cases),
    )
    require(0 < settings.traffic_stop_mb < _USAGE_LIMIT_MB,
            f"{where} > traffic_stop_mb：必須大於 0、小於每天的上限 {_USAGE_LIMIT_MB} MB")
    require(0 < settings.max_query_days <= _KBARS_MAX_DAYS,
            f"{where} > max_query_days：必須在 1～{_KBARS_MAX_DAYS} 之間（kbars 每次查詢不超過 30 天）")
    require(settings.max_failures > 0, f"{where} > max_failures：必須大於 0")
    require(settings.bytes_per_stock_day > 0, f"{where} > bytes_per_stock_day：必須大於 0")
    require(settings.progress_every > 0, f"{where} > progress_every：必須大於 0")
    return settings


def _parse_check(data: Mapping[str, Any], where: str) -> CheckSettings:
    check_keys(data, {"output_dir", "volume_ratio_min", "listed"}, where)
    settings = CheckSettings(output_dir=Path(get_field(data, "output_dir", str, where)),
                             volume_ratio_min=get_field(data, "volume_ratio_min", float, where),
                             listed=get_field(data, "listed", int, where))
    require(0 < settings.volume_ratio_min <= 1, f"{where} > volume_ratio_min：必須大於 0、不超過 1")
    require(settings.listed > 0, f"{where} > listed：必須大於 0")
    return settings


# --- daytrade.yaml ----------------------------------------------------------

def load_daytrade_config(path: Path) -> DaytradeConfig:
    data = _read_yaml(path)
    where = path.name
    check_keys(data, {"session", "moving_averages"}, where)
    session_data = get_field(data, "session", dict, where)
    check_keys(session_data, {"open", "closing_call", "close"}, f"{where} > session")
    session = Session(open=_parse_time(session_data, "open", f"{where} > session"),
                      closing_call=_parse_time(session_data, "closing_call", f"{where} > session"),
                      close=_parse_time(session_data, "close", f"{where} > session"))
    require(session.open < session.closing_call < session.close,
            f"{where} > session：必須 open < closing_call < close")

    specs = []
    for index, item in enumerate(get_field(data, "moving_averages", list, where)):
        item_where = f"{where} > moving_averages[{index}]"
        require(isinstance(item, dict), f"{item_where}：應為 key: value 結構")
        check_keys(item, {"id", "bar_minutes", "period"}, item_where)
        spec = MovingAverageSpec(id=get_field(item, "id", str, item_where),
                                 bar_minutes=get_field(item, "bar_minutes", int, item_where),
                                 period=get_field(item, "period", int, item_where))
        require(spec.bar_minutes > 0, f"{item_where} > bar_minutes：必須大於 0")
        require(spec.period > 0, f"{item_where} > period：必須大於 0")
        specs.append(spec)
    ids = [spec.id for spec in specs]
    require(len(ids) == len(set(ids)), f"{where} > moving_averages：id 重複")
    return DaytradeConfig(session=session, moving_averages=tuple(specs))


# --- .env -------------------------------------------------------------------

def load_shioaji_keys(env_file: Path) -> ShioajiKeys:
    """環境變數優先於 .env；空字串視為未設定。金鑰的值不會出現在錯誤訊息中。"""
    file_values = dotenv_values(env_file) if env_file.is_file() else {}
    values = {name: os.environ.get(name) or file_values.get(name) or None for name in SHIOAJI_ENV_NAMES}
    missing = [name for name, value in values.items() if not value]
    require(not missing, f"未設定 {'、'.join(missing)}（.env）")
    return ShioajiKeys(api_key=values["SHIOAJI_API_KEY"], secret_key=values["SHIOAJI_SECRET_KEY"])


# --- 共用 -------------------------------------------------------------------

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


def _parse_codes(data: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    codes = get_field(data, key, list, where)
    require(all(isinstance(code, str) and code for code in codes), f"{where} > {key}：代號應為字串（加上引號）")
    require(len(codes) == len(set(codes)), f"{where} > {key}：代號重複")
    return tuple(codes)


def _parse_date(data: Mapping[str, Any], key: str, where: str) -> date:
    """YAML 的 2020-06-01 讀成 date；datetime 不接受。"""
    value = get_field(data, key, date, where)
    require(not isinstance(value, datetime), f"{where} > {key}：應為日期（YYYY-MM-DD），實際為 {value!r}")
    return value


def _parse_time(data: Mapping[str, Any], key: str, where: str) -> time:
    text = get_field(data, key, str, where)
    try:
        return time.fromisoformat(text)
    except ValueError:
        raise ConfigError(f"{where} > {key}：時間格式應為 HH:MM，實際為 {text!r}") from None
