"""盤中錄製：python -m screener rt-record（REALTIME_PLAN.md 7.1 第 4 點，L15、L16）。

開盤前啟動，訂閱觀察清單的成交（對照股另訂五檔），把收到的資料原樣寫入當天的錄製檔
（recorder.py），到 realtime.yaml 的 record.end_time（或 --until）或按 Ctrl+C 為止。M12 由使用者手動執行，M14 才排程。

訂閱的分配（L16）：對照股的成交與五檔放在第 1 個連線，其餘觀察清單依序只訂成交，每個連線訂到 max_subscriptions
個為止（成交、五檔各算 1 個）；連線數由 record.connections（或 --connections）決定，放不下的股票列在結束的摘要。

只讀取行情，不寫入 screener.db（REALTIME_PLAN.md 4.2 規則 3）。
"""

import logging
import sys
import time as clock
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any, TextIO

from screener.config import AppConfig
from screener.jobs import JobFailed
from screener.realtime.config import RealtimeConfig, ShioajiKeys
from screener.realtime.recorder import (BIDASK, EVENT, META, SESSION_DOWN, SUBSCRIBE, TICK, USAGE, Recorder,
                                        recording_path)
from screener.realtime.shioaji_source import ShioajiClient, ShioajiRequestError

logger = logging.getLogger(__name__)

LOOP_SLEEP_SEC = 0.2             # 主迴圈每次寫入之間的間隔
STATUS_INTERVAL_SEC = 300        # 每隔幾秒在終端與 log 印一次目前收到的筆數

ClientFactory = Callable[[], ShioajiClient]


@dataclass(frozen=True)
class Subscription:
    code: str
    kind: str                    # tick | bidask


@dataclass
class SubscriptionPlan:
    connections: list[list[Subscription]]
    left_out: list[str] = field(default_factory=list)      # 額度放不下、沒有訂閱的股票


def plan_subscriptions(codes: Sequence[str], compare_codes: Sequence[str], *, max_subscriptions: int,
                       connections: int) -> SubscriptionPlan:
    """對照股（有在 codes 中的）訂成交與五檔，放在第 1 個連線的最前面；其餘依序只訂成交。"""
    compare = [code for code in compare_codes if code in codes]
    wanted = [Subscription(code, kind) for code in compare for kind in (TICK, BIDASK)]
    wanted += [Subscription(code, TICK) for code in codes if code not in compare]
    if len([s for s in wanted if s.code in compare]) > max_subscriptions:
        raise JobFailed(f"對照股的成交與五檔共 {2 * len(compare)} 個，超過每個連線的上限 {max_subscriptions}")
    plan = SubscriptionPlan(connections=[[] for _ in range(connections)])
    slot = 0
    for subscription in wanted:
        while slot < connections and len(plan.connections[slot]) >= max_subscriptions:
            slot += 1
        if slot == connections:
            if subscription.code not in plan.left_out:
                plan.left_out.append(subscription.code)
            continue
        plan.connections[slot].append(subscription)
    return plan


def run(config: AppConfig, realtime: RealtimeConfig, keys: ShioajiKeys, *, codes: Sequence[str] | None = None,
        until: time | None = None, connections: int | None = None, client_factory: ClientFactory | None = None,
        now: Callable[[], datetime] = datetime.now, sleep: Callable[[float], None] = clock.sleep,
        out: TextIO | None = None) -> Path:
    """回傳錄製檔的路徑。codes 為 None 時錄觀察清單全部的股票。"""
    settings = realtime.record
    out = out or sys.stdout
    started = now()
    end = datetime.combine(started.date(), until or settings.end_time)
    if started >= end:
        raise JobFailed(f"現在 {started:%H:%M} 已超過結束時間 {end:%H:%M}；盤後測試請加上 --until HH:MM")
    watchlist = [stock.code for stock in config.watchlist.stocks]
    codes = list(codes) if codes is not None else watchlist
    unknown = [code for code in codes if code not in watchlist]
    if unknown:
        raise JobFailed(f"不在觀察清單的代號：{', '.join(unknown)}")
    plan = plan_subscriptions(codes, realtime.compare_codes, max_subscriptions=settings.max_subscriptions,
                              connections=connections or settings.connections)

    path = recording_path(settings.tick_dir, started.date())
    recorder = Recorder(path, settings.flush_interval_sec, clock=now).open()
    factory = client_factory or (lambda: ShioajiClient(realtime.shioaji, keys))
    clients: list[ShioajiClient] = []
    active: list[tuple[ShioajiClient, Any, Subscription]] = []
    stop_reason = "結束時間"
    try:
        for index, subscriptions in enumerate(plan.connections):
            if not subscriptions:
                continue
            client = factory()
            clients.append(client)
            _connect(client, index, recorder)
            active += _subscribe(client, index, subscriptions, recorder)
            recorder.pump()
        recorder.put(META, 0, {"event": "start", "end": end.isoformat(), "codes": len(codes),
                               "subscriptions": [len(s) for s in plan.connections], "left_out": plan.left_out})
        _say(out, f"開始錄製 {path}：{len(active)} 個訂閱（{len(clients)} 個連線），到 {end:%H:%M} 或按 Ctrl+C 停止"
                  + (f"；額度放不下 {len(plan.left_out)} 檔：{', '.join(plan.left_out)}" if plan.left_out else ""))
        stop_reason = _loop(clients, recorder, settings.usage_interval_sec, end, now, sleep, out)
    except KeyboardInterrupt:
        stop_reason = "手動停止"
    finally:
        for client, contract, subscription in reversed(active):
            try:
                client.unsubscribe(contract, subscription.kind)
            except Exception as exc:
                logger.warning("取消訂閱 %s %s 失敗：%s", subscription.code, subscription.kind, exc)
        for client in clients:
            client.logout()
        recorder.put(META, 0, {"event": "stop", "reason": stop_reason, "counts": dict(recorder.counts)})
        recorder.close()
    size = path.stat().st_size if path.exists() else 0
    _say(out, f"錄製結束（{stop_reason}）：{dict(recorder.counts)}；檔案 {path}（{size / 1024 / 1024:,.1f} MB）")
    return path


