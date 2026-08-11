"""Daily KOSPI/KOSDAQ multi-factor stock screener.

The live path intentionally separates screening from the legacy strategy
optimizer.  The optimizer is useful for research, but its trade aggregation
is not a portfolio simulation.  This module creates a daily, explainable
candidate list using point-in-time market snapshots and price history.

Optional accounting data can be supplied through
``data/fundamentals_latest.csv``.  Rows are only used when ``available_date``
is on or before the screen date.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup
from pykrx import stock


KST = ZoneInfo("Asia/Seoul")
MARKET_INDEX_CODES = {"KOSPI": "1001", "KOSDAQ": "2001"}
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_REPORT_DIR = Path(__file__).resolve().parent / "reports"
NAVER_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DailyMultiFactorScreener/1.0)"}
NAVER_PAGE_SIZE = 50
FACTOR_WEIGHTS = {
    "momentum_score": 0.40,
    "quality_score": 0.20,
    "value_score": 0.20,
    "catalyst_score": 0.10,
    "risk_score": 0.10,
}


@dataclass
class ScreenConfig:
    markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")
    lookback_days: int = 450
    prefilter_limit: int = 300
    top_n: int = 10
    min_market_cap: float = 100_000_000_000.0
    min_trading_value: float = 500_000_000.0
    min_history: int = 180
    # 2026-08-11 trailing-stop review: score >=82 was the first profitable
    # bucket; preserve the raw score but tighten the live entry threshold.
    min_score: float = 82.0
    watch_score: float = 75.0
    min_factor_coverage: float = 0.70
    request_sleep: float = 0.08
    allow_unknown_regime: bool = True
    fundamentals_path: str = "data/fundamentals_latest.csv"
    output_dir: str = "reports"


def _as_float(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return result if np.isfinite(result) else float("nan")


def _safe_int(value: object) -> int:
    result = _as_float(value)
    return int(result) if np.isfinite(result) else 0


def _normalize_ohlcv(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()

    out = raw.reset_index().rename(
        columns={
            "날짜": "Date",
            "시가": "Open",
            "고가": "High",
            "저가": "Low",
            "종가": "Close",
            "거래량": "Volume",
            "거래대금": "TradingValue",
        }
    )
    if "Date" not in out.columns:
        out = out.rename(columns={out.columns[0]: "Date"})

    required = ["Date", "Open", "High", "Low", "Close", "Volume"]
    if any(column not in out.columns for column in required):
        return pd.DataFrame()

    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    for column in ["Open", "High", "Low", "Close", "Volume", "TradingValue"]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out.dropna(subset=required).sort_values("Date").reset_index(drop=True)
    return out


def _percentile(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    if not higher_is_better:
        values = -values
    return values.rank(pct=True, method="average") * 100.0


def _weighted_row_mean(parts: list[tuple[pd.Series, float]]) -> pd.Series:
    numerator = pd.Series(0.0, index=parts[0][0].index)
    denominator = pd.Series(0.0, index=parts[0][0].index)
    for series, weight in parts:
        usable = pd.to_numeric(series, errors="coerce").notna()
        numerator = numerator.add(pd.to_numeric(series, errors="coerce").fillna(0.0) * weight, fill_value=0.0)
        denominator = denominator.add(usable.astype(float) * weight, fill_value=0.0)
    return numerator.div(denominator.replace(0.0, np.nan))


def _mean_available(values: Iterable[object]) -> float:
    numeric = [_as_float(value) for value in values]
    numeric = [value for value in numeric if np.isfinite(value)]
    return float(np.mean(numeric)) if numeric else float("nan")


def build_price_features(raw: pd.DataFrame, min_history: int = 180) -> dict:
    """Build a point-in-time feature row from one ticker's OHLCV history."""
    df = _normalize_ohlcv(raw)
    if len(df) < min_history:
        return {}

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    high120_prev = high.rolling(120).max().shift(1)
    high200_prev = high.rolling(200).max().shift(1)
    avg_volume20 = volume.rolling(20).mean()

    true_range = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr14 = true_range.rolling(14).mean()

    daily_return = close.pct_change()
    volatility20 = daily_return.rolling(20).std() * np.sqrt(252.0)
    rolling_high60 = high.rolling(60).max()
    drawdown60 = close / rolling_high60 - 1.0

    last = len(df) - 1
    current_close = _as_float(close.iloc[last])
    current_ma20 = _as_float(ma20.iloc[last])
    current_ma60 = _as_float(ma60.iloc[last])
    current_ma120 = _as_float(ma120.iloc[last])

    if not all(np.isfinite(value) for value in [current_close, current_ma20, current_ma60]):
        return {}

    return {
        "data_date": df["Date"].iloc[last].strftime("%Y-%m-%d"),
        "close": current_close,
        "ret_20": _as_float(close.pct_change(20).iloc[last]),
        "ret_60": _as_float(close.pct_change(60).iloc[last]),
        "ret_120": _as_float(close.pct_change(120).iloc[last]),
        "ret_200": _as_float(close.pct_change(200).iloc[last]),
        "close_vs_ma20": current_close / current_ma20 - 1.0,
        "close_vs_ma60": current_close / current_ma60 - 1.0,
        "ma20_vs_ma60": current_ma20 / current_ma60 - 1.0,
        "ma60_vs_ma120": current_ma60 / current_ma120 - 1.0 if np.isfinite(current_ma120) and current_ma120 > 0 else float("nan"),
        "dist_high120": current_close / _as_float(high120_prev.iloc[last]) - 1.0,
        "dist_high200": current_close / _as_float(high200_prev.iloc[last]) - 1.0,
        "volume_vs20": _as_float(volume.iloc[last]) / _as_float(avg_volume20.iloc[last]),
        "atr_pct": _as_float(atr14.iloc[last]) / current_close,
        "volatility20": _as_float(volatility20.iloc[last]),
        "drawdown60": _as_float(drawdown60.iloc[last]),
        "ma20": current_ma20,
        "ma60": current_ma60,
        "atr14": _as_float(atr14.iloc[last]),
        "trend_ok": bool(current_close > current_ma20 > current_ma60),
        "not_chasing": bool(current_close <= current_ma20 * 1.12),
    }


