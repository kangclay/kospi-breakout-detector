"""Independent entry and exit timing engine for recommended stocks.

This module deliberately does not create orders, send notifications, or alter
the daily multi-factor screener.  It turns daily OHLCV data into explicit,
auditable timing states that a separate report or automation can consume.

Entry timing:
* BUY_NOW: 20-day closing breakout with 20% volume confirmation in an uptrend.
* BUY_PULLBACK: bullish reversal near the 20-day moving average in an uptrend.
* WAIT_BREAKOUT / WAIT_PULLBACK / AVOID: no entry yet.

Position timing:
* Initial stop: entry price minus 2.5 ATR(14).
* Trail activates after a 1R gain and then trails at highest high minus 3 ATR.
* Stop fills model a downside gap at the opening price, otherwise at the stop.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd
from pykrx import stock

from daily_screener import _normalize_ohlcv


@dataclass(frozen=True)
class TimingConfig:
    min_history: int = 60
    breakout_days: int = 20
    breakout_volume_multiple: float = 1.20
    pullback_atr_multiple: float = 0.50
    initial_stop_atr_multiple: float = 2.50
    trail_activation_r: float = 1.00
    trail_atr_multiple: float = 3.00


@dataclass(frozen=True)
class Position:
    entry_date: str
    entry_price: float


def _as_float(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return number if np.isfinite(number) else float("nan")


def _add_indicators(raw: pd.DataFrame, config: TimingConfig) -> pd.DataFrame:
    frame = _normalize_ohlcv(raw).copy()
    if frame.empty:
        return frame

    high = frame["High"]
    low = frame["Low"]
    close = frame["Close"]
    volume = frame["Volume"]
    true_range = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    frame["atr14"] = true_range.rolling(14).mean()
    frame["ma20"] = close.rolling(20).mean()
    frame["ma60"] = close.rolling(60).mean()
    frame["avg_volume20"] = volume.rolling(20).mean()
    frame["high_breakout"] = high.rolling(config.breakout_days).max().shift(1)
    frame["prev_close"] = close.shift(1)
    return frame


def entry_timing(raw: pd.DataFrame, config: TimingConfig = TimingConfig()) -> dict:
    """Return the current entry timing state for one ticker's daily OHLCV."""
    frame = _add_indicators(raw, config)
    if len(frame) < config.min_history:
        return {"action": "INSUFFICIENT_DATA", "reason": f"최소 {config.min_history}거래일 이력이 필요합니다."}

    last = frame.iloc[-1]
    required = [last.get(column) for column in ["Close", "High", "Volume", "atr14", "ma20", "ma60", "avg_volume20", "high_breakout", "prev_close"]]
    if not all(np.isfinite(_as_float(value)) for value in required):
        return {"action": "INSUFFICIENT_DATA", "reason": "매수 타이밍 지표를 계산할 수 없습니다."}

    close = float(last.Close)
    atr = float(last.atr14)
    ma20 = float(last.ma20)
    ma60 = float(last.ma60)
    trend_ok = close > ma20 > ma60
    breakout = close >= float(last.high_breakout)
    volume_ok = float(last.Volume) >= float(last.avg_volume20) * config.breakout_volume_multiple
    near_ma20 = ma20 <= close <= ma20 + atr * config.pullback_atr_multiple
    bullish_reversal = close > float(last.prev_close)
    initial_stop = close - atr * config.initial_stop_atr_multiple

    common = {
        "as_of_date": pd.Timestamp(last.Date).strftime("%Y-%m-%d"),
        "close": close,
        "atr14": atr,
        "ma20": ma20,
        "ma60": ma60,
        "initial_stop": initial_stop,
        "risk_per_share": close - initial_stop,
    }
    if trend_ok and breakout and volume_ok:
        return {"action": "BUY_NOW", "reason": "20일 신고가 돌파와 거래량 확인", **common}
    if trend_ok and near_ma20 and bullish_reversal:
        return {"action": "BUY_PULLBACK", "reason": "20일선 부근의 상승 반전", **common}
    if trend_ok:
        return {"action": "WAIT_BREAKOUT", "reason": "상승 추세지만 돌파 또는 거래량 확인 대기", **common}
    if close > ma60:
        return {"action": "WAIT_PULLBACK", "reason": "20일선 추세 회복 또는 반전 확인 대기", **common}
    return {"action": "AVOID", "reason": "종가가 60일선 아래여서 신규 진입 보류", **common}


