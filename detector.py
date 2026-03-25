from pykrx import stock
import pandas as pd
import datetime
import time
import os
import requests
from zoneinfo import ZoneInfo

from sheet_logger import log_selection  # Google Sheets 단건 기록


# ──────────────────────────────────────────────────────────────
# Time / runtime
KST = ZoneInfo("Asia/Seoul")
MAX_TELEGRAM_LEN = 3500
REQUEST_SLEEP_SEC = 0.20

# 데이터 조회 범위
FETCH_LOOKBACK_DAYS = 500
FETCH_FALLBACK_DAYS = 7

# ──────────────────────────────────────────────────────────────
# Strategy presets
BREAKOUT_WIN = 40
MACD_RECENT_LOOKBACK = 5

TREND_VOL_MULT = 1.3
TREND_ATR_LO = 0.01
TREND_ATR_HI = 0.08

POWER_VOL_MULT = 2.0
POWER_CANDLE_PCT = 0.02
POWER_WIN = 60


# ──────────────────────────────────────────────────────────────
def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN 또는 TELEGRAM_CHAT_ID가 비어 있습니다.")

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    chunks = _split_message(message, MAX_TELEGRAM_LEN)
    for idx, chunk in enumerate(chunks, start=1):
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "text": chunk},
            timeout=10,
        )

        print(f"[TELEGRAM] chunk={idx}/{len(chunks)} status={resp.status_code}")
        print(f"[TELEGRAM] body={resp.text[:500]}")

        resp.raise_for_status()

        try:
            body = resp.json()
        except Exception:
            raise RuntimeError(f"텔레그램 응답 JSON 파싱 실패: {resp.text}")

        if not body.get("ok", False):
            raise RuntimeError(f"텔레그램 API 실패: {body}")


def _split_message(message: str, max_len: int) -> list[str]:
    if len(message) <= max_len:
        return [message]

    chunks = []
    current = []

    for line in message.splitlines():
        candidate = "\n".join(current + [line]).strip()
        if len(candidate) <= max_len:
            current.append(line)
        else:
            if current:
                chunks.append("\n".join(current).strip())
                current = [line]
            else:
                # 한 줄 자체가 너무 긴 경우 강제 자르기
                for i in range(0, len(line), max_len):
                    chunks.append(line[i:i + max_len])

    if current:
        chunks.append("\n".join(current).strip())

    return chunks


# ──────────────────────────────────────────────────────────────
# 지표 계산
def _calc_macd(close: pd.Series):
    exp12 = close.ewm(span=12, adjust=False).mean()
    exp26 = close.ewm(span=26, adjust=False).mean()
    macd = exp12 - exp26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal


def _add_extras(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()

    # 전일 기준 20일 평균거래량
    d["vol20"] = d["Volume"].rolling(20, min_periods=20).mean()
    d["vol20_prev"] = d["vol20"].shift(1)

    # MA20 / MA60
    d["ma20"] = d["Close"].rolling(20, min_periods=20).mean()
    d["ma60"] = d["Close"].rolling(60, min_periods=60).mean()

    # ATR14
    tr = pd.concat(
        [
            (d["High"] - d["Low"]),
            (d["High"] - d["Close"].shift()).abs(),
            (d["Low"] - d["Close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    d["atr14"] = tr.rolling(14, min_periods=14).mean()
    return d


def _with_extras(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    needed = {"vol20", "vol20_prev", "ma20", "ma60", "atr14"}
    if needed.issubset(df.columns):
        return df

    return _add_extras(df)


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.reset_index().rename(
        columns={
            "날짜": "Date",
            "시가": "Open",
            "고가": "High",
            "저가": "Low",
            "종가": "Close",
            "거래량": "Volume",
        }
    )

    # 날짜 컬럼명이 다를 수 있으니 첫 컬럼 fallback
    if "Date" not in out.columns:
        out = out.rename(columns={out.columns[0]: "Date"})

    required = ["Date", "Open", "High", "Low", "Close", "Volume"]
    for col in required:
        if col not in out.columns:
            raise ValueError(f"필수 컬럼 누락: {col}")

    out["Date"] = pd.to_datetime(out["Date"])
    return out


def _make_start_date(end_dt: datetime.datetime, lookback_days: int = FETCH_LOOKBACK_DAYS) -> str:
    return (end_dt - datetime.timedelta(days=lookback_days)).strftime("%Y%m%d")


def _fetch_recent_ohlcv(
    ticker: str,
    end_dt: datetime.datetime,
    start_date: str | None = None,
    max_fallback_days: int = FETCH_FALLBACK_DAYS,
) -> pd.DataFrame:
    """
    end_dt부터 최대 max_fallback_days일 전까지 거슬러 올라가며
    가장 최근 유효한 OHLCV를 가져온다.
    """
    base_start_date = start_date or _make_start_date(end_dt)

    for delta in range(max_fallback_days + 1):
        target_dt = end_dt - datetime.timedelta(days=delta)
        target_str = target_dt.strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv_by_date(base_start_date, target_str, ticker)
            if df is not None and not df.empty:
                return _normalize_ohlcv(df)
        except Exception as e:
            print(f"[{ticker}] OHLCV 조회 실패 ({target_str}): {e}")

    return pd.DataFrame()


def _safe_log_selection(
    ticker: str,
    close_price: float,
    method: str,
    when: datetime.datetime,
) -> None:
    try:
        log_selection(
            ticker=ticker,
            close_price=close_price,
            method=method,
            when=when,
        )
    except Exception as e:
        print(f"[{ticker}] 시트 기록 오류 ({method}): {e}")


# ──────────────────────────────────────────────────────────────
# 전략 함수
def _vol_spike_today(df: pd.DataFrame, mult: float) -> bool:
    x = _with_extras(df)
    if x is None or x.empty or len(x) < 21:
        return False

    vp = x["vol20_prev"].iloc[-1]
    if pd.isna(vp) or vp <= 0:
        return False

    return float(x["Volume"].iloc[-1]) >= mult * float(vp)


def is_breakout_today(df: pd.DataFrame, window: int = BREAKOUT_WIN) -> bool:
    """
    오늘 종가가 '어제까지의 window일 최고가'를 상향 돌파하고,
    어제 종가는 '그제까지의 window일 최고가'를 돌파하지 않았을 때만 True.
    """
    if df is None or df.empty or len(df) < (window + 3):
        return False

    d = df.copy()
    roll_max = d["High"].rolling(window=window, min_periods=window).max()

    prev_max_today = roll_max.shift(1)  # 어제까지의 최대
    prev_max_yday = roll_max.shift(2)   # 그제까지의 최대

    if pd.isna(prev_max_today.iloc[-1]) or pd.isna(prev_max_yday.iloc[-1]):
        return False

    close_today = float(d["Close"].iloc[-1])
    close_yday = float(d["Close"].iloc[-2])

    cond_today = close_today > float(prev_max_today.iloc[-1])
    cond_yday = close_yday <= float(prev_max_yday.iloc[-1])

    return bool(cond_today and cond_yday)


def is_macd_golden_cross_recent(
    df: pd.DataFrame,
    lookback: int = MACD_RECENT_LOOKBACK,
) -> bool:
    """
    최근 lookback일 이내 MACD 골든크로스가 1회라도 발생했으면 True.
    """
    if df is None or df.empty or len(df) < max(35, lookback + 2):
        return False

    macd, signal = _calc_macd(df["Close"])

    start_idx = max(1, len(df) - lookback)
    for i in range(start_idx, len(df)):
        if (macd.iloc[i - 1] <= signal.iloc[i - 1]) and (macd.iloc[i] > signal.iloc[i]):
            return True

    return False


def is_ma2060_atr_with_volume(
    df: pd.DataFrame,
    vol_mult: float = TREND_VOL_MULT,
    atr_lo: float = TREND_ATR_LO,
    atr_hi: float = TREND_ATR_HI,
) -> bool:
    """
    추세추종 프리셋:
    - MA20 > MA60
    - ATR/Close in [atr_lo, atr_hi]
    - 거래량 급등
    """
    x = _with_extras(df)
    if x is None or x.empty or len(x) < 60:
        return False

    if not _vol_spike_today(x, mult=vol_mult):
        return False

    ma20 = x["ma20"].iloc[-1]
    ma60 = x["ma60"].iloc[-1]
    if pd.isna(ma20) or pd.isna(ma60) or not (ma20 > ma60):
        return False

    atr = x["atr14"].iloc[-1]
    close = float(x["Close"].iloc[-1])
    if pd.isna(atr) or close <= 0:
        return False

    atr_ratio = float(atr) / close
    return bool(atr_lo <= atr_ratio <= atr_hi)


def is_power_breakout(
    df: pd.DataFrame,
    vol_mult: float = POWER_VOL_MULT,
    candle_pct: float = POWER_CANDLE_PCT,
    win: int = POWER_WIN,
) -> bool:
    """
    완화 버전:
    - 거래량: 오늘 Volume ≥ vol_mult × (전일 기준 20일 평균)
    - 캔들: (Close/Open - 1) ≥ candle_pct
    - 돌파: 오늘 종가 > 어제까지의 win일 고가
    """
    x = _with_extras(df)
    if x is None or x.empty or len(x) < max(21, win) + 2:
        return False

    vp = x["vol20_prev"].iloc[-1]
    if pd.isna(vp) or vp <= 0:
        return False
    if float(x["Volume"].iloc[-1]) < vol_mult * float(vp):
        return False

    open_i = float(x["Open"].iloc[-1])
    close_i = float(x["Close"].iloc[-1])
    if open_i <= 0:
        return False
    if not (close_i > open_i and (close_i / open_i - 1.0) >= candle_pct):
        return False

    prev_high = x["High"].rolling(win, min_periods=win).max().shift(1).iloc[-1]
    if pd.isna(prev_high):
        return False

    return bool(close_i > float(prev_high))


def is_backtest_entry_today(df: pd.DataFrame) -> bool:
    return is_macd_golden_cross_recent(df, lookback=MACD_RECENT_LOOKBACK)


# ──────────────────────────────────────────────────────────────
# 진단 함수
def eval_breakout_strategy(df: pd.DataFrame) -> dict:
    info = {
        "len_ok": False,
        "breakout_ok": False,
        "macd_ok": False,
        "final": False,
    }

    if df is None or df.empty or len(df) < max(BREAKOUT_WIN + 3, 35, MACD_RECENT_LOOKBACK + 2):
        return info

    info["len_ok"] = True
    info["breakout_ok"] = is_breakout_today(df, window=BREAKOUT_WIN)
    info["macd_ok"] = is_macd_golden_cross_recent(df, lookback=MACD_RECENT_LOOKBACK)
    info["final"] = info["breakout_ok"] and info["macd_ok"]
    return info


def eval_trend_strategy(df: pd.DataFrame) -> dict:
    info = {
        "len_ok": False,
        "vol_ok": False,
        "ma_ok": False,
        "atr_ok": False,
        "final": False,
    }

    x = _with_extras(df)
    if x is None or x.empty or len(x) < 60:
        return info

    info["len_ok"] = True

    if not _vol_spike_today(x, mult=TREND_VOL_MULT):
        return info
    info["vol_ok"] = True

    ma20 = x["ma20"].iloc[-1]
    ma60 = x["ma60"].iloc[-1]
    if pd.isna(ma20) or pd.isna(ma60) or not (ma20 > ma60):
        return info
    info["ma_ok"] = True

    atr = x["atr14"].iloc[-1]
    close = float(x["Close"].iloc[-1])
    if pd.isna(atr) or close <= 0:
        return info

    atr_ratio = float(atr) / close
    if not (TREND_ATR_LO <= atr_ratio <= TREND_ATR_HI):
        return info
    info["atr_ok"] = True

    info["final"] = True
    return info


def eval_power_strategy(df: pd.DataFrame) -> dict:
    info = {
        "len_ok": False,
        "vol_ok": False,
        "candle_ok": False,
        "breakout_ok": False,
        "final": False,
    }

    x = _with_extras(df)
    if x is None or x.empty or len(x) < max(21, POWER_WIN) + 2:
        return info

    info["len_ok"] = True

    vp = x["vol20_prev"].iloc[-1]
    if pd.isna(vp) or vp <= 0:
        return info
    if float(x["Volume"].iloc[-1]) < POWER_VOL_MULT * float(vp):
        return info
    info["vol_ok"] = True

    open_i = float(x["Open"].iloc[-1])
    close_i = float(x["Close"].iloc[-1])
    if open_i <= 0:
        return info
    if not (close_i > open_i and (close_i / open_i - 1.0) >= POWER_CANDLE_PCT):
        return info
    info["candle_ok"] = True

    prev_high = x["High"].rolling(POWER_WIN, min_periods=POWER_WIN).max().shift(1).iloc[-1]
    if pd.isna(prev_high):
        return info
    if not (close_i > float(prev_high)):
        return info
    info["breakout_ok"] = True

    info["final"] = True
    return info


# ──────────────────────────────────────────────────────────────
def main() -> None:
    now_kst = datetime.datetime.now(KST)
    run_date_str = now_kst.strftime("%Y%m%d")

    print(f"[INFO] now_kst={now_kst.isoformat()}")
    print(f"[INFO] run_date_str={run_date_str}")

    tickers = stock.get_market_ticker_list(market="KOSPI")
    print(f"[INFO] KOSPI ticker count={len(tickers)}")

    breakout_list = []
    trend_list = []
    power_list = []

    data_error_count = 0

    processed = 0
    non_empty_count = 0
    latest_data_date = None

    diag = {
        "breakout_len_ok": 0,
        "breakout_breakout_ok": 0,
        "breakout_macd_ok": 0,
        "breakout_final": 0,
        "trend_len_ok": 0,
        "trend_vol_ok": 0,
        "trend_ma_ok": 0,
        "trend_atr_ok": 0,
        "trend_final": 0,
        "power_len_ok": 0,
        "power_vol_ok": 0,
        "power_candle_ok": 0,
        "power_breakout_ok": 0,
        "power_final": 0,
    }

    start_date = _make_start_date(now_kst, FETCH_LOOKBACK_DAYS)

    for idx, ticker in enumerate(tickers, start=1):
        processed += 1

        try:
            time.sleep(REQUEST_SLEEP_SEC)

            df = _fetch_recent_ohlcv(
                ticker=ticker,
                end_dt=now_kst,
                start_date=start_date,
                max_fallback_days=FETCH_FALLBACK_DAYS,
            )
            if df is None or df.empty:
                continue

            non_empty_count += 1

            x = _add_extras(df)
            name = stock.get_market_ticker_name(ticker)
            close_price = float(x["Close"].iloc[-1])

            data_date = pd.to_datetime(x["Date"].iloc[-1]).to_pydatetime()
            if latest_data_date is None or data_date > latest_data_date:
                latest_data_date = data_date

        except Exception as e:
            data_error_count += 1
            print(f"[{ticker}] 데이터 준비 오류: {e}")
            continue

        # ── 1) Breakout: 40일 돌파 + 최근 5일 MACD GC
        try:
            b = eval_breakout_strategy(x)

            if b["len_ok"]:
                diag["breakout_len_ok"] += 1
            if b["breakout_ok"]:
                diag["breakout_breakout_ok"] += 1
            if b["macd_ok"]:
                diag["breakout_macd_ok"] += 1
            if b["final"]:
                diag["breakout_final"] += 1
                breakout_list.append(f"- {name} ({ticker}) | 종가 {close_price:,.0f}")
                _safe_log_selection(
                    ticker=ticker,
                    close_price=close_price,
                    method=f"Breakout{BREAKOUT_WIN}+MACD_GC(Recent{MACD_RECENT_LOOKBACK}d)",
                    when=now_kst,
                )
        except Exception as e:
            print(f"[{ticker}] breakout 오류: {e}")

        # ── 2) Trend: MA20>MA60 & ATR/Close 범위 & 거래량 급등
        try:
            t = eval_trend_strategy(x)

            if t["len_ok"]:
                diag["trend_len_ok"] += 1
            if t["vol_ok"]:
                diag["trend_vol_ok"] += 1
            if t["ma_ok"]:
                diag["trend_ma_ok"] += 1
            if t["atr_ok"]:
                diag["trend_atr_ok"] += 1
            if t["final"]:
                diag["trend_final"] += 1
                trend_list.append(f"- {name} ({ticker}) | 종가 {close_price:,.0f}")
                _safe_log_selection(
                    ticker=ticker,
                    close_price=close_price,
                    method=f"Trend(MA20>MA60,ATR={TREND_ATR_LO:.1%}~{TREND_ATR_HI:.1%},VOL≥{TREND_VOL_MULT}x)",
                    when=now_kst,
                )
        except Exception as e:
            print(f"[{ticker}] trend 오류: {e}")

        # ── 3) Power: 거래량 증가 + 양봉 + 중기 신고가 돌파
        try:
            p = eval_power_strategy(x)

            if p["len_ok"]:
                diag["power_len_ok"] += 1
            if p["vol_ok"]:
                diag["power_vol_ok"] += 1
            if p["candle_ok"]:
                diag["power_candle_ok"] += 1
            if p["breakout_ok"]:
                diag["power_breakout_ok"] += 1
            if p["final"]:
                diag["power_final"] += 1
                power_list.append(f"- {name} ({ticker}) | 종가 {close_price:,.0f}")
                _safe_log_selection(
                    ticker=ticker,
                    close_price=close_price,
                    method=f"Power(VOL≥{POWER_VOL_MULT}x,Bull≥{POWER_CANDLE_PCT:.1%},Breakout{POWER_WIN})",
                    when=now_kst,
                )
        except Exception as e:
            print(f"[{ticker}] power 오류: {e}")

        if idx % 100 == 0:
            print(
                f"[INFO] processed={idx}/{len(tickers)} "
                f"breakout={len(breakout_list)} "
                f"trend={len(trend_list)} "
                f"power={len(power_list)}"
            )

    # ────────────────── 텔레그램 메시지 구성
    asof_date_str = latest_data_date.strftime("%Y%m%d") if latest_data_date else "N/A"

    lines = []
    lines.append(f"📌 KOSPI detector 결과")
    lines.append(f"- 실행일시: {now_kst.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    lines.append(f"- 데이터 기준일: {asof_date_str}")
    lines.append("")

    if breakout_list:
        lines.append(f"[📈 Breakout: {BREAKOUT_WIN}일 돌파 + 최근 {MACD_RECENT_LOOKBACK}일 MACD GC]")
        lines.extend(sorted(breakout_list))
        lines.append("")

    if trend_list:
        lines.append(
            f"[📈 Trend: MA20>MA60 & ATR/Close∈[{TREND_ATR_LO:.1%},{TREND_ATR_HI:.1%}] + VOL≥{TREND_VOL_MULT}x]"
        )
        lines.extend(sorted(trend_list))
        lines.append("")

    if power_list:
        lines.append(
            f"[⚡ Power: VOL≥{POWER_VOL_MULT}x + Bull≥{POWER_CANDLE_PCT:.1%} + {POWER_WIN}일 Breakout]"
        )
        lines.extend(sorted(power_list))
        lines.append("")

    if not breakout_list and not trend_list and not power_list:
        lines.append("📉 오늘은 조건을 통과한 종목이 없습니다.")
        lines.append("")

    lines.append("[SUMMARY]")
    lines.append(f"processed={processed}")
    lines.append(f"non_empty_data={non_empty_count}")
    lines.append(f"breakout={len(breakout_list)}")
    lines.append(f"trend={len(trend_list)}")
    lines.append(f"power={len(power_list)}")
    lines.append(f"errors(data)={data_error_count}")
    lines.append("")

    lines.append("[DIAG - Breakout]")
    lines.append(f"len_ok={diag['breakout_len_ok']}")
    lines.append(f"breakout_ok={diag['breakout_breakout_ok']}")
    lines.append(f"macd_recent_ok={diag['breakout_macd_ok']}")
    lines.append(f"final={diag['breakout_final']}")
    lines.append("")

    lines.append("[DIAG - Trend]")
    lines.append(f"len_ok={diag['trend_len_ok']}")
    lines.append(f"vol_ok={diag['trend_vol_ok']}")
    lines.append(f"ma_ok={diag['trend_ma_ok']}")
    lines.append(f"atr_ok={diag['trend_atr_ok']}")
    lines.append(f"final={diag['trend_final']}")
    lines.append("")

    lines.append("[DIAG - Power]")
    lines.append(f"len_ok={diag['power_len_ok']}")
    lines.append(f"vol_ok={diag['power_vol_ok']}")
    lines.append(f"candle_ok={diag['power_candle_ok']}")
    lines.append(f"breakout_ok={diag['power_breakout_ok']}")
    lines.append(f"final={diag['power_final']}")

    send_telegram("\n".join(lines))


# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err_msg = f"❌ detector.py 실행 실패: {e}"
        print(err_msg)
        try:
            send_telegram(err_msg)
        except Exception as te:
            print(f"[FATAL] 텔레그램 실패: {te}")
        raise