def _load_fundamentals(path: str, asof_date: str) -> pd.DataFrame:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(__file__).resolve().parent / candidate
    if not candidate.exists():
        return pd.DataFrame()

    frame = pd.read_csv(candidate, dtype={"ticker": str})
    if frame.empty or "ticker" not in frame.columns or "available_date" not in frame.columns:
        raise ValueError("fundamentals file must contain ticker and available_date columns")

    frame["ticker"] = frame["ticker"].astype(str).str.extract(r"(\d{6})", expand=False)
    frame["available_date"] = pd.to_datetime(frame["available_date"], errors="coerce")
    asof = pd.Timestamp(asof_date)
    frame = frame[frame["available_date"] <= asof].copy()
    frame = frame.sort_values("available_date").drop_duplicates("ticker", keep="last")
    return frame


def _fundamental_scores(frame: pd.DataFrame) -> pd.DataFrame:
    """Add optional quality and catalyst scores without imputing missing filings."""
    result = frame.copy()
    for column in ["roe", "roic", "operating_margin", "sales_yoy", "op_profit_yoy", "debt_ratio", "fcf_yield"]:
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")

    def row_mean(columns: list[str], higher: list[bool]) -> pd.Series:
        component_scores = []
        for column, is_higher in zip(columns, higher):
            if column not in result.columns:
                continue
            component_scores.append(_percentile(result[column], is_higher))
        if not component_scores:
            return pd.Series(np.nan, index=result.index)
        return pd.concat(component_scores, axis=1).mean(axis=1, skipna=True)

    result["quality_score"] = row_mean(
        ["roe", "roic", "operating_margin", "debt_ratio"],
        [True, True, True, False],
    )
    result["catalyst_score"] = row_mean(["sales_yoy", "op_profit_yoy"], [True, True])
    return result


