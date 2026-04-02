import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd


ENTRY_PRESET_CHOICES = (
    "macd_gc",
    "macd_ma_vol",
    "breakout40_slope",
    "macd_rsi65_gap3",
    "macd_nodup5_high95",
    "ma2060_atr",
    "strict_union",
    "trend_long_bull",
    "trend_ma_union",
    "trend_breakout40_macd",
    "trend_ma2060_vol",
    "trend_high95_macd",
    "gc_today_vol",
    "breakout40_gc_today_vol",
)


@dataclass
class StrategyConfig:
    entry_set: str = "macd_gc"
    stop_pct: float = 0.10
    max_hold: int = 20
    vol_mult: float = 1.5
    entry: str = "next_open"
    macd_zero_filter: bool = True
    market: str = "KOSPI"
    days: int = 365
    fee_bp: float = 5.0
    slip_bp: float = 5.0
    score: Optional[float] = None

    @classmethod
    def from_dict(cls, payload: Dict) -> "StrategyConfig":
        return cls(
            entry_set=str(payload.get("entry_set", "macd_gc")),
            stop_pct=float(payload.get("stop_pct", 0.10)),
            max_hold=int(payload.get("max_hold", 20)),
            vol_mult=float(payload.get("vol_mult", 1.5)),
            entry=str(payload.get("entry", "next_open")),
            macd_zero_filter=bool(payload.get("macd_zero_filter", True)),
            market=str(payload.get("market", "KOSPI")),
            days=int(payload.get("days", 365)),
            fee_bp=float(payload.get("fee_bp", 5.0)),
            slip_bp=float(payload.get("slip_bp", 5.0)),
            score=float(payload["score"]) if payload.get("score") is not None else None,
        )


def load_strategy_config(path: str | Path) -> StrategyConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return StrategyConfig.from_dict(payload)


def calculate_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    exp12 = close.ewm(span=fast, adjust=False).mean()
    exp26 = close.ewm(span=slow, adjust=False).mean()
    macd = exp12 - exp26
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig


def detect_macd_golden_cross(macd: pd.Series, signal: pd.Series) -> pd.Series:
    return (macd.shift(1) < signal.shift(1)) & (macd > signal)


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    renamed = df.reset_index().rename(
        columns={"시가": "Open", "고가": "High", "저가": "Low", "종가": "Close", "거래량": "Volume"}
    )
    return renamed


