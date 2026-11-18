"""載入並驗證即時系統的設定：config/realtime.yaml、config/daytrade.yaml 與 .env 的 Shioaji 金鑰（REALTIME_PLAN.md 4.2）。

規則同 screener.config：缺少欄位、型別不符或出現未知欄位時一律報錯。這兩份設定只有即時系統的指令載入，
寫錯時不影響每日的 update、scan。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
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
class RealtimeConfig:
    compare_codes: tuple[str, ...]
    shioaji: ShioajiSettings
    probe: ProbeSettings
    record: RecordSettings
    bars: BarsSettings


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
    check_keys(data, {"compare_codes", "shioaji", "probe", "record", "bars"}, where)
    return RealtimeConfig(
        compare_codes=_parse_codes(data, "compare_codes", where),
        shioaji=_parse_shioaji(get_field(data, "shioaji", dict, where), f"{where} > shioaji"),
        probe=_parse_probe(get_field(data, "probe", dict, where), f"{where} > probe"),
        record=_parse_record(get_field(data, "record", dict, where), f"{where} > record"),
        bars=_parse_bars(get_field(data, "bars", dict, where), f"{where} > bars"),
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


def _parse_time(data: Mapping[str, Any], key: str, where: str) -> time:
    text = get_field(data, key, str, where)
    try:
        return time.fromisoformat(text)
    except ValueError:
        raise ConfigError(f"{where} > {key}：時間格式應為 HH:MM，實際為 {text!r}") from None