def _add_cross_sectional_scores(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result["momentum_score"] = _weighted_row_mean(
        [
            (_percentile(result["ret_20"]), 0.25),
            (_percentile(result["ret_60"]), 0.25),
            (_percentile(result["ret_120"]), 0.20),
            (_percentile(result["ret_200"]), 0.15),
            (_percentile(result["dist_high120"]), 0.10),
            (_percentile(result["ma20_vs_ma60"]), 0.05),
        ]
    )
    result["value_score"] = _weighted_row_mean(
        [
            (_percentile(result["per_positive"], higher_is_better=False), 0.45),
            (_percentile(result["pbr_positive"], higher_is_better=False), 0.35),
            (_percentile(result["dividend_yield"]), 0.20),
        ]
    )
    result["risk_score"] = _weighted_row_mean(
        [
            (_percentile(result["volatility20"], higher_is_better=False), 0.55),
            (_percentile(result["drawdown60"]), 0.45),
        ]
    )

    def usable_factors(row: pd.Series) -> list[tuple[float, float]]:
        usable = [(weight, _as_float(row.get(column))) for column, weight in FACTOR_WEIGHTS.items()]
        usable = [(weight, value) for weight, value in usable if np.isfinite(value)]
        return usable

    def total_score(row: pd.Series) -> float:
        usable = usable_factors(row)
        if not usable:
            return float("nan")
        return float(sum(weight * value for weight, value in usable) / sum(weight for weight, _ in usable))

    result["score"] = result.apply(total_score, axis=1)
    # The raw composite is normalized over available factors.  Surface the
    # available weight so an otherwise high score cannot be treated as equally
    # complete when quality/catalyst inputs are absent.
    result["factor_coverage"] = result.apply(
        lambda row: sum(weight for weight, _ in usable_factors(row)), axis=1
    )
    return result.sort_values(["score", "momentum_score"], ascending=[False, False]).reset_index(drop=True)


def _normalize_snapshot(frame: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["ticker"])
    out = frame.reset_index().rename(columns={frame.index.name or "index": "ticker"})
    if "ticker" not in out.columns:
        out = out.rename(columns={out.columns[0]: "ticker"})
    out["ticker"] = out["ticker"].astype(str).str.extract(r"(\d{6})", expand=False)
    if prefix:
        out = out.rename(columns={column: f"{prefix}{column}" for column in out.columns if column != "ticker"})
    return out


def _parse_naver_number(raw: str, percent: bool = False) -> float:
    value = str(raw or "").strip().replace(",", "").replace("+", "")
    if value in {"", "-", "N/A", "nan"}:
        return float("nan")
    if percent:
        value = value.replace("%", "")
    try:
        return float(value)
    except ValueError:
        return float("nan")


def _naver_get(url: str) -> BeautifulSoup:
    response = requests.get(url, headers=NAVER_HEADERS, timeout=20)
    response.raise_for_status()
    response.encoding = "euc-kr"
    return BeautifulSoup(response.text, "html.parser")


def _load_naver_market_snapshot(market: str, prefilter_limit: int) -> pd.DataFrame:
    """Fallback universe source that does not require KRX account credentials."""
    sosok = {"KOSPI": 0, "KOSDAQ": 1}[market]
    pages = max(1, int(np.ceil(prefilter_limit * 1.5 / NAVER_PAGE_SIZE)))
    rows = []
    for page in range(1, pages + 1):
        soup = _naver_get(f"https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}")
        table = soup.select_one("table.type_2")
        if table is None:
            continue
        for tr in table.select("tr"):
            cells = tr.select("td")
            link = tr.select_one('a[href*="code="]')
            if len(cells) < 12 or link is None:
                continue
            ticker = link.get("href", "").split("code=")[-1][:6]
            values = [cell.get_text(" ", strip=True) for cell in cells]
            if not ticker.isdigit() or len(ticker) != 6:
                continue
            close = _parse_naver_number(values[2])
            market_cap = _parse_naver_number(values[6]) * 100_000_000.0
            volume = _parse_naver_number(values[9])
            rows.append(
                {
                    "ticker": ticker,
                    "market": market,
                    "market_cap": market_cap,
                    "trading_value_today": close * volume if np.isfinite(close) and np.isfinite(volume) else float("nan"),
                    "close_snapshot": close,
                    "per": _parse_naver_number(values[10]),
                    "pbr": float("nan"),
                    "dividend_yield": float("nan"),
                    "roe": _parse_naver_number(values[11]),
                    "source": "naver",
                }
            )
        if len(rows) >= prefilter_limit:
            break
        if page % 5 == 0:
            time.sleep(0.2)
    return pd.DataFrame(rows).drop_duplicates("ticker")


def _load_naver_index_history(market: str, pages: int = 22) -> pd.DataFrame:
    rows = []
    for page in range(1, pages + 1):
        soup = _naver_get(f"https://finance.naver.com/sise/sise_index_day.naver?code={market}&page={page}")
        table = soup.select_one("table.type_1")
        if table is None:
            continue
        for tr in table.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.select("td")]
            if len(cells) < 2 or "." not in cells[0]:
                continue
            date = pd.to_datetime(cells[0], format="%Y.%m.%d", errors="coerce")
            close = _parse_naver_number(cells[1])
            if pd.notna(date) and np.isfinite(close):
                rows.append({"Date": date, "Close": close})
        if page % 10 == 0:
            time.sleep(0.2)
    return pd.DataFrame(rows).drop_duplicates("Date").sort_values("Date") if rows else pd.DataFrame()