def _connect(client: ShioajiClient, index: int, recorder: Recorder) -> None:
    try:
        accounts = client.connect()
    except ShioajiRequestError as exc:
        raise JobFailed(f"第 {index + 1} 個連線：{exc}") from exc
    recorder.put(META, index, {"event": "login", "accounts": [account.account_type for account in accounts]})
    client.on_tick(lambda tick: recorder.put(TICK, index, _to_dict(tick)))
    client.on_bidask(lambda bidask: recorder.put(BIDASK, index, _to_dict(bidask)))
    client.on_event(lambda code, event_code, info, event: recorder.put(
        EVENT, index, {"resp_code": code, "event_code": event_code, "event": event, "info": info}))
    client.on_session_down(lambda: recorder.put(SESSION_DOWN, index, {}))


def _subscribe(client: ShioajiClient, index: int, subscriptions: Sequence[Subscription],
               recorder: Recorder) -> list[tuple[ShioajiClient, Any, Subscription]]:
    active = []
    for subscription in subscriptions:
        contract = client.stock(subscription.code)
        if contract is None:
            recorder.put(SUBSCRIBE, index, {"code": subscription.code, "kind": subscription.kind,
                                            "error": "合約查不到"})
            continue
        try:
            client.subscribe(contract, subscription.kind)
        except Exception as exc:
            recorder.put(SUBSCRIBE, index, {"code": subscription.code, "kind": subscription.kind,
                                            "error": f"{type(exc).__name__}: {exc}"})
            logger.warning("訂閱 %s %s 失敗：%s", subscription.code, subscription.kind, exc)
            continue
        recorder.put(SUBSCRIBE, index, {"code": subscription.code, "kind": subscription.kind, "n": len(active) + 1})
        active.append((client, contract, subscription))
    return active


def _loop(clients: Sequence[ShioajiClient], recorder: Recorder, usage_interval_sec: float, end: datetime,
          now: Callable[[], datetime], sleep: Callable[[float], None], out: TextIO) -> str:
    last_usage = last_status = now()
    _record_usage(clients, recorder)
    while True:
        current = now()
        if current >= end:
            return "結束時間"
        recorder.pump()
        if (current - last_usage).total_seconds() >= usage_interval_sec:
            _record_usage(clients, recorder)
            last_usage = current
        if (current - last_status).total_seconds() >= STATUS_INTERVAL_SEC:
            counts = Counter(recorder.counts)
            _say(out, f"{current:%H:%M:%S} 已收到：成交 {counts[TICK]:,}、五檔 {counts[BIDASK]:,}、"
                      f"事件 {counts[EVENT]}、斷線 {counts[SESSION_DOWN]}")
            last_status = current
        sleep(LOOP_SLEEP_SEC)


def _record_usage(clients: Sequence[ShioajiClient], recorder: Recorder) -> None:
    for index, client in enumerate(clients):
        try:
            usage = client.usage()
        except ShioajiRequestError as exc:
            recorder.put(USAGE, index, {"error": str(exc)})
            continue
        recorder.put(USAGE, index, {"connections": usage.connections, "bytes": usage.bytes,
                                    "limit_bytes": usage.limit_bytes, "remaining_bytes": usage.remaining_bytes})


def _to_dict(item: Any) -> dict[str, Any]:
    """Shioaji 的成交、五檔物件轉成 dict（在 callback 的執行緒中轉換，不保留 Shioaji 的物件）。"""
    try:
        return dict(item.to_dict())
    except Exception as exc:                   # 轉換失敗時保留文字，不讓 callback 拋出例外
        return {"to_dict_error": f"{type(exc).__name__}: {exc}", "repr": repr(item)}


def _say(out: TextIO, text: str) -> None:
    logger.info("%s", text)
    out.write(text + "\n")
    out.flush()