def position_timing(raw: pd.DataFrame, position: Position, config: TimingConfig = TimingConfig()) -> dict:
    """Evaluate an existing position without look-ahead in daily stop updates.

    A high recorded today only raises tomorrow's executable stop.  This avoids
    treating an unknown intraday high-low ordering as favorable to the model.
    """
    if position.entry_price <= 0:
        raise ValueError("entry_price must be positive")
    frame = _add_indicators(raw, config)
    if len(frame) < config.min_history:
        return {"action": "INSUFFICIENT_DATA", "reason": f"최소 {config.min_history}거래일 이력이 필요합니다."}

    entry_date = pd.Timestamp(position.entry_date)
    eligible = frame.index[frame["Date"] >= entry_date]
    if len(eligible) == 0:
        return {"action": "PENDING_ENTRY", "reason": "진입일 이후 가격 데이터가 없습니다."}

    entry_index = int(eligible[0])
    entry_atr = _as_float(frame.loc[entry_index, "atr14"])
    if not np.isfinite(entry_atr) or entry_atr <= 0:
        return {"action": "INSUFFICIENT_DATA", "reason": "진입일 ATR을 계산할 수 없습니다."}

    initial_stop = position.entry_price - entry_atr * config.initial_stop_atr_multiple
    risk_per_share = position.entry_price - initial_stop
    high_water = position.entry_price
    stop_price = initial_stop
    trail_active = False

    for index in range(entry_index + 1, len(frame)):
        bar = frame.loc[index]
        open_price = _as_float(bar.Open)
        low_price = _as_float(bar.Low)
        high_price = _as_float(bar.High)
        bar_date = pd.Timestamp(bar.Date).strftime("%Y-%m-%d")
        if not all(np.isfinite(value) for value in [open_price, low_price, high_price]):
            continue
        if open_price <= stop_price:
            return {
                "action": "EXITED",
                "reason": "갭하락으로 시가 청산",
                "exit_date": bar_date,
                "exit_price": open_price,
                "stop_price": stop_price,
                "initial_stop": initial_stop,
                "highest_high": high_water,
                "trail_active": trail_active,
            }
        if low_price <= stop_price:
            return {
                "action": "EXITED",
                "reason": "ATR 스탑 도달",
                "exit_date": bar_date,
                "exit_price": stop_price,
                "stop_price": stop_price,
                "initial_stop": initial_stop,
                "highest_high": high_water,
                "trail_active": trail_active,
            }

        high_water = max(high_water, high_price)
        if high_water >= position.entry_price + risk_per_share * config.trail_activation_r:
            trail_active = True
        atr = _as_float(bar.atr14)
        if trail_active and np.isfinite(atr) and atr > 0:
            stop_price = max(stop_price, high_water - atr * config.trail_atr_multiple)

    last = frame.iloc[-1]
    result = {
        "action": "TRAIL_ACTIVE" if trail_active else "HOLD",
        "reason": "ATR 트레일링스탑 적용 중" if trail_active else "초기 리스크 관리 구간",
        "as_of_date": pd.Timestamp(last.Date).strftime("%Y-%m-%d"),
        "close": float(last.Close),
        "entry_price": position.entry_price,
        "initial_stop": initial_stop,
        "stop_price": stop_price,
        "highest_high": high_water,
        "trail_active": trail_active,
        "unrealized_return": float(last.Close) / position.entry_price - 1.0,
    }
    return result


def _fetch_ohlcv(ticker: str, as_of_date: str, lookback_days: int = 360) -> pd.DataFrame:
    end = pd.Timestamp(as_of_date)
    start = end - pd.Timedelta(days=lookback_days)
    return stock.get_market_ohlcv_by_date(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker)


def _json_safe(value: object) -> object:
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    return value


def _column(frame: pd.DataFrame, choices: Iterable[str]) -> Optional[str]:
    normalized = {str(column).strip().lower(): str(column) for column in frame.columns}
    for choice in choices:
        if choice.lower() in normalized:
            return normalized[choice.lower()]
    return None