def _load_market_snapshot(asof_date: str, market: str, prefilter_limit: int) -> pd.DataFrame:
    if os.getenv("KRX_ID") and os.getenv("KRX_PW"):
        try:
            price = _normalize_snapshot(stock.get_market_ohlcv_by_ticker(asof_date, market=market), "price_")
            cap = _normalize_snapshot(stock.get_market_cap_by_ticker(asof_date, market=market), "cap_")
            fundamental = _normalize_snapshot(stock.get_market_fundamental_by_ticker(asof_date, market=market), "fund_")
            if not price.empty:
                merged = price.merge(cap, on="ticker", how="left").merge(fundamental, on="ticker", how="left")
                merged["market"] = market
                merged["market_cap"] = pd.to_numeric(merged.get("cap_시가총액"), errors="coerce")
                merged["trading_value_today"] = pd.to_numeric(merged.get("cap_거래대금"), errors="coerce")
                if merged["trading_value_today"].isna().all():
                    merged["trading_value_today"] = pd.to_numeric(merged.get("price_거래대금"), errors="coerce")
                merged["close_snapshot"] = pd.to_numeric(merged.get("price_종가"), errors="coerce")
                merged["per"] = pd.to_numeric(merged.get("fund_PER"), errors="coerce")
                merged["pbr"] = pd.to_numeric(merged.get("fund_PBR"), errors="coerce")
                merged["dividend_yield"] = pd.to_numeric(merged.get("fund_DIV"), errors="coerce")
                merged["source"] = "krx"
                return merged
        except Exception as exc:
            print(f"[WARN] KRX snapshot failed for {market}; using Naver fallback: {exc}")
    return _load_naver_market_snapshot(market, prefilter_limit)


def _resolve_asof_date(requested: Optional[str], markets: tuple[str, ...]) -> str:
    if requested:
        return requested
    for market in markets:
        try:
            history = _load_naver_index_history(market, pages=1)
            if not history.empty:
                return history["Date"].max().strftime("%Y%m%d")
        except Exception:
            continue
    fallback = pd.Timestamp.now(tz=KST).tz_localize(None).normalize()
    return fallback.strftime("%Y%m%d")


