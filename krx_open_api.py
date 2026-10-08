"""Small client for the KRX Data Marketplace Open API.

The API returns one market-wide daily snapshot per request.  That is a better
fit for daily screening than scraping an HTML page or making a separate
unofficial request for every ticker.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

import numpy as np
import pandas as pd
import requests


BASE_URL = "https://data-dbg.krx.co.kr/svc/apis"
MARKET_ENDPOINTS = {"KOSPI": "sto/stk_bydd_trd", "KOSDAQ": "sto/ksq_bydd_trd"}
INDEX_ENDPOINTS = {"KOSPI": "idx/kospi_dd_trd", "KOSDAQ": "idx/kosdaq_dd_trd"}
INDEX_NAMES = {"KOSPI": {"KOSPI", "코스피"}, "KOSDAQ": {"KOSDAQ", "코스닥"}}


class KRXOpenAPIError(RuntimeError):
    """An unavailable or malformed KRX Open API response."""


def _number(value: object) -> float:
    text = str(value or "").strip().replace(",", "")
    if text in {"", "-", "N/A"}:
        return float("nan")
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _ticker(value: object) -> str:
    """Convert either a KRX short code or ISIN-form issue code to six digits."""
    code = str(value or "").strip().upper()
    if re.fullmatch(r"\d{6}", code):
        return code
    isin_match = re.search(r"KR7(\d{6})", code)
    return isin_match.group(1) if isin_match else ""


def _records(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise KRXOpenAPIError("KRX API가 JSON 객체를 반환하지 않았습니다.")
    for key in ("OutBlock_1", "output", "result"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            return [value]
    # KRX returns an empty object for some non-trading days.
    if not payload:
        return []
    raise KRXOpenAPIError(f"KRX API 응답 형식을 해석할 수 없습니다: {list(payload)[:4]}")


@dataclass
class KRXOpenAPIClient:
    auth_key: str
    request_sleep: float = 0.02
    session: requests.Session | None = None
    max_workers: int = 8

    def __post_init__(self) -> None:
        if not self.auth_key:
            raise ValueError("KRX Open API 인증키가 필요합니다.")
        if self.max_workers < 1:
            raise ValueError("max_workers must be at least 1")

    def _get(self, endpoint: str, date: str) -> list[dict]:
        # A supplied Session is useful for unit tests.  Live history loading is
        # concurrent, so use requests.get rather than sharing one Session
        # between worker threads.
        get = self.session.get if self.session is not None else requests.get
        response = get(
            f"{BASE_URL}/{endpoint}",
            headers={"AUTH_KEY": self.auth_key},
            params={"basDd": date},
            timeout=30,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise KRXOpenAPIError(f"KRX API 요청 실패({response.status_code}): {endpoint} {date}") from exc
        try:
            return _records(response.json())
        except ValueError as exc:
            raise KRXOpenAPIError("KRX API가 JSON이 아닌 응답을 반환했습니다.") from exc

    def market_daily(self, market: str, date: str) -> pd.DataFrame:
        endpoint = MARKET_ENDPOINTS[market]
        rows = self._get(endpoint, date)
        if self.request_sleep:
            time.sleep(self.request_sleep)
        if not rows:
            return pd.DataFrame(columns=["ticker", "name", "Date", "Open", "High", "Low", "Close", "Volume", "TradingValue", "market_cap", "market"])

        frame = pd.DataFrame(rows)
        output = pd.DataFrame(
            {
                "ticker": frame.get("ISU_CD", pd.Series(dtype=str)).map(_ticker),
                "name": frame.get("ISU_NM", pd.Series(dtype=str)).astype(str),
                "Date": pd.to_datetime(frame.get("BAS_DD"), format="%Y%m%d", errors="coerce"),
                "Open": frame.get("TDD_OPNPRC", pd.Series(dtype=object)).map(_number),
                "High": frame.get("TDD_HGPRC", pd.Series(dtype=object)).map(_number),
                "Low": frame.get("TDD_LWPRC", pd.Series(dtype=object)).map(_number),
                "Close": frame.get("TDD_CLSPRC", pd.Series(dtype=object)).map(_number),
                "Volume": frame.get("ACC_TRDVOL", pd.Series(dtype=object)).map(_number),
                "TradingValue": frame.get("ACC_TRDVAL", pd.Series(dtype=object)).map(_number),
                "market_cap": frame.get("MKTCAP", pd.Series(dtype=object)).map(_number),
                "market": market,
            }
        )
        return output.dropna(subset=["ticker", "Date", "Close"]).query("ticker != ''").reset_index(drop=True)

    def market_history(self, market: str, start: str, end: str, tickers: Iterable[str] | None = None) -> pd.DataFrame:
        wanted = {str(ticker).zfill(6) for ticker in tickers} if tickers is not None else None
        rows = []
        dates = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D").strftime("%Y%m%d").tolist()
        # One request contains the entire market for the selected date.  Fetch
        # dates concurrently, while keeping the worker count deliberately low
        # enough to stay far below KRX's daily request limit.
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(dates) or 1)) as executor:
            daily_frames = executor.map(lambda date: self.market_daily(market, date), dates)
            for daily in daily_frames:
                if wanted is not None and not daily.empty:
                    daily = daily[daily["ticker"].isin(wanted)]
                if not daily.empty:
                    rows.append(daily)
        if not rows:
            return pd.DataFrame(columns=["ticker", "name", "Date", "Open", "High", "Low", "Close", "Volume", "TradingValue", "market_cap", "market"])
        return pd.concat(rows, ignore_index=True).sort_values(["ticker", "Date"]).reset_index(drop=True)

    def index_history(self, market: str, start: str, end: str) -> pd.DataFrame:
        rows = []
        wanted_names = INDEX_NAMES[market]
        dates = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="D").strftime("%Y%m%d").tolist()

        def load_one(date: str) -> list[dict]:
            response_rows = self._get(INDEX_ENDPOINTS[market], date)
            if self.request_sleep:
                time.sleep(self.request_sleep)
            return response_rows

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(dates) or 1)) as executor:
            response_sets = executor.map(load_one, dates)
            for response_rows in response_sets:
                for row in response_rows:
                    if str(row.get("IDX_NM", "")).strip() not in wanted_names:
                        continue
                    close = _number(row.get("CLSPRC_IDX"))
                    date = pd.to_datetime(row.get("BAS_DD"), format="%Y%m%d", errors="coerce")
                    if pd.notna(date) and np.isfinite(close):
                        rows.append({"Date": date, "Close": close})
        return pd.DataFrame(rows).drop_duplicates("Date").sort_values("Date").reset_index(drop=True) if rows else pd.DataFrame(columns=["Date", "Close"])

    def latest_market_day(self, market: str, before: str, max_days: int = 10) -> str | None:
        current = pd.Timestamp(before)
        for _ in range(max_days):
            daily = self.market_daily(market, current.strftime("%Y%m%d"))
            if not daily.empty:
                return current.strftime("%Y%m%d")
            current -= timedelta(days=1)
        return None

    def ticker_history(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        """Look up one ticker while limiting the API calls to its actual market."""
        normalized = str(ticker).zfill(6)
        for market in MARKET_ENDPOINTS:
            latest = self.market_daily(market, end)
            if normalized in set(latest.get("ticker", [])):
                return self.market_history(market, start, end, [normalized]).drop(columns=["ticker", "name", "market", "market_cap"], errors="ignore")
        return pd.DataFrame()
