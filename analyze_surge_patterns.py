import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from pykrx import stock

from optimize_signals import load_market_dataset
from strategy_engine import (
    build_quant_surge_strategy,
    detect_live_signals,
    evaluate_strategy_on_dataset,
    prepare_signal_df,
    summarize_evaluations,
)


FEATURE_COLUMNS = [
    "near_high95",
    "breakout40",
    "ma20_gt_60",
    "ma5_gt_20_gt_60",
    "macd_bull",
    "vol_1_2",
    "vol_1_5",
    "body_pos_2",
    "close_upper70",
    "atr_ok",
    "rsi_55_75",
]


def build_feature_frame(dataset: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for ticker, raw in dataset.items():
        df = prepare_signal_df(raw)
        future_max_10 = df["Close"].shift(-1).rolling(10).max().shift(-9)
        future_ret_10 = (future_max_10 / df["Close"]) - 1.0
        future_max_5 = df["Close"].shift(-1).rolling(5).max().shift(-4)
        future_ret_5 = (future_max_5 / df["Close"]) - 1.0
        body_pct = (df["Close"] / df["Open"]) - 1.0
        close_pos = (df["Close"] - df["Low"]) / (df["High"] - df["Low"]).replace(0, pd.NA)

        rows.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "future_ret_5": future_ret_5,
                    "future_ret_10": future_ret_10,
                    "near_high95": df["Close"] >= 0.95 * df["high40_prev"],
                    "breakout40": df["Close"] > df["high40_prev"],
                    "ma20_gt_60": df["ma20"] > df["ma60"],
                    "ma5_gt_20_gt_60": (df["ma5"] > df["ma20"]) & (df["ma20"] > df["ma60"]),
                    "macd_bull": (df["macd"] > df["signal"]) & (df["signal"] > 0),
                    "vol_1_2": df["Volume"] >= 1.2 * df["vol20_prev"],
                    "vol_1_5": df["Volume"] >= 1.5 * df["vol20_prev"],
                    "body_pos_2": body_pct >= 0.02,
                    "close_upper70": close_pos >= 0.7,
                    "atr_ok": ((df["atr14"] / df["Close"]) >= 0.01) & ((df["atr14"] / df["Close"]) <= 0.08),
                    "rsi_55_75": (df["rsi14"] >= 55) & (df["rsi14"] <= 75),
                    "valid": df["high40_prev"].notna()
                    & df["ma60"].notna()
                    & df["vol20_prev"].notna()
                    & df["atr14"].notna(),
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def summarize_feature_lift(frame: pd.DataFrame, target_col: str) -> list[dict]:
    valid = frame[frame["valid"]].copy()
    positive = valid[valid[target_col]]
    negative = valid[~valid[target_col]]
    summary = []
    for column in FEATURE_COLUMNS:
        pos_rate = float(positive[column].mean() * 100.0) if len(positive) else 0.0
        neg_rate = float(negative[column].mean() * 100.0) if len(negative) else 0.0
        lift = (pos_rate + 1e-9) / (neg_rate + 1e-9)
        summary.append(
            {
                "feature": column,
                "pos_rate_pct": round(pos_rate, 2),
                "neg_rate_pct": round(neg_rate, 2),
                "lift": round(lift, 3),
            }
        )
    return summary


def validate_quant_strategy(dataset: dict[str, pd.DataFrame]) -> dict:
    strategy = build_quant_surge_strategy()
    rows = evaluate_strategy_on_dataset(dataset, strategy)
    summary = summarize_evaluations(rows, strategy)
    live_candidates = []
    for ticker, raw in dataset.items():
        if detect_live_signals({ticker: raw}, strategy):
            try:
                name = stock.get_market_ticker_name(ticker)
            except Exception:
                name = ticker
            live_candidates.append(name)
    return {
        "strategy": strategy.__dict__,
        "backtest": summary,
        "live_candidates": live_candidates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="급등 사례 공통점과 quant surge 전략을 분석한다.")
    parser.add_argument("--market", default="KOSPI")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--as-of-date", default="")
    parser.add_argument("--tickers-csv", default="kospi_tickers.csv")
    parser.add_argument("--output", default="reports/quant_surge_analysis.json")
    args = parser.parse_args()

    dataset = load_market_dataset(
        market=args.market,
        days=args.days,
        limit=args.limit,
        as_of_date=args.as_of_date.strip() or None,
        csv_path=args.tickers_csv,
    )
    if not dataset:
        raise SystemExit("No market data loaded.")

    frame = build_feature_frame(dataset)
    valid = frame[frame["valid"]].copy()
    valid["surge_5_8"] = valid["future_ret_5"] >= 0.08
    valid["surge_10_12"] = valid["future_ret_10"] >= 0.12
    valid["surge_10_15"] = valid["future_ret_10"] >= 0.15

    result = {
        "sample_count": int(len(valid)),
        "targets": {
            target: {
                "positive_rate_pct": round(float(valid[target].mean() * 100.0), 2),
                "feature_lift": summarize_feature_lift(valid, target),
            }
            for target in ["surge_5_8", "surge_10_12", "surge_10_15"]
        },
        "quant_surge_validation": validate_quant_strategy(dataset),
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(result["quant_surge_validation"], ensure_ascii=False, indent=2))
    print(f"Saved analysis to {output_path}")


if __name__ == "__main__":
    main()