def _compute_extra_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["ma5"] = d["Close"].rolling(5).mean()
    d["ma20"] = d["Close"].rolling(20).mean()
    d["ma60"] = d["Close"].rolling(60).mean()
    d["vol20"] = d["Volume"].rolling(20).mean()
    d["vol20_prev"] = d["vol20"].shift(1)
    d["high40"] = d["High"].rolling(40).max()
    d["high40_prev"] = d["high40"].shift(1)

    delta = d["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    d["rsi14"] = (100 - 100 / (1 + rs)).bfill()

    tr = pd.concat(
        [
            d["High"] - d["Low"],
            (d["High"] - d["Close"].shift()).abs(),
            (d["Low"] - d["Close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    d["atr14"] = tr.rolling(14).mean()
    return d


def prepare_signal_df(raw_df: pd.DataFrame) -> pd.DataFrame:
    df = normalize_ohlcv(raw_df)
    macd, signal = calculate_macd(df["Close"])
    cross_gc = detect_macd_golden_cross(macd, signal)
    prepared = df.assign(macd=macd, signal=signal, cross_gc=cross_gc)
    return _compute_extra_indicators(prepared)


def _valid(*values: float) -> bool:
    for value in values:
        if not np.isfinite(float(value)):
            return False
    return True


def passes_entry_preset(
    preset: str,
    df: pd.DataFrame,
    signal_idx: int,
    entry_idx: int,
    vol_mult: float = 1.5,
) -> bool:
    if preset == "macd_gc":
        return True

    if preset == "macd_ma_vol":
        if not _valid(df["ma5"].iloc[signal_idx], df["ma20"].iloc[signal_idx], df["ma60"].iloc[signal_idx], df["Volume"].iloc[signal_idx], df["vol20"].iloc[signal_idx]):
            return False
        return bool(
            df["ma5"].iloc[signal_idx] > df["ma20"].iloc[signal_idx] > df["ma60"].iloc[signal_idx]
            and df["Volume"].iloc[signal_idx] >= vol_mult * df["vol20"].iloc[signal_idx]
        )

    if preset == "breakout40_slope":
        if signal_idx < 41:
            return False
        prev_high40 = df["high40_prev"].iloc[signal_idx]
        if not _valid(prev_high40, df["Close"].iloc[signal_idx], df["macd"].iloc[signal_idx], df["macd"].iloc[signal_idx - 1], df["signal"].iloc[signal_idx]):
            return False
        return bool(
            df["Close"].iloc[signal_idx] > prev_high40
            and (df["macd"].iloc[signal_idx] - df["macd"].iloc[signal_idx - 1]) > 0
            and df["macd"].iloc[signal_idx] > df["signal"].iloc[signal_idx]
        )

    if preset == "macd_rsi65_gap3":
        if not _valid(df["rsi14"].iloc[signal_idx], df["Close"].iloc[signal_idx]):
            return False
        if df["rsi14"].iloc[signal_idx] > 65:
            return False
        if entry_idx < len(df):
            prev_close = float(df["Close"].iloc[signal_idx])
            next_open = float(df["Open"].iloc[entry_idx])
            if prev_close <= 0 or (next_open / prev_close) > 1.03:
                return False
        return True

    if preset == "macd_nodup5_high95":
        if signal_idx < 41:
            return False
        if df["cross_gc"].iloc[max(0, signal_idx - 5):signal_idx].any():
            return False
        prev_high40 = df["high40_prev"].iloc[signal_idx]
        if not _valid(prev_high40, df["Close"].iloc[signal_idx]):
            return False
        return bool(df["Close"].iloc[signal_idx] >= 0.95 * prev_high40)

    if preset == "ma2060_atr":
        if signal_idx < 1:
            return False
        if not _valid(df["ma20"].iloc[signal_idx], df["ma60"].iloc[signal_idx], df["ma20"].iloc[signal_idx - 1], df["ma60"].iloc[signal_idx - 1], df["atr14"].iloc[signal_idx], df["Close"].iloc[signal_idx]):
            return False
        cross_ma = df["ma20"].iloc[signal_idx - 1] <= df["ma60"].iloc[signal_idx - 1] and df["ma20"].iloc[signal_idx] > df["ma60"].iloc[signal_idx]
        atr_ratio = float(df["atr14"].iloc[signal_idx]) / float(df["Close"].iloc[signal_idx])
        return bool(cross_ma and 0.015 <= atr_ratio <= 0.06)

    if preset == "strict_union":
        if not _valid(df["macd"].iloc[signal_idx], df["Volume"].iloc[signal_idx], df["vol20_prev"].iloc[signal_idx]):
            return False
        if df["macd"].iloc[signal_idx] <= 0 or df["Volume"].iloc[signal_idx] < vol_mult * df["vol20_prev"].iloc[signal_idx]:
            return False
        ok_a = False
        if signal_idx >= 41 and not df["cross_gc"].iloc[max(0, signal_idx - 5):signal_idx].any():
            hprev = df["high40_prev"].iloc[signal_idx]
            if _valid(hprev, df["Close"].iloc[signal_idx]):
                ok_a = bool(df["Close"].iloc[signal_idx] >= 0.95 * hprev)
        ok_b = _valid(df["rsi14"].iloc[signal_idx]) and df["rsi14"].iloc[signal_idx] <= 65
        ok_c = _valid(df["ma5"].iloc[signal_idx], df["ma20"].iloc[signal_idx], df["ma60"].iloc[signal_idx]) and (
            df["ma5"].iloc[signal_idx] > df["ma20"].iloc[signal_idx] > df["ma60"].iloc[signal_idx]
        )
        return bool(ok_a or ok_b or ok_c)

    if preset == "trend_long_bull":
        if not _valid(df["Close"].iloc[signal_idx], df["Open"].iloc[signal_idx], df["High"].iloc[signal_idx], df["Low"].iloc[signal_idx], df["vol20_prev"].iloc[signal_idx], df["Volume"].iloc[signal_idx]):
            return False
        close_i = float(df["Close"].iloc[signal_idx])
        open_i = float(df["Open"].iloc[signal_idx])
        high_i = float(df["High"].iloc[signal_idx])
        low_i = float(df["Low"].iloc[signal_idx])
        body = close_i - open_i
        rng = max(high_i - low_i, 1e-9)
        if not (body > 0 and body / rng >= 0.60):
            return False
        prev_close = float(df["Close"].shift(1).iloc[signal_idx]) if _valid(df["Close"].shift(1).iloc[signal_idx]) else np.nan
        big_move = bool(np.isfinite(prev_close) and prev_close > 0 and (body / prev_close) >= 0.03)
        if _valid(df["atr14"].iloc[signal_idx]) and body >= float(df["atr14"].iloc[signal_idx]):
            big_move = True
        if not big_move:
            return False
        if not _valid(df["ma20"].iloc[signal_idx], df["ma60"].iloc[signal_idx], df["macd"].iloc[signal_idx], df["signal"].iloc[signal_idx]):
            return False
        return bool(
            df["ma20"].iloc[signal_idx] > df["ma60"].iloc[signal_idx]
            and df["macd"].iloc[signal_idx] > df["signal"].iloc[signal_idx] > 0
            and df["Volume"].iloc[signal_idx] >= vol_mult * df["vol20_prev"].iloc[signal_idx]
        )

    if preset == "trend_ma_union":
        if signal_idx < 1:
            return False
        required = [
            df["vol20_prev"].iloc[signal_idx],
            df["Volume"].iloc[signal_idx],
            df["ma5"].iloc[signal_idx],
            df["ma20"].iloc[signal_idx],
            df["ma60"].iloc[signal_idx],
            df["ma20"].iloc[signal_idx - 1],
            df["ma60"].iloc[signal_idx - 1],
            df["macd"].iloc[signal_idx],
            df["signal"].iloc[signal_idx],
        ]
        if not _valid(*required):
            return False
        return bool(
            df["Volume"].iloc[signal_idx] >= vol_mult * df["vol20_prev"].iloc[signal_idx]
            and df["ma5"].iloc[signal_idx] > df["ma20"].iloc[signal_idx] > df["ma60"].iloc[signal_idx]
            and df["ma20"].iloc[signal_idx] > df["ma20"].iloc[signal_idx - 1]
            and df["ma60"].iloc[signal_idx] > df["ma60"].iloc[signal_idx - 1]
            and df["macd"].iloc[signal_idx] > df["signal"].iloc[signal_idx] > 0
        )

    if preset == "trend_breakout40_macd":
        if signal_idx < 1:
            return False
        if not _valid(df["vol20_prev"].iloc[signal_idx], df["Volume"].iloc[signal_idx], df["high40_prev"].iloc[signal_idx], df["Close"].iloc[signal_idx], df["macd"].iloc[signal_idx], df["macd"].iloc[signal_idx - 1], df["signal"].iloc[signal_idx]):
            return False
        return bool(
            df["Volume"].iloc[signal_idx] >= vol_mult * df["vol20_prev"].iloc[signal_idx]
            and df["Close"].iloc[signal_idx] > df["high40_prev"].iloc[signal_idx]
            and (df["macd"].iloc[signal_idx] - df["macd"].iloc[signal_idx - 1]) > 0
            and df["macd"].iloc[signal_idx] > df["signal"].iloc[signal_idx]
        )

    if preset == "trend_ma2060_vol":
        if signal_idx < 2:
            return False
        required = [
            df["vol20_prev"].iloc[signal_idx],
            df["Volume"].iloc[signal_idx],
            df["ma20"].iloc[signal_idx],
            df["ma60"].iloc[signal_idx],
            df["ma20"].iloc[signal_idx - 1],
            df["ma60"].iloc[signal_idx - 1],
            df["ma20"].iloc[signal_idx - 2],
            df["ma60"].iloc[signal_idx - 2],
        ]
        if not _valid(*required):
            return False
        return bool(
            df["Volume"].iloc[signal_idx] >= vol_mult * df["vol20_prev"].iloc[signal_idx]
            and df["ma20"].iloc[signal_idx] > df["ma60"].iloc[signal_idx]
            and df["ma20"].iloc[signal_idx - 1] > df["ma60"].iloc[signal_idx - 1]
            and df["ma20"].iloc[signal_idx - 2] > df["ma60"].iloc[signal_idx - 2]
        )

    if preset == "trend_high95_macd":
        if not _valid(df["vol20_prev"].iloc[signal_idx], df["Volume"].iloc[signal_idx], df["high40_prev"].iloc[signal_idx], df["Close"].iloc[signal_idx], df["macd"].iloc[signal_idx], df["signal"].iloc[signal_idx]):
            return False
        return bool(
            df["Volume"].iloc[signal_idx] >= vol_mult * df["vol20_prev"].iloc[signal_idx]
            and df["Close"].iloc[signal_idx] >= 0.95 * df["high40_prev"].iloc[signal_idx]
            and df["macd"].iloc[signal_idx] > 0
            and df["signal"].iloc[signal_idx] > 0
        )

    if preset == "gc_today_vol":
        return bool(
            _valid(df["vol20_prev"].iloc[signal_idx], df["Volume"].iloc[signal_idx])
            and df["Volume"].iloc[signal_idx] >= vol_mult * df["vol20_prev"].iloc[signal_idx]
        )

    if preset == "breakout40_gc_today_vol":
        if signal_idx < 41:
            return False
        if not _valid(df["vol20_prev"].iloc[signal_idx], df["Volume"].iloc[signal_idx], df["high40_prev"].iloc[signal_idx], df["high40"].shift(2).iloc[signal_idx], df["Close"].iloc[signal_idx], df["Close"].iloc[signal_idx - 1]):
            return False
        return bool(
            df["Volume"].iloc[signal_idx] >= vol_mult * df["vol20_prev"].iloc[signal_idx]
            and df["Close"].iloc[signal_idx] > df["high40_prev"].iloc[signal_idx]
            and df["Close"].iloc[signal_idx - 1] <= df["high40"].shift(2).iloc[signal_idx]
        )

    return False


def simulate_trade(
    df: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    stop_pct: float,
    max_hold: int,
    fee_bp: float,
    slip_bp: float,
) -> tuple[float, int, str]:
    buy = float(entry_price)
    peak = float(df["High"].iloc[entry_idx])
    last_idx = min(len(df) - 1, entry_idx + max_hold)
    exit_price = None
    exit_idx = entry_idx
    reason = "max_hold"

    for j in range(entry_idx, last_idx + 1):
        peak = max(peak, float(df["High"].iloc[j]))
        stop_level = peak * (1 - stop_pct)
        if float(df["Low"].iloc[j]) <= stop_level:
            open_j = float(df["Open"].iloc[j])
            exit_price = open_j if open_j <= stop_level else stop_level
            exit_idx = j
            reason = "trailing_stop"
            break
        if j > entry_idx and df["macd"].iloc[j] < df["signal"].iloc[j]:
            exit_price = float(df["Close"].iloc[j])
            exit_idx = j
            reason = "dead_cross"
            break

    if exit_price is None:
        exit_idx = last_idx
        exit_price = float(df["Close"].iloc[exit_idx])

    cost = (fee_bp + slip_bp) / 10000.0
    ret = ((exit_price - buy) / buy) - cost
    return ret, exit_idx, reason


def evaluate_strategy_on_dataset(
    dataset: Dict[str, pd.DataFrame],
    config: StrategyConfig,
) -> List[dict]:
    rows: List[dict] = []
    for ticker, raw_df in dataset.items():
        if raw_df is None or raw_df.empty or len(raw_df) < max(35, config.max_hold + 2):
            continue
        df = prepare_signal_df(raw_df)
        returns: List[float] = []
        reasons = {"trailing_stop": 0, "dead_cross": 0, "max_hold": 0}
        for i in range(len(df) - 2):
            if not bool(df["cross_gc"].iloc[i]):
                continue
            if config.macd_zero_filter and float(df["macd"].iloc[i]) <= 0:
                continue
            entry_idx = i + 1
            if not passes_entry_preset(config.entry_set, df, i, entry_idx, vol_mult=config.vol_mult):
                continue
            buy = float(df["Open"].iloc[entry_idx]) if config.entry == "next_open" else float(df["Close"].iloc[entry_idx])
            ret, _, reason = simulate_trade(df, entry_idx, buy, config.stop_pct, config.max_hold, config.fee_bp, config.slip_bp)
            returns.append(ret)
            reasons[reason] += 1
        if not returns:
            continue
        curve = pd.Series([1 + r for r in returns], dtype=float).cumprod()
        peak = curve.cummax()
        mdd = float(((curve / peak) - 1.0).min() * 100.0)
        gross_profit = float(sum(r for r in returns if r > 0))
        gross_loss = float(-sum(r for r in returns if r < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf
        rows.append(
            {
                "ticker": ticker,
                "trade_returns": returns,
                "trade_count": len(returns),
                "win_rate_pct": float(np.mean([r > 0 for r in returns]) * 100.0),
                "avg_trade_return_pct": float(np.mean(returns) * 100.0),
                "cumulative_return_pct": float((curve.iloc[-1] - 1.0) * 100.0),
                "max_drawdown_pct": mdd,
                "profit_factor": float(profit_factor) if np.isfinite(profit_factor) else None,
                "trailing_stop_count": reasons["trailing_stop"],
                "dead_cross_count": reasons["dead_cross"],
                "max_hold_count": reasons["max_hold"],
            }
        )
    return rows


def summarize_evaluations(rows: Iterable[dict], config: StrategyConfig) -> Optional[dict]:
    rows = list(rows)
    if not rows:
        return None
    all_returns: List[float] = []
    for row in rows:
        all_returns.extend(row["trade_returns"])
    if not all_returns:
        return None
    curve = pd.Series([1 + r for r in all_returns], dtype=float).cumprod()
    peak = curve.cummax()
    mdd = float(((curve / peak) - 1.0).min() * 100.0)
    gross_profit = float(sum(r for r in all_returns if r > 0))
    gross_loss = float(-sum(r for r in all_returns if r < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf
    win_rate_pct = float(np.mean([r > 0 for r in all_returns]) * 100.0)
    avg_trade_return_pct = float(np.mean(all_returns) * 100.0)
    cumulative_return_pct = float((curve.iloc[-1] - 1.0) * 100.0)
    score = cumulative_return_pct + (avg_trade_return_pct * 2.0) + (win_rate_pct * 0.15) + (min(len(all_returns), 50) * 0.1) - (abs(mdd) * 0.5)
    return {
        "market": config.market,
        "days": config.days,
        "entry_set": config.entry_set,
        "entry": config.entry,
        "macd_zero_filter": config.macd_zero_filter,
        "vol_mult": config.vol_mult,
        "stop_pct": config.stop_pct,
        "max_hold": config.max_hold,
        "fee_bp": config.fee_bp,
        "slip_bp": config.slip_bp,
        "active_tickers": len(rows),
        "trade_count": len(all_returns),
        "win_rate_pct": round(win_rate_pct, 2),
        "avg_trade_return_pct": round(avg_trade_return_pct, 2),
        "cumulative_return_pct": round(cumulative_return_pct, 2),
        "max_drawdown_pct": round(mdd, 2),
        "profit_factor": round(float(profit_factor), 3) if np.isfinite(profit_factor) else None,
        "trailing_stop_count": int(sum(row["trailing_stop_count"] for row in rows)),
        "dead_cross_count": int(sum(row["dead_cross_count"] for row in rows)),
        "max_hold_count": int(sum(row["max_hold_count"] for row in rows)),
        "score": round(score, 2),
    }


def detect_live_signals(dataset: Dict[str, pd.DataFrame], config: StrategyConfig) -> List[dict]:
    signals: List[dict] = []
    for ticker, raw_df in dataset.items():
        if raw_df is None or raw_df.empty or len(raw_df) < max(35, config.max_hold + 2):
            continue
        df = prepare_signal_df(raw_df)
        i = len(df) - 1
        if not bool(df["cross_gc"].iloc[i]):
            continue
        if config.macd_zero_filter and float(df["macd"].iloc[i]) <= 0:
            continue
        if not passes_entry_preset(config.entry_set, df, i, i + 1, vol_mult=config.vol_mult):
            continue
        signals.append(
            {
                "ticker": ticker,
                "date": df["날짜"].iloc[i].strftime("%Y-%m-%d") if "날짜" in df.columns else str(df.index[i]),
                "close": float(df["Close"].iloc[i]),
            }
        )
    return signals
