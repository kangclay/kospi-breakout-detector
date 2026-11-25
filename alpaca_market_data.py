"""Small read-only client for Alpaca's historical stock market-data API."""

from __future__ import annotations

import os
import time
from typing import Iterable, Optional

import pandas as pd
import requests


class AlpacaMarketDataError(RuntimeError):
    """Raised when Alpaca market data cannot be retrieved safely."""


class AlpacaMarketDataClient:
    BASE_URL = "https://data.alpaca.markets"

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        *,
        feed: str = "iex",
        session: Optional[requests.Session] = None,
        request_retries: int = 3,
        retry_backoff: float = 1.0,
    ) -> None:
        self.api_key = api_key or os.getenv("ALPACA_API_KEY", "")
        self.api_secret = api_secret or os.getenv("ALPACA_API_SECRET", "")
        if not self.api_key or not self.api_secret:
            raise AlpacaMarketDataError(
                "ALPACA_API_KEY 또는 ALPACA_API_SECRET 환경 변수가 설정되지 않았습니다."
            )
        self.feed = feed
        self.session = session or requests.Session()
        self.request_retries = max(1, request_retries)
        self.retry_backoff = max(0.0, retry_backoff)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
        }

    def _get(self, path: str, params: dict) -> dict:
        last_error: Optional[Exception] = None
        for attempt in range(self.request_retries):
            try:
                response = self.session.get(
                    f"{self.BASE_URL}{path}",
                    headers=self.headers,
                    params=params,
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("JSON object가 아닌 응답")
                return payload
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self.request_retries:
                    time.sleep(self.retry_backoff * (2**attempt))
        status = getattr(getattr(last_error, "response", None), "status_code", "unknown")
        raise AlpacaMarketDataError(f"Alpaca 시세 요청 실패({status})") from last_error

    def daily_bars(
        self,
        symbols: Iterable[str],
        start: str,
        end: str,
    ) -> dict[str, pd.DataFrame]:
        """Return corporate-action-adjusted daily OHLCV grouped by symbol.

        This method only calls ``data.alpaca.markets``. It never submits an
        order to the paper or live trading endpoints.
        """
        normalized = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
        if not normalized:
            return {}

        params = {
            "symbols": ",".join(normalized),
            "timeframe": "1Day",
            "start": start,
            "end": end,
            "adjustment": "all",
            "feed": self.feed,
            "sort": "asc",
            "limit": 10_000,
        }
        rows: dict[str, list[dict]] = {symbol: [] for symbol in normalized}
        page_token = None
        while True:
            request_params = dict(params)
            if page_token:
                request_params["page_token"] = page_token
            payload = self._get("/v2/stocks/bars", request_params)
            for symbol, bars in (payload.get("bars") or {}).items():
                if symbol in rows and isinstance(bars, list):
                    rows[symbol].extend(bars)
            page_token = payload.get("next_page_token")
            if not page_token:
                break

        result: dict[str, pd.DataFrame] = {}
        rename = {"t": "Date", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"}
        for symbol, bars in rows.items():
            if not bars:
                continue
            frame = pd.DataFrame(bars).rename(columns=rename)
            required = list(rename.values())
            if any(column not in frame.columns for column in required):
                continue
            frame = frame[required].copy()
            frame["Date"] = pd.to_datetime(frame["Date"], utc=True, errors="coerce").dt.tz_localize(None)
            for column in required[1:]:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
            frame = frame.dropna().sort_values("Date").drop_duplicates("Date", keep="last")
            frame["TradingValue"] = frame["Close"] * frame["Volume"]
            if not frame.empty:
                result[symbol] = frame.reset_index(drop=True)
        return result
