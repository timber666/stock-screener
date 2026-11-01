"""證交所 ISIN 清單的上市日／上櫃日（PROJECT_PLAN.md 14.2、D38）。"""

from datetime import date

import pytest
import requests

from conftest import FakeResponse, FakeSession, FakeTime
from screener.config import IsinSettings
from screener.data.base import DataSourceError
from screener.data.isin import URLS, IsinClient, parse_listings
from screener.models import Listing, Market

SETTINGS = IsinSettings(timeout_sec=60, retry=1, retry_backoff_sec=5, request_interval_sec=1)

# 取自 2026-11-01 實際頁面的片段：分類標題列、股票列、權證列
TSE_PAGE = """<table><tr><td colspan=7><b> 股票 <b> </td></tr>
<tr><td bgcolor=#FAFAD2>1101　台泥</td><td bgcolor=#FAFAD2>TW0001101004</td><td bgcolor=#FAFAD2>1962/02/09</td>
<td bgcolor=#FAFAD2>上市</td><td bgcolor=#FAFAD2>水泥工業</td><td bgcolor=#FAFAD2>ESVUFR</td><td bgcolor=#FAFAD2></td></tr>
<tr><td bgcolor=#FAFAD2>3135　凌航</td><td bgcolor=#FAFAD2>TW0003135000</td><td bgcolor=#FAFAD2>2025/08/06</td>
<td bgcolor=#FAFAD2>上市</td><td bgcolor=#FAFAD2>半導體業</td><td bgcolor=#FAFAD2>ESVUFR</td><td bgcolor=#FAFAD2></td></tr>
<tr><td colspan=7><b> 上市認購(售)權證 <b> </td></tr>
<tr><td>031350　凱基凌航6A購01</td><td>TW26Z0313505</td><td>2026/10/08</td><td>上市</td><td></td><td>RWSCCA</td><td></td></tr>
</table>"""
OTC_PAGE = """<table><tr><td>4577　達航科技</td><td>TW0004577002</td><td>2022/06/14</td><td>上櫃</td>
<td>其他電子業</td><td>ESVUFR</td><td></td></tr></table>"""


def page(html: str) -> FakeResponse:
    return FakeResponse(200, text="", content=html.encode("cp950"))


def test_parses_listing_dates_and_skips_title_rows() -> None:
    listings = parse_listings(TSE_PAGE, Market.TSE)

    assert listings["3135"] == Listing("3135", Market.TSE, date(2025, 8, 6))
    assert set(listings) == {"1101", "3135", "031350"}


def test_fetches_both_markets_from_big5_pages() -> None:
    session = FakeSession(page(TSE_PAGE), page(OTC_PAGE))
    timer = FakeTime()

    listings = IsinClient(SETTINGS, session=session, sleep=timer.sleep, clock=timer.clock).fetch_listings()

    assert [call["url"] for call in session.calls] == [URLS[Market.TSE], URLS[Market.OTC]]
    assert listings["4577"] == Listing("4577", Market.OTC, date(2022, 6, 14))


def test_connection_broken_mid_download_is_retried() -> None:
    # 約 10 MB 的頁面傳到一半連線中斷時，requests 拋出 ChunkedEncodingError，不是 ConnectionError
    broken = requests.exceptions.ChunkedEncodingError("Connection broken: IncompleteRead(4096 bytes read)")
    session = FakeSession(broken, page(TSE_PAGE), page(OTC_PAGE))
    timer = FakeTime()

    listings = IsinClient(SETTINGS, session=session, sleep=timer.sleep, clock=timer.clock).fetch_listings()

    assert len(session.calls) == 3 and SETTINGS.retry_backoff_sec in timer.sleeps
    assert listings["4577"] == Listing("4577", Market.OTC, date(2022, 6, 14))


def test_empty_pages_are_rejected() -> None:
    session = FakeSession(page("<html></html>"), page("<html></html>"))
    timer = FakeTime()

    with pytest.raises(DataSourceError, match="格式可能已變動"):
        IsinClient(SETTINGS, session=session, sleep=timer.sleep, clock=timer.clock).fetch_listings()