def _load_regime(asof_date: str, market: str, lookback_days: int) -> dict:
    index_code = MARKET_INDEX_CODES.get(market)
    if not index_code:
        return {"state": "UNKNOWN", "reason": "unsupported market"}
    start = (pd.Timestamp(asof_date) - pd.Timedelta(days=lookback_days)).strftime("%Y%m%d")
    try:
        if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):
            normalized = _load_naver_index_history(market, pages=40)
        else:
            raw = stock.get_index_ohlcv_by_date(start, asof_date, index_code)
            if raw is None or raw.empty:
                return {"state": "UNKNOWN", "reason": "empty index data"}
            normalized = raw.reset_index().rename(columns={"날짜": "Date", "종가": "Close"})
        if normalized.empty:
            return {"state": "UNKNOWN", "reason": "empty index data"}
        close = pd.to_numeric(normalized["Close"], errors="coerce").dropna()
        if len(close) < 200:
            return {"state": "UNKNOWN", "reason": "insufficient index history"}
        ma60 = close.rolling(60).mean().iloc[-1]
        ma200 = close.rolling(200).mean().iloc[-1]
        last = close.iloc[-1]
        state = "ON" if last > ma200 and ma60 > ma200 else "OFF"
        return {
            "state": state,
            "close": float(last),
            "ma60": float(ma60),
            "ma200": float(ma200),
        }
    except Exception as exc:
        return {"state": "UNKNOWN", "reason": str(exc)[:180]}


def _safe_name(ticker: str) -> str:
    try:
        return stock.get_market_ticker_name(ticker) or ticker
    except Exception:
        return ticker


def _prepare_market_rows(
    snapshot: pd.DataFrame,
    market: str,
    asof_date: str,
    config: ScreenConfig,
    fundamentals: pd.DataFrame,
) -> tuple[pd.DataFrame, dict]:
    if snapshot.empty:
        return pd.DataFrame(), {"market": market, "snapshot_count": 0, "prefilter_count": 0, "history_count": 0}

    snapshot = snapshot.copy()
    snapshot = snapshot[
        (snapshot["market_cap"] >= config.min_market_cap)
        & (snapshot["trading_value_today"] >= config.min_trading_value)
        & (snapshot["close_snapshot"] > 0)
    ].copy()
    snapshot = snapshot.sort_values(["market_cap", "trading_value_today"], ascending=False).head(config.prefilter_limit)

    rows = []
    start_date = (pd.Timestamp(asof_date) - pd.Timedelta(days=config.lookback_days)).strftime("%Y%m%d")
    for item in snapshot.itertuples(index=False):
        ticker = str(item.ticker)
        try:
            raw = stock.get_market_ohlcv_by_date(start_date, asof_date, ticker)
            features = build_price_features(raw, min_history=config.min_history)
            if not features:
                continue
            row = {
                "ticker": ticker,
                "market": market,
                "market_cap": _as_float(item.market_cap),
                "trading_value_today": _as_float(item.trading_value_today),
                "per": _as_float(item.per),
                "pbr": _as_float(item.pbr),
                "dividend_yield": _as_float(item.dividend_yield),
                "roe": _as_float(getattr(item, "roe", float("nan"))),
                **features,
            }
            rows.append(row)
        except Exception as exc:
            print(f"[WARN] {market} {ticker} history failed: {exc}")
        if config.request_sleep > 0:
            time.sleep(config.request_sleep)

    if not rows:
        return pd.DataFrame(), {
            "market": market,
            "snapshot_count": int(len(snapshot)),
            "prefilter_count": int(len(snapshot)),
            "history_count": 0,
        }

    result = pd.DataFrame(rows)
    result["per_positive"] = result["per"].where(result["per"] > 0)
    result["pbr_positive"] = result["pbr"].where(result["pbr"] > 0)

    if not fundamentals.empty:
        result = result.merge(fundamentals, on="ticker", how="left", suffixes=("", "_fund"))
        result = _fundamental_scores(result)
    else:
        result["quality_score"] = _percentile(result["roe"]) if "roe" in result.columns else float("nan")
        result["catalyst_score"] = float("nan")

    regime = _load_regime(asof_date, market, config.lookback_days)
    result["regime_state"] = regime.get("state", "UNKNOWN")
    result["regime_ok"] = result["regime_state"].ne("OFF") if config.allow_unknown_regime else result["regime_state"].eq("ON")
    result = _add_cross_sectional_scores(result)
    result["rank"] = np.arange(1, len(result) + 1)
    result["stop_price_atr2"] = result["close"] - 2.0 * result["atr14"]
    result["action"] = np.where(
        result["regime_ok"]
        & result["trend_ok"]
        & result["not_chasing"]
        & (result["score"] >= config.min_score)
        & (result["factor_coverage"] >= config.min_factor_coverage),
        "BUY_CANDIDATE",
        np.where((result["score"] >= config.watch_score) & result["trend_ok"], "WATCH", "PASS"),
    )
    result["regime_close"] = _as_float(regime.get("close"))
    result["regime_ma60"] = _as_float(regime.get("ma60"))
    result["regime_ma200"] = _as_float(regime.get("ma200"))
    result["fundamentals_available"] = bool(not fundamentals.empty)
    result["quality_proxy_available"] = bool("roe" in result.columns and result["roe"].notna().any())

    return result, {
        "market": market,
        "snapshot_count": int(len(snapshot)),
        "prefilter_count": int(len(snapshot)),
        "history_count": int(len(result)),
        "regime": regime,
    }


