"""Independent US large-cap daily screener backed by Alpaca market data.

This module is deliberately separate from the Korean multi-factor model. Its
scores are cross-sectional price/liquidity scores and are not comparable with
the Korean score. It reads market data only and never places orders.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from alpaca_market_data import AlpacaMarketDataClient
from daily_screener import build_price_features


ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
DEFAULT_UNIVERSE = ROOT / "data" / "us_nasdaq100.csv"
DEFAULT_OUTPUT_DIR = ROOT / "reports"


@dataclass
class USScreenConfig:
    universe_path: str = str(DEFAULT_UNIVERSE)
    benchmark: str = "QQQ"
    lookback_days: int = 420
    min_history: int = 210
    min_price: float = 5.0
    min_avg_dollar_volume20: float = 20_000_000.0
    min_score: float = 82.0
    off_watch_min_score: float = 88.0
    watch_score: float = 75.0
    breakout_volume_multiple: float = 1.30
    top_n: int = 10
    feed: str = "iex"
    output_dir: str = str(DEFAULT_OUTPUT_DIR)


def _percentile(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    if not higher_is_better:
        values = -values
    return values.rank(pct=True, method="average") * 100.0


def _load_universe(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype=str)
    required = {"symbol", "name"}
    if not required.issubset(frame.columns):
        raise RuntimeError(f"미국 유니버스 파일에 {sorted(required)} 열이 필요합니다.")
    frame = frame[["symbol", "name"]].copy()
    frame["symbol"] = frame["symbol"].str.strip().str.upper()
    frame["name"] = frame["name"].fillna(frame["symbol"]).str.strip()
    return frame[frame["symbol"].ne("")].drop_duplicates("symbol").reset_index(drop=True)


def _market_regime(benchmark: pd.DataFrame) -> dict:
    if benchmark is None or benchmark.empty or len(benchmark) < 200:
        return {"state": "UNKNOWN", "reason": "benchmark history insufficient"}
    close = pd.to_numeric(benchmark["Close"], errors="coerce").dropna()
    if len(close) < 200:
        return {"state": "UNKNOWN", "reason": "benchmark history insufficient"}
    last = float(close.iloc[-1])
    ma60 = float(close.rolling(60).mean().iloc[-1])
    ma200 = float(close.rolling(200).mean().iloc[-1])
    return {
        "state": "ON" if last > ma200 and ma60 > ma200 else "OFF",
        "close": last,
        "ma60": ma60,
        "ma200": ma200,
    }


def _score_rows(rows: pd.DataFrame) -> pd.DataFrame:
    result = rows.copy()
    result["momentum_score"] = (
        _percentile(result["ret_20"]) * 0.15
        + _percentile(result["ret_60"]) * 0.25
        + _percentile(result["ret_120"]) * 0.35
        + _percentile(result["ret_200"]) * 0.25
    )
    result["trend_score"] = (
        _percentile(result["close_vs_ma20"]) * 0.20
        + _percentile(result["close_vs_ma60"]) * 0.30
        + _percentile(result["ma20_vs_ma60"]) * 0.25
        + _percentile(result["dist_high120"]) * 0.25
    )
    result["risk_score"] = (
        _percentile(result["volatility20"], higher_is_better=False) * 0.55
        + _percentile(result["drawdown60"]) * 0.45
    )
    result["liquidity_score"] = _percentile(result["avg_trading_value20"])
    result["score"] = (
        result["momentum_score"] * 0.45
        + result["trend_score"] * 0.30
        + result["risk_score"] * 0.15
        + result["liquidity_score"] * 0.10
    )
    return result


def _assign_actions(result: pd.DataFrame, config: USScreenConfig, regime_state: str) -> pd.DataFrame:
    output = result.copy()
    threshold = config.off_watch_min_score if regime_state == "OFF" else config.min_score
    quality = (
        output["trend_ok"]
        & output["not_chasing"]
        & (output["score"] >= threshold)
        & (output["close"] >= config.min_price)
        & (output["avg_trading_value20"] >= config.min_avg_dollar_volume20)
    )
    confirmed = output["breakout20"] & (output["volume_vs20"] >= config.breakout_volume_multiple)
    if regime_state == "OFF":
        output["action"] = np.where(quality, "US_OFF_WATCH", np.where((output["score"] >= config.watch_score) & output["trend_ok"], "US_WATCH", "PASS"))
    else:
        output["action"] = np.select(
            [quality & confirmed, quality],
            ["US_BUY_CANDIDATE", "US_ENTRY_WATCH"],
            default=np.where((output["score"] >= config.watch_score) & output["trend_ok"], "US_WATCH", "PASS"),
        )
    return output


def run_screen(
    config: USScreenConfig,
    as_of_date: Optional[str] = None,
    client: Optional[AlpacaMarketDataClient] = None,
) -> tuple[pd.DataFrame, dict]:
    universe = _load_universe(config.universe_path)
    requested = pd.Timestamp(as_of_date).date() if as_of_date else datetime.now(ET).date()
    start = requested - timedelta(days=config.lookback_days)
    # One extra day lets a KST-morning job include the completed prior US session.
    end = requested + timedelta(days=1)
    market_client = client or AlpacaMarketDataClient(feed=config.feed)
    symbols = universe["symbol"].tolist()
    bars = market_client.daily_bars(
        [*symbols, config.benchmark],
        start.isoformat(),
        end.isoformat(),
    )
    benchmark = bars.get(config.benchmark, pd.DataFrame())
    if benchmark.empty:
        raise RuntimeError(f"시장국면 기준 {config.benchmark} 시세가 없습니다.")
    resolved_date = pd.Timestamp(benchmark["Date"].max()).date()
    benchmark = benchmark[pd.to_datetime(benchmark["Date"]).dt.date <= resolved_date]
    regime = _market_regime(benchmark)

    names = universe.set_index("symbol")["name"].to_dict()
    rows = []
    for symbol in symbols:
        history = bars.get(symbol, pd.DataFrame())
        if history.empty:
            continue
        history = history[pd.to_datetime(history["Date"]).dt.date <= resolved_date]
        features = build_price_features(history, min_history=config.min_history)
        if not features:
            continue
        rows.append({"ticker": symbol, "name": names.get(symbol, symbol), **features})

    if not rows:
        result = pd.DataFrame()
    else:
        result = _score_rows(pd.DataFrame(rows))
        result = _assign_actions(result, config, regime["state"])
        order = {"US_BUY_CANDIDATE": 0, "US_ENTRY_WATCH": 1, "US_OFF_WATCH": 2, "US_WATCH": 3, "PASS": 4}
        result["regime_state"] = regime["state"]
        result["action_order"] = result["action"].map(order).fillna(99)
        result = result.sort_values(["action_order", "score"], ascending=[True, False]).drop(columns="action_order")

    stats = {
        "asof_date": resolved_date.isoformat(),
        "requested_date": requested.isoformat(),
        "provider": f"alpaca_{config.feed}",
        "universe": "Nasdaq-100",
        "universe_count": int(len(universe)),
        "history_count": int(len(result)),
        "regime": regime,
        "config": asdict(config),
    }
    return result.reset_index(drop=True), stats


def build_message(result: pd.DataFrame, stats: dict, top_n: int = 10) -> str:
    regime = stats.get("regime", {})
    lines = [
        f"🇺🇸 US Daily Price-Factor Screen | 기준일 {stats.get('asof_date', '-')}",
        f"유니버스: Nasdaq-100 | 시세: Alpaca IEX | 시장국면(QQQ): {regime.get('state', 'UNKNOWN')}",
        "국내 멀티팩터 점수와 비교 불가 · 자동 주문 없음",
    ]
    if result.empty or "action" not in result.columns:
        lines.extend(["", "오늘의 US_BUY_CANDIDATE가 없습니다.", "오늘의 US_ENTRY_WATCH가 없습니다.", "오늘의 US_OFF_WATCH가 없습니다."])
        return "\n".join(lines)

    sections = [
        ("US_BUY_CANDIDATE", "[US_BUY_CANDIDATE · 돌파/거래량 확인]"),
        ("US_ENTRY_WATCH", "[US_ENTRY_WATCH · 진입 확인 대기]"),
        ("US_OFF_WATCH", "[US_OFF_WATCH · QQQ OFF · 관찰 전용]"),
    ]
    for action, title in sections:
        selected = result[result["action"] == action].head(top_n)
        lines.append("")
        if selected.empty:
            lines.append(f"오늘의 {action}가 없습니다.")
            continue
        lines.append(title)
        for row in selected.itertuples(index=False):
            lines.append(
                f"• {row.ticker} {row.name} | ${row.close:,.2f} | {row.score:.1f}점 | "
                f"20일 {row.ret_20:+.1%} | 거래량 {row.volume_vs20:.2f}x"
            )
    if regime.get("state") == "OFF":
        lines.append("\nQQQ 시장국면 OFF이므로 개별 종목 강세도 실제 매수 추천이 아닌 관찰 신호입니다.")
    return "\n".join(lines)


def _send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[WARN] TELEGRAM_TOKEN 또는 TELEGRAM_CHAT_ID가 없어 알림을 건너뜁니다.")
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=20,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "unknown")
        print(f"[WARN] Telegram 전송 실패({status})")
        return False


def _log_sheet(result: pd.DataFrame, asof_date: str) -> None:
    if result.empty or "action" not in result.columns:
        return
    from us_sheet_logger import log_observation, log_recommendation

    when = datetime.strptime(asof_date, "%Y-%m-%d")
    method = "us_price_factor_v1"
    for row in result[result["action"] == "US_BUY_CANDIDATE"].itertuples(index=False):
        added = log_recommendation(row.ticker, row.name, row.close, row.score, row.action, method, when)
        if not added:
            print(f"[INFO] {row.ticker} 미국추천 14일 중복 기록 생략")
    watch_actions = {"US_ENTRY_WATCH", "US_OFF_WATCH"}
    for row in result[result["action"].isin(watch_actions)].itertuples(index=False):
        log_observation(row.ticker, row.name, row.close, row.score, row.action, method, when)


def _save_reports(result: pd.DataFrame, stats: dict, output_dir: str) -> None:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    result.to_csv(target / "us_daily_screen.csv", index=False)
    payload = {"stats": stats, "rows": result.replace({np.nan: None}).to_dict(orient="records")}
    (target / "us_daily_screen.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="US Nasdaq-100 daily price-factor screener")
    parser.add_argument("--as-of-date", default="", help="YYYY-MM-DD")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--log-sheet", action="store_true")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=82.0)
    parser.add_argument("--off-watch-min-score", type=float, default=88.0)
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = USScreenConfig(
        universe_path=args.universe,
        top_n=args.top_n,
        min_score=args.min_score,
        off_watch_min_score=args.off_watch_min_score,
    )
    result, stats = run_screen(config, args.as_of_date.strip() or None)
    _save_reports(result, stats, config.output_dir)
    message = build_message(result, stats, config.top_n)
    print(message)
    if args.log_sheet:
        _log_sheet(result, stats["asof_date"])
    if args.notify:
        _send_telegram(message)


if __name__ == "__main__":
    main()
