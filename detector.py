from pykrx import stock
import pandas as pd
import datetime
import time
import os
import requests
from zoneinfo import ZoneInfo

from sheet_logger import log_selection  # Google Sheets 단건 기록

# ──────────────────────────────────────────────────────────────
# Preset constants
VOL_MULT = 1.8   # 당일 ≥ VOL_MULT × 전일 기준 20일 평균거래량
ATR_LO   = 0.015 # ATR/Close 하한(1.5%)
ATR_HI   = 0.06  # ATR/Close 상한(6%)

KST = ZoneInfo("Asia/Seoul")
MAX_TELEGRAM_LEN = 3500  # 텔레그램 4096 제한보다 약간 보수적으로 사용


# ──────────────────────────────────────────────────────────────
def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN 또는 TELEGRAM_CHAT_ID가 비어 있습니다.")

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # 메시지가 길면 분할 전송
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
        candidate = ("\n".join(current + [line])).strip()
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
# 보조 지표 계산
def _calc_macd(close: pd.Series):
    exp12 = close.ewm(span=12, adjust=False).mean()
    exp26 = close.ewm(span=26, adjust=False).mean()
    macd = exp12 - exp26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal


def _add_extras(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()

    # 전일 기준 20일 평균거래량 (룩어헤드 방지)
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


def _vol_spike_today(df: pd.DataFrame, mult: float = VOL_MULT) -> bool:
    """
    오늘 거래량이 전일 기준 20일 평균거래량의 mult배 이상인지.
    """
    if df is None or df.empty or len(df) < 21:
        return False

    x = _add_extras(df)
    vp = x["vol20_prev"].iloc[-1]

    if pd.isna(vp) or vp <= 0:
        return False

    return float(x["Volume"].iloc[-1]) >= mult * float(vp)


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.reset_index().rename(
        columns={
            "시가": "Open",
            "고가": "High",
            "저가": "Low",
            "종가": "Close",
            "거래량": "Volume",
        }
    )

    required = ["Open", "High", "Low", "Close", "Volume"]
    for col in required:
        if col not in out.columns:
            raise ValueError(f"필수 컬럼 누락: {col}")

    return out


def _fetch_recent_ohlcv(
    ticker: str,
    start_date: str,
    end_dt: datetime.datetime,
    max_fallback_days: int = 7,
) -> pd.DataFrame:
    """
    end_dt부터 최대 max_fallback_days일 전까지 거슬러 올라가며
    가장 최근 유효한 OHLCV를 가져온다.
    """
    for delta in range(max_fallback_days + 1):
        target_dt = end_dt - datetime.timedelta(days=delta)
        target_str = target_dt.strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv_by_date(start_date, target_str, ticker)
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
# 3배 거래증가 + 3% 이상 양봉 + 6개월(120거래일) 전고점 돌파
# - 거래량: 오늘 Volume ≥ 3.0 × (전일 기준 20일 평균)
# - 캔들: (Close/Open - 1) ≥ 0.03
# - 돌파: 오늘 종가 > 어제까지의 120일 고가
def is_vol3_candle3_breakout6m(
    df: pd.DataFrame,
    vol_mult: float = 3.0,
    win: int = 120,
) -> bool:
    if df is None or df.empty or len(df) < max(20, win) + 2:
        return False

    x = _add_extras(df)

    # 거래량 급등
    vp = x["vol20_prev"].iloc[-1]
    if pd.isna(vp) or vp <= 0:
        return False
    if float(x["Volume"].iloc[-1]) < vol_mult * float(vp):
        return False

    # 3% 이상 양봉
    open_i = float(x["Open"].iloc[-1])
    close_i = float(x["Close"].iloc[-1])
    if open_i <= 0:
        return False
    if not (close_i > open_i and (close_i / open_i - 1.0) >= 0.03):
        return False

    # 120일 전고점 돌파
    high_win = x["High"].rolling(win, min_periods=win).max()
    prev_high = high_win.shift(1).iloc[-1]
    if pd.isna(prev_high):
        return False

    return bool(close_i > float(prev_high))


# ──────────────────────────────────────────────────────────────
# 오늘 '최초' 돌파만 True
def is_breakout_today(df: pd.DataFrame, window: int = 40) -> bool:
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


def is_macd_golden_cross_today(df: pd.DataFrame) -> bool:
    """
    당일 MACD 골든크로스만 True.
    """
    if df is None or df.empty or len(df) < 35:
        return False

    macd, signal = _calc_macd(df["Close"])
    return bool(
        (macd.iloc[-2] <= signal.iloc[-2]) and
        (macd.iloc[-1] > signal.iloc[-1])
    )


def is_ma2060_atr_with_volume(
    df: pd.DataFrame,
    vol_mult: float = VOL_MULT,
    atr_lo: float = ATR_LO,
    atr_hi: float = ATR_HI,
) -> bool:
    """
    추세추종 프리셋:
    - MA20 > MA60
    - ATR/Close in [atr_lo, atr_hi]
    - 거래량 급등
    """
    if df is None or df.empty or len(df) < 60:
        return False

    x = _add_extras(df)

    if not _vol_spike_today(df, mult=vol_mult):
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


def is_backtest_entry_today(df: pd.DataFrame) -> bool:
    return is_macd_golden_cross_today(df)


# ──────────────────────────────────────────────────────────────
def main() -> None:
    now_kst = datetime.datetime.now(KST)
    today_str = now_kst.strftime("%Y%m%d")

    print(f"[INFO] now_kst={now_kst.isoformat()}")
    print(f"[INFO] today_str={today_str}")

    tickers = stock.get_market_ticker_list(market="KOSPI")
    print(f"[INFO] KOSPI ticker count={len(tickers)}")

    breakout_list = []  # [📈 40일 돌파 + MACD (당일 최초)]
    trend_list = []     # [📈 ma2060_atr + VOL spike]
    power_list = []     # [⚡ VOL≥3x + Bullish≥3% + 120D Breakout]

    data_error_count = 0
    breakout_error_count = 0
    trend_error_count = 0
    power_error_count = 0

    processed = 0
    non_empty_count = 0

    for idx, ticker in enumerate(tickers, start=1):
        processed += 1

        try:
            time.sleep(0.3)  # API 과호출 방지
            df = _fetch_recent_ohlcv(
                ticker=ticker,
                start_date="20220101",
                end_dt=now_kst,
                max_fallback_days=7,
            )
            if df is None or df.empty:
                continue

            non_empty_count += 1
            name = stock.get_market_ticker_name(ticker)
            close_price = float(df["Close"].iloc[-1])

        except Exception as e:
            data_error_count += 1
            print(f"[{ticker}] 데이터 준비 오류: {e}")
            continue

        # ── 1) 40일 돌파 + 당일 MACD GC
        try:
            if is_breakout_today(df, window=40) and is_macd_golden_cross_today(df):
                breakout_list.append(f"{name} ({ticker})")
                _safe_log_selection(
                    ticker=ticker,
                    close_price=close_price,
                    method="Breakout40(FirstToday)+MACD_GC(Today)",
                    when=now_kst,
                )
        except Exception as e:
            breakout_error_count += 1
            print(f"[{ticker}] breakout 오류: {e}")

        # ── 2) 추세추종: MA20>MA60 & ATR/Close in [1.5%, 6%] + 거래량 급등
        try:
            if is_ma2060_atr_with_volume(
                df,
                vol_mult=VOL_MULT,
                atr_lo=ATR_LO,
                atr_hi=ATR_HI,
            ):
                trend_list.append(f"{name} ({ticker})")
                _safe_log_selection(
                    ticker=ticker,
                    close_price=close_price,
                    method=f"ma2060_atr+VOL≥{VOL_MULT}x",
                    when=now_kst,
                )
        except Exception as e:
            trend_error_count += 1
            print(f"[{ticker}] trend 오류: {e}")

        # ── 3) VOL×3 + 양봉3% + 120D 전고점 돌파
        try:
            if is_vol3_candle3_breakout6m(df, vol_mult=3.0, win=120):
                power_list.append(f"{name} ({ticker})")
                _safe_log_selection(
                    ticker=ticker,
                    close_price=close_price,
                    method="VOL≥3x+Bull≥3%+Breakout120(PrevHigh)",
                    when=now_kst,
                )
        except Exception as e:
            power_error_count += 1
            print(f"[{ticker}] power 오류: {e}")

        if idx % 100 == 0:
            print(
                f"[INFO] processed={idx}/{len(tickers)} "
                f"breakout={len(breakout_list)} trend={len(trend_list)} power={len(power_list)}"
            )

    # ────────────────── 텔레그램 메시지 구성
    lines = []
    lines.append(f"📌 KOSPI detector 결과 ({today_str})")
    lines.append("")

    if breakout_list:
        lines.append("[📈 40일 돌파 + MACD (당일 최초)]")
        lines.extend(breakout_list)
        lines.append("")

    if trend_list:
        lines.append(f"[📈 Trend: MA20>MA60 & ATR/Close∈[1.5%,6%] + VOL≥{VOL_MULT}x]")
        lines.extend(trend_list)
        lines.append("")

    if power_list:
        lines.append("[⚡ VOL≥3x + Bull≥3% + 120D Breakout]")
        lines.extend(power_list)
        lines.append("")

    if not breakout_list and not trend_list and not power_list:
        lines.append("📉 오늘은 추천 종목이 없습니다.")
        lines.append("")

    lines.append("[DEBUG]")
    lines.append(f"processed={processed}")
    lines.append(f"non_empty_data={non_empty_count}")
    lines.append(f"breakout={len(breakout_list)}")
    lines.append(f"trend={len(trend_list)}")
    lines.append(f"power={len(power_list)}")
    lines.append(
        f"errors(data/breakout/trend/power)="
        f"{data_error_count}/{breakout_error_count}/{trend_error_count}/{power_error_count}"
    )

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