def run_screen(config: ScreenConfig, asof_date: Optional[str] = None) -> tuple[pd.DataFrame, dict]:
    resolved_asof = _resolve_asof_date(asof_date, config.markets)
    fundamentals = _load_fundamentals(config.fundamentals_path, resolved_asof)
    frames = []
    stats = {
        "asof_date": resolved_asof,
        "fundamentals_available": bool(not fundamentals.empty),
        "min_score": config.min_score,
        "watch_score": config.watch_score,
        "min_factor_coverage": config.min_factor_coverage,
        "markets": [],
    }

    for market in config.markets:
        try:
            snapshot = _load_market_snapshot(resolved_asof, market, config.prefilter_limit)
        except Exception as exc:
            print(f"[ERROR] {market} snapshot failed: {exc}")
            stats["markets"].append({"market": market, "snapshot_count": 0, "prefilter_count": 0, "history_count": 0, "error": str(exc)[:180]})
            continue
        frame, market_stats = _prepare_market_rows(snapshot, market, resolved_asof, config, fundamentals)
        if not frame.empty:
            frames.append(frame)
        stats["markets"].append(market_stats)

    if not frames:
        return pd.DataFrame(), stats

    result = pd.concat(frames, ignore_index=True)
    stats["quality_proxy_available"] = bool("quality_proxy_available" in result.columns and result["quality_proxy_available"].any())
    result = result.sort_values(["action", "score"], key=lambda column: column.map({"BUY_CANDIDATE": 0, "WATCH": 1, "PASS": 2}) if column.name == "action" else column, ascending=[True, False]).reset_index(drop=True)
    result["rank"] = np.arange(1, len(result) + 1)
    return result, stats


def _format_number(value: object, digits: int = 1) -> str:
    number = _as_float(value)
    return "-" if not np.isfinite(number) else f"{number:.{digits}f}"


