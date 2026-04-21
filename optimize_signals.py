import argparse
import json
from itertools import product
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from pykrx import stock

from strategy_engine import ENTRY_PRESET_CHOICES, StrategyConfig, evaluate_strategy_on_dataset, summarize_evaluations


def _parse_csv_list(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_float_list(raw: str) -> List[float]:
    return [float(item.strip()) for item in raw.split(",") if item.strip()]


def _parse_int_list(raw: str) -> List[int]:
    return [int(item.strip()) for item in raw.split(",") if item.strip()]


def _parse_bool_list(raw: str) -> List[bool]:
    mapper = {"1": True, "true": True, "yes": True, "0": False, "false": False, "no": False}
    values: List[bool] = []
    for item in _parse_csv_list(raw):
        key = item.lower()
        if key not in mapper:
            raise ValueError(f"Unsupported boolean value: {item}")
        values.append(mapper[key])
    return values


def _load_tickers_from_csv(csv_path: str) -> List[str]:
    path = Path(csv_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path

    if not path.exists():
        return []

    df = pd.read_csv(path, dtype=str)
    if df.empty:
        return []

    candidate_cols = ["ticker", "Ticker", "종목코드", "code", "Code"]
    col = next((name for name in candidate_cols if name in df.columns), df.columns[0])
    series = df[col].astype(str).str.strip()
    series = series[series != ""]
    series = series.str.replace(r"\.0$", "", regex=True)
    series = series.apply(lambda x: x.zfill(6) if x.isdigit() else x)
    series = series[series.str.fullmatch(r"\d{6}")]
    return list(dict.fromkeys(series.tolist()))


def resolve_ticker_list(
    market: str,
    limit: int,
    as_of_date: Optional[str] = None,
    csv_path: str = "kospi_tickers.csv",
) -> tuple[List[str], str]:
    candidate_dates: List[str] = []
    if as_of_date:
        candidate_dates.append(as_of_date)
    else:
        base = pd.Timestamp.today().normalize()
        candidate_dates.extend([(base - pd.Timedelta(days=offset)).strftime("%Y%m%d") for offset in range(0, 15)])

    for date_str in candidate_dates:
        try:
            tickers = stock.get_market_ticker_list(date_str, market=market)
            if tickers:
                print(f"[ticker-loader] source=pykrx market={market} date={date_str} count={len(tickers)}")
                return (tickers[:limit] if limit else tickers, date_str)
            print(f"[ticker-loader] source=pykrx market={market} date={date_str} count=0")
        except Exception as exc:
            print(f"[ticker-loader] source=pykrx market={market} date={date_str} error={exc}")

    tickers = _load_tickers_from_csv(csv_path)
    if tickers:
        print(f"[ticker-loader] source=csv path={csv_path} count={len(tickers)}")
        return (tickers[:limit] if limit else tickers, candidate_dates[-1] if candidate_dates else "")
    return [], candidate_dates[-1] if candidate_dates else ""


def load_market_dataset(
    market: str,
    days: int,
    limit: int,
    as_of_date: Optional[str] = None,
    csv_path: str = "kospi_tickers.csv",
) -> Dict[str, pd.DataFrame]:
    tickers, resolved_date = resolve_ticker_list(market=market, limit=limit, as_of_date=as_of_date, csv_path=csv_path)
    if not tickers:
        return {}

    end_date = resolved_date or pd.Timestamp.today().strftime("%Y%m%d")
    start_date = (pd.Timestamp(end_date) - pd.Timedelta(days=days)).strftime("%Y%m%d")
    if limit:
        tickers = tickers[:limit]
    dataset: Dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            df = stock.get_market_ohlcv_by_date(
                start_date,
                end_date,
                ticker,
            )
            if df is None or df.empty:
                continue
            dataset[ticker] = df
        except Exception as exc:
            print(f"[WARN] {ticker}: {exc}")
    return dataset


def _search_space(args: argparse.Namespace):
    entry_sets = _parse_csv_list(args.entry_sets)
    invalid = [item for item in entry_sets if item not in ENTRY_PRESET_CHOICES]
    if invalid:
        raise ValueError(f"Unsupported presets: {', '.join(invalid)}")
    for entry_set, stop_pct, max_hold, vol_mult, entry, macd_zero_filter in product(
        entry_sets,
        _parse_float_list(args.stop_pcts),
        _parse_int_list(args.max_holds),
        _parse_float_list(args.vol_mults),
        _parse_csv_list(args.entries),
        _parse_bool_list(args.macd_zero_filters),
    ):
        yield StrategyConfig(
            entry_set=entry_set,
            stop_pct=stop_pct,
            max_hold=max_hold,
            vol_mult=vol_mult,
            entry=entry,
            macd_zero_filter=macd_zero_filter,
            market=args.market,
            days=args.days,
            fee_bp=args.fee_bp,
            slip_bp=args.slip_bp,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="여러 매수 전략 조합을 백테스트해 최적 전략을 찾는다.")
    parser.add_argument("--market", default="KOSPI")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--entry-sets", default="macd_gc,macd_ma_vol,strict_union,trend_long_bull,trend_ma_union,breakout40_gc_today_vol,quant_surge_common")
    parser.add_argument("--stop-pcts", default="0.08,0.1,0.12")
    parser.add_argument("--max-holds", default="10,15,20")
    parser.add_argument("--vol-mults", default="1.5,1.8,2.0")
    parser.add_argument("--entries", default="next_open,next_close")
    parser.add_argument("--macd-zero-filters", default="true,false")
    parser.add_argument("--fee-bp", type=float, default=5.0)
    parser.add_argument("--slip-bp", type=float, default=5.0)
    parser.add_argument("--min-trades", type=int, default=8)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument("--as-of-date", default="", help="티커 조회 기준일(YYYYMMDD). 비우면 최근 15일을 역탐색")
    parser.add_argument("--tickers-csv", default="kospi_tickers.csv", help="pykrx 티커 조회 실패 시 사용할 CSV 경로")
    args = parser.parse_args()

    dataset = load_market_dataset(
        args.market,
        args.days,
        args.limit,
        as_of_date=args.as_of_date.strip() or None,
        csv_path=args.tickers_csv,
    )
    if not dataset:
        raise SystemExit("No market data loaded.")

    summaries = []
    for config in _search_space(args):
        rows = evaluate_strategy_on_dataset(dataset, config)
        summary = summarize_evaluations(rows, config)
        if summary is None or summary["trade_count"] < args.min_trades:
            continue
        summaries.append(summary)

    if not summaries:
        raise SystemExit("No strategy combination passed the filters.")

    ranking = pd.DataFrame(summaries).sort_values(
        ["score", "cumulative_return_pct", "profit_factor"],
        ascending=[False, False, False],
    ).reset_index(drop=True)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ranking_path = output_dir / "strategy_ranking.csv"
    best_path = output_dir / "best_strategy.json"

    ranking.to_csv(ranking_path, index=False, encoding="utf-8-sig")
    best_path.write_text(json.dumps(ranking.iloc[0].to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    print(ranking.head(args.top_n).to_string(index=False))
    print(f"Saved ranking to {ranking_path}")
    print(f"Saved best strategy to {best_path}")


if __name__ == "__main__":
    main()