def _ticker(value: object) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    return digits.zfill(6) if digits else ""


def _batch_input(path: str, positions: bool) -> list[dict]:
    frame = pd.read_csv(path, dtype=str)
    ticker_column = _column(frame, ["ticker", "티커"])
    name_column = _column(frame, ["name", "종목명"])
    date_column = _column(frame, ["entry_date", "추천일", "일자", "date"])
    price_column = _column(frame, ["entry_price", "추천가", "종가", "close"])
    if ticker_column is None:
        raise ValueError("CSV에는 ticker 또는 티커 열이 필요합니다.")
    if positions and (date_column is None or price_column is None):
        raise ValueError("보유 포지션 CSV에는 entry_date/entry_price 또는 일자/종가 열이 필요합니다.")

    rows = []
    for item in frame.to_dict(orient="records"):
        ticker = _ticker(item.get(ticker_column))
        if not ticker:
            continue
        row = {"ticker": ticker, "name": str(item.get(name_column, "") or "")}
        if positions:
            entry_price = _as_float(item.get(price_column))
            entry_date = pd.to_datetime(item.get(date_column), errors="coerce")
            if not np.isfinite(entry_price) or entry_price <= 0 or pd.isna(entry_date):
                continue
            row.update({"entry_date": entry_date.strftime("%Y-%m-%d"), "entry_price": entry_price})
        rows.append(row)
    return rows


def batch_timing(
    rows: Iterable[dict],
    as_of_date: str,
    positions: bool,
    config: TimingConfig = TimingConfig(),
) -> list[dict]:
    """Calculate independent timing states for a recommendation or position list."""
    result = []
    cache: dict[str, pd.DataFrame] = {}
    for row in rows:
        ticker = _ticker(row.get("ticker"))
        if not ticker:
            continue
        try:
            if ticker not in cache:
                cache[ticker] = _fetch_ohlcv(ticker, as_of_date)
            timing = (
                position_timing(cache[ticker], Position(str(row["entry_date"]), float(row["entry_price"])), config)
                if positions
                else entry_timing(cache[ticker], config)
            )
        except Exception as exc:
            timing = {"action": "ERROR", "reason": str(exc)[:180]}
        source = {"ticker": ticker, "name": row.get("name", "")}
        if positions:
            source.update({"entry_date": row.get("entry_date", ""), "entry_price": row.get("entry_price")})
        result.append({**source, **timing})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="추천 종목의 독립 매수·매도 타이밍을 계산합니다.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--ticker", help="6자리 종목코드")
    selection.add_argument("--recommendations-csv", help="추천 목록 CSV. ticker 또는 티커 열 필요")
    selection.add_argument("--positions-csv", help="보유 목록 CSV. ticker·entry_date·entry_price 열 필요")
    parser.add_argument("--as-of-date", required=True, help="기준일 YYYYMMDD")
    parser.add_argument("--entry-date", default="", help="보유 포지션 진입일 YYYY-MM-DD")
    parser.add_argument("--entry-price", type=float, default=0.0, help="보유 포지션 진입가")
    parser.add_argument("--output", default="", help="결과 JSON 파일 경로")
    args = parser.parse_args()

    config = TimingConfig()
    if args.recommendations_csv or args.positions_csv:
        is_positions = bool(args.positions_csv)
        rows = _batch_input(args.positions_csv or args.recommendations_csv, positions=is_positions)
        results = batch_timing(rows, args.as_of_date, positions=is_positions, config=config)
        payload = {
            "as_of_date": args.as_of_date,
            "mode": "positions" if is_positions else "recommendations",
            "config": asdict(config),
            "results": [{key: _json_safe(value) for key, value in row.items()} for row in results],
        }
    else:
        raw = _fetch_ohlcv(args.ticker.zfill(6), args.as_of_date)
        if args.entry_date and args.entry_price > 0:
            result = position_timing(raw, Position(args.entry_date, args.entry_price), config)
        else:
            result = entry_timing(raw, config)
        payload = {"ticker": args.ticker.zfill(6), "config": asdict(config), "result": {key: _json_safe(value) for key, value in result.items()}}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
