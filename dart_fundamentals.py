"""Point-in-time fundamentals from the official OpenDART API."""

from __future__ import annotations

import io
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Iterable
from xml.etree import ElementTree

import numpy as np
import pandas as pd
import requests


DART_BASE_URL = "https://opendart.fss.or.kr/api"
DART_INDEX_CATEGORIES = ("M210000", "M220000", "M230000")


class OpenDARTError(RuntimeError):
    """An unavailable or invalid OpenDART response."""


@dataclass(frozen=True)
class ReportPeriod:
    business_year: int
    report_code: str
    available_date: date


def report_period_for(asof_date: str) -> ReportPeriod:
    """Return the latest regular report conservatively available by a date."""
    asof = pd.Timestamp(asof_date).date()
    year = asof.year
    candidates = [
        ReportPeriod(year, "11014", date(year, 11, 15)),
        ReportPeriod(year, "11012", date(year, 8, 15)),
        ReportPeriod(year, "11013", date(year, 5, 16)),
        ReportPeriod(year - 1, "11011", date(year, 4, 1)),
        ReportPeriod(year - 1, "11014", date(year - 1, 11, 15)),
    ]
    return next(period for period in candidates if period.available_date <= asof)


def _chunks(values: list[str], size: int = 100) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _number(value: object) -> float:
    text = str(value or "").strip().replace(",", "").replace("%", "")
    if text in {"", "-", "N/A"}:
        return float("nan")
    try:
        number = float(text)
    except ValueError:
        return float("nan")
    return number if np.isfinite(number) else float("nan")


def _metric_name(value: object) -> str:
    name = re.sub(r"[\s_()%·]", "", str(value or "")).lower()
    mappings = [
        (("자기자본이익률", "roe", "returnonequity"), "roe"),
        (("영업이익률", "영업이익율", "operatingincomemargin", "operatingprofitmargin"), "operating_margin"),
        (("부채비율", "debtratio"), "debt_ratio"),
        (("매출액증가율", "매출증가율", "salesgrowth", "revenuegrowth"), "sales_yoy"),
        (("영업이익증가율", "operatingincomegrowth", "operatingprofitgrowth"), "op_profit_yoy"),
    ]
    for aliases, output in mappings:
        if any(alias in name for alias in aliases):
            return output
    return ""


class OpenDARTClient:
    def __init__(
        self,
        api_key: str,
        session: requests.Session | None = None,
        request_retries: int = 4,
        retry_backoff: float = 1.0,
    ):
        if not api_key:
            raise ValueError("OpenDART API 인증키가 필요합니다.")
        if request_retries < 1:
            raise ValueError("request_retries must be at least 1")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.request_retries = request_retries
        self.retry_backoff = retry_backoff

    def _get(self, endpoint: str, params: dict[str, str]) -> requests.Response:
        last_error: requests.RequestException | None = None
        last_status: int | None = None
        for attempt in range(1, self.request_retries + 1):
            try:
                response = self.session.get(
                    f"{DART_BASE_URL}/{endpoint}",
                    params={"crtfc_key": self.api_key, **params},
                    timeout=(10, 60),
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                last_status = response.status_code
                if response.status_code == 200:
                    return response
                if response.status_code != 429 and response.status_code < 500:
                    raise OpenDARTError(f"OpenDART HTTP 요청 실패({response.status_code}): {endpoint}")
            if attempt < self.request_retries:
                time.sleep(self.retry_backoff * (2 ** (attempt - 1)))

        if last_status is not None:
            raise OpenDARTError(f"OpenDART HTTP 요청이 {self.request_retries}회 실패했습니다({last_status}): {endpoint}")
        detail = type(last_error).__name__ if last_error is not None else "UnknownError"
        raise OpenDARTError(f"OpenDART 네트워크 요청이 {self.request_retries}회 실패했습니다: {detail}")

    def corporation_codes(self) -> pd.DataFrame:
        response = self._get("corpCode.xml", {})
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                xml_name = next(name for name in archive.namelist() if name.lower().endswith(".xml"))
                root = ElementTree.fromstring(archive.read(xml_name))
        except (zipfile.BadZipFile, StopIteration, ElementTree.ParseError) as exc:
            raise OpenDARTError("OpenDART 고유번호 파일을 해석하지 못했습니다. 인증키 상태를 확인하세요.") from exc

        rows = []
        for item in root.findall("list"):
            ticker = (item.findtext("stock_code") or "").strip()
            corp_code = (item.findtext("corp_code") or "").strip()
            if re.fullmatch(r"\d{6}", ticker) and re.fullmatch(r"\d{8}", corp_code):
                rows.append({"ticker": ticker, "corp_code": corp_code})
        return pd.DataFrame(rows).drop_duplicates("ticker") if rows else pd.DataFrame(columns=["ticker", "corp_code"])

    def financial_indices(self, corp_codes: Iterable[str], period: ReportPeriod) -> list[dict]:
        codes = [str(code) for code in corp_codes if re.fullmatch(r"\d{8}", str(code))]
        rows: list[dict] = []
        for category in DART_INDEX_CATEGORIES:
            for batch in _chunks(codes):
                response = self._get(
                    "fnlttCmpnyIndx.json",
                    {
                        "corp_code": ",".join(batch),
                        "bsns_year": str(period.business_year),
                        "reprt_code": period.report_code,
                        "idx_cl_code": category,
                    },
                )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise OpenDARTError("OpenDART 재무지표 응답이 JSON이 아닙니다.") from exc
                status = str(payload.get("status", ""))
                if status == "013":
                    continue
                if status != "000":
                    message = str(payload.get("message", "알 수 없는 오류"))[:160]
                    raise OpenDARTError(f"OpenDART 재무지표 오류 {status}: {message}")
                rows.extend(row for row in payload.get("list", []) if isinstance(row, dict))
        return rows


def load_dart_fundamentals(
    api_key: str,
    asof_date: str,
    tickers: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Build one point-in-time quality/growth snapshot per listed ticker."""
    client = OpenDARTClient(api_key)
    mapping = client.corporation_codes()
    if tickers is not None:
        wanted = {str(ticker).zfill(6) for ticker in tickers}
        mapping = mapping[mapping["ticker"].isin(wanted)].copy()
    if mapping.empty:
        raise OpenDARTError("OpenDART 조회 대상의 상장사 고유번호 목록이 비어 있습니다.")

    period = report_period_for(asof_date)
    indicators = client.financial_indices(mapping["corp_code"].tolist(), period)
    corp_to_ticker = dict(zip(mapping["corp_code"], mapping["ticker"]))
    records: dict[str, dict] = {}
    for item in indicators:
        ticker = str(item.get("stock_code", "")).strip() or corp_to_ticker.get(str(item.get("corp_code", "")), "")
        metric = _metric_name(item.get("idx_nm"))
        value = _number(item.get("idx_val"))
        if not re.fullmatch(r"\d{6}", ticker) or not metric or not np.isfinite(value):
            continue
        record = records.setdefault(
            ticker,
            {
                "ticker": ticker,
                "available_date": period.available_date.isoformat(),
                "dart_business_year": period.business_year,
                "dart_report_code": period.report_code,
            },
        )
        record[metric] = value

    columns = [
        "ticker", "available_date", "dart_business_year", "dart_report_code",
        "roe", "operating_margin", "debt_ratio", "sales_yoy", "op_profit_yoy",
    ]
    return pd.DataFrame(records.values()).reindex(columns=columns)