def build_message(result: pd.DataFrame, stats: dict, top_n: int) -> str:
    if stats.get("fundamentals_available"):
        fundamental_status = "사용"
    elif stats.get("quality_proxy_available"):
        fundamental_status = "미사용(가격·가치+ROE 프록시)"
    else:
        fundamental_status = "미사용(가격·가치 중심)"
    lines = [
        f"📊 Daily Multi-Factor Screen | 기준일 {stats.get('asof_date', 'N/A')}",
        f"재무 스냅샷: {fundamental_status}",
        f"매수 기준: 점수 ≥{_format_number(stats.get('min_score'), 1)}, 팩터 충족률 ≥{_format_number(_as_float(stats.get('min_factor_coverage')) * 100, 0)}%",
    ]
    for market_stat in stats.get("markets", []):
        regime = market_stat.get("regime", {})
        lines.append(f"{market_stat.get('market')}: regime={regime.get('state', 'UNKNOWN')} history={market_stat.get('history_count', 0)}")

    if result.empty:
        lines.append("\n조건을 만족한 후보가 없습니다.")
        return "\n".join(lines)

    candidates = result[result["action"] == "BUY_CANDIDATE"].head(top_n)
    watch = result[result["action"] == "WATCH"].head(max(3, top_n // 2))
    if candidates.empty:
        lines.append("\n오늘의 BUY_CANDIDATE가 없습니다.")
    else:
        lines.append("\n[BUY_CANDIDATE]")
        for row in candidates.itertuples(index=False):
            lines.append(
                f"- {row.ticker} {_safe_name(row.ticker)} [{row.market}] "
                f"score={_format_number(row.score, 1)} close={_format_number(row.close, 0)} "
                f"mom={_format_number(row.momentum_score, 0)} value={_format_number(row.value_score, 0)} "
                f"coverage={_format_number(row.factor_coverage * 100, 0)}% "
                f"stop(ATR2)={_format_number(row.stop_price_atr2, 0)}"
            )
    if not watch.empty:
        lines.append("\n[WATCH]")
        for row in watch.itertuples(index=False):
            lines.append(f"- {row.ticker} {_safe_name(row.ticker)} score={_format_number(row.score, 1)}")
    return "\n".join(lines)


def _json_safe(value: object) -> object:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if pd.isna(value):
        return None
    return value


def _write_reports(result: pd.DataFrame, stats: dict, output_dir: str) -> tuple[Path, Path]:
    directory = Path(output_dir)
    if not directory.is_absolute():
        directory = Path(__file__).resolve().parent / directory
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / "daily_screen.csv"
    json_path = directory / "daily_screen.json"

    result.to_csv(csv_path, index=False, encoding="utf-8-sig")
    payload = {
        "stats": stats,
        "rows": [
            {column: _json_safe(value) for column, value in row.items()}
            for row in result.to_dict(orient="records")
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path


def _send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[WARN] TELEGRAM_TOKEN 또는 TELEGRAM_CHAT_ID가 없어 알림을 건너뜁니다.")
        return
    import requests

    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": message[:3900]},
        timeout=15,
    )
    response.raise_for_status()


def _log_sheet(result: pd.DataFrame) -> None:
    try:
        from sheet_logger import log_selection
    except Exception as exc:
        print(f"[WARN] sheet logger unavailable: {exc}")
        return

    for row in result[result["action"] == "BUY_CANDIDATE"].itertuples(index=False):
        try:
            log_selection(
                ticker=row.ticker,
                name=_safe_name(row.ticker),
                close_price=float(row.close),
                method=f"daily_multifactor:{row.market}:score={float(row.score):.1f}:coverage={float(row.factor_coverage):.2f}",
                when=datetime.strptime(str(row.data_date), "%Y-%m-%d"),
            )
        except Exception as exc:
            print(f"[WARN] sheet log failed for {row.ticker}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="매일 KOSPI/KOSDAQ 멀티팩터 후보를 선별한다.")
    parser.add_argument("--as-of-date", default="", help="기준일 YYYYMMDD. 비우면 최근 거래일")
    parser.add_argument("--prefilter-limit", type=int, default=300)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--min-market-cap", type=float, default=100_000_000_000.0)
    parser.add_argument("--min-trading-value", type=float, default=500_000_000.0)
    parser.add_argument("--min-score", type=float, default=ScreenConfig.min_score)
    parser.add_argument("--watch-score", type=float, default=ScreenConfig.watch_score)
    parser.add_argument("--min-factor-coverage", type=float, default=ScreenConfig.min_factor_coverage)
    parser.add_argument("--request-sleep", type=float, default=0.08)
    parser.add_argument("--fundamentals-path", default="data/fundamentals_latest.csv")
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--log-sheet", action="store_true")
    args = parser.parse_args()

    config = ScreenConfig(
        prefilter_limit=args.prefilter_limit,
        top_n=args.top_n,
        min_market_cap=args.min_market_cap,
        min_trading_value=args.min_trading_value,
        min_score=args.min_score,
        watch_score=args.watch_score,
        min_factor_coverage=args.min_factor_coverage,
        request_sleep=args.request_sleep,
        fundamentals_path=args.fundamentals_path,
        output_dir=args.output_dir,
    )
    result, stats = run_screen(config, args.as_of_date.strip() or None)
    csv_path, json_path = _write_reports(result, stats, config.output_dir)
    message = build_message(result, stats, config.top_n)
    print(message)
    print(f"Saved {csv_path}")
    print(f"Saved {json_path}")

    if args.log_sheet:
        _log_sheet(result)
    if args.notify:
        _send_telegram(message)


if __name__ == "__main__":
    main()
