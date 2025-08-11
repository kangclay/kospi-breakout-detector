# detector.py
from pykrx import stock
import pandas as pd
import datetime, time, os, requests
from sheet_logger import log_selection  # Google Sheets 단건 기록

# ──────────────────────────────────────────────────────────────
# Preset constants for volume and ATR filters
VOL_MULT = 1.8   # 당일 ≥ VOL_MULT × 전일 기준 20일 평균거래량
ATR_LO   = 0.015 # ATR/Close 하한(1.5%)
ATR_HI   = 0.06  # ATR/Close 상한(6%)

# ──────────────────────────────────────────────────────────────
def send_telegram(message: str) -> None:
    token   = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("텔레그램 토큰 또는 챗 ID가 없습니다.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": message},
            timeout=5
        )
    except requests.exceptions.RequestException as e:
        print(f"텔레그램 전송 오류: {e}")

# ──────────────────────────────────────────────────────────────
# 보조 지표 계산
def _calc_macd(close: pd.Series):
    exp12  = close.ewm(span=12, adjust=False).mean()
    exp26  = close.ewm(span=26, adjust=False).mean()
    macd   = exp12 - exp26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal

def _add_extras(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    # 전일 기준 20일 평균거래량 (룩어헤드 방지)
    d["vol20"] = d["Volume"].rolling(20, min_periods=20).mean()
    d["vol20_prev"] = d["vol20"].shift(1)
    # MA20/MA60
    d["ma20"] = d["Close"].rolling(20, min_periods=20).mean()
    d["ma60"] = d["Close"].rolling(60, min_periods=60).mean()
    # ATR14
    tr = pd.concat([
        (d["High"] - d["Low"]),
        (d["High"] - d["Close"].shift()).abs(),
        (d["Low"]  - d["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    d["atr14"] = tr.rolling(14, min_periods=14).mean()
    return d

def _vol_spike_today(d: pd.DataFrame, mult: float = VOL_MULT) -> bool:
    if len(d) < 21:
        return False
    x = _add_extras(d)
    vp = x["vol20_prev"].iloc[-1]
    if pd.isna(vp) or vp <= 0:
        return False
    return float(x["Volume"].iloc[-1]) >= mult * float(vp)

# ──────────────────────────────────────────────────────────────
# (업데이트) 당일 '최초' 돌파만 True, 기본 window=40
def is_breakout_today(df: pd.DataFrame, window: int = 40) -> bool:
    """
    오늘 종가가 '어제까지의 window일 최고가'를 상향 돌파하고,
    어제 종가는 '그제까지의 window일 최고가'를 돌파하지 않았을 때만 True.
    """
    if df is None or df.empty or len(df) < (window + 3):
        return False

    d = df.copy()
    roll_max = d["High"].rolling(window=window, min_periods=window).max()

    prev_max_today = roll_max.shift(1)  # 어제까지의 최대 (오늘 기준)
    prev_max_yday  = roll_max.shift(2)  # 그제까지의 최대 (어제 기준)

    # NaN 방지
    if pd.isna(prev_max_today.iloc[-1]) or pd.isna(prev_max_yday.iloc[-1]):
        return False

    close_today = float(d["Close"].iloc[-1])
    close_yday  = float(d["Close"].iloc[-2])

    # 오늘은 돌파 + 어제는 비돌파 → '당일 최초'
    cond_today = close_today > float(prev_max_today.iloc[-1])
    cond_yday  = close_yday  <= float(prev_max_yday.iloc[-1])

    return bool(cond_today and cond_yday)

# 당일 MACD 골든크로스만 True (백테스트 엔트리 기준)
def is_macd_golden_cross_today(df: pd.DataFrame) -> bool:
    if df is None or df.empty or len(df) < 35:
        return False
    short_ema = df["Close"].ewm(span=12, adjust=False).mean()
    long_ema  = df["Close"].ewm(span=26, adjust=False).mean()
    macd   = short_ema - long_ema
    signal = macd.ewm(span=9, adjust=False).mean()
    # 어제는 교차 전(<=), 오늘은 교차 후(>) → '당일 최초 발생'
    return (macd.iloc[-2] <= signal.iloc[-2]) and (macd.iloc[-1] > signal.iloc[-1])

# 추세추종 프리셋: MA20>MA60 & ATR/Close in [1.5%, 6%] + 거래량 급등
def is_ma2060_atr_with_volume(df: pd.DataFrame,
                              vol_mult: float = VOL_MULT,
                              atr_lo: float = ATR_LO,
                              atr_hi: float = ATR_HI) -> bool:
    if df is None or df.empty or len(df) < 60:
        return False
    x = _add_extras(df)
    # 거래량 급등 공통 필터
    if not _vol_spike_today(df, mult=vol_mult):
        return False
    # MA20 > MA60 (추세)
    ma20 = x["ma20"].iloc[-1]
    ma60 = x["ma60"].iloc[-1]
    if pd.isna(ma20) or pd.isna(ma60) or not (ma20 > ma60):
        return False
    # ATR 비율 범위
    atr = x["atr14"].iloc[-1]
    close = float(x["Close"].iloc[-1])
    if pd.isna(atr) or close <= 0:
        return False
    atr_ratio = float(atr) / close
    return (atr_lo <= atr_ratio <= atr_hi)

# 백테스트 엔트리: 오늘 GC 확인 → 내일 시가 진입 가정
def is_backtest_entry_today(df: pd.DataFrame) -> bool:
    return is_macd_golden_cross_today(df)

# ──────────────────────────────────────────────────────────────
def main() -> None:
    today      = datetime.datetime.today()
    today_str  = today.strftime("%Y%m%d")
    tickers    = stock.get_market_ticker_list(today_str, market="KOSPI")

    breakout_list = []  # [📈 40일 돌파 + MACD (당일 최초)]
    trend_list = []   # [📈 ma2060_atr + VOL spike]

    for ticker in tickers:
        try:
            time.sleep(0.5)  # API 과호출 방지
            df = stock.get_market_ohlcv_by_date("20220101", today_str, ticker)
            if df is None or df.empty:
                continue

            # 컬럼명 정규화
            df = df.reset_index().rename(
                columns={"시가":"Open", "고가":"High", "저가":"Low",
                         "종가":"Close", "거래량":"Volume"}
            )

            name        = stock.get_market_ticker_name(ticker)
            close_price = float(df["Close"].iloc[-1])

            # ── 1) 당일 '최초' 40일 돌파 + 당일 MACD GC 동시 충족만 채택
            if is_breakout_today(df, window=40) and is_macd_golden_cross_today(df):
                breakout_list.append(f"{name} ({ticker})")
                try:
                    log_selection(
                        ticker=ticker,
                        close_price=close_price,
                        method="Breakout40(FirstToday)+MACD_GC(Today)",
                        when=today
                    )
                except Exception as e:
                    print(f"[{ticker}] 시트 기록 오류: {e}")

            # ── 2) 추세추종: MA20>MA60 & ATR/Close in [1.5%,6%] + 거래량 급등
            if is_ma2060_atr_with_volume(df, vol_mult=VOL_MULT, atr_lo=ATR_LO, atr_hi=ATR_HI):
                trend_list.append(f"{name} ({ticker})")
                try:
                    log_selection(
                        ticker=ticker,
                        close_price=close_price,
                        method=f"ma2060_atr+VOL≥{VOL_MULT}x",
                        when=today
                    )
                except Exception as e:
                    print(f"[{ticker}] 시트 기록 오류: {e}")

        except Exception as e:
            print(f"[{ticker}] 데이터 처리 오류: {e}")

    # ────────────────── 텔레그램 전송
    lines = []
    if breakout_list:
        lines.append("[📈 40일 돌파 + MACD (당일 최초)]")
        lines.extend(breakout_list)
        lines.append("")

    if trend_list:
        lines.append(f"[📈 Trend: MA20>MA60 & ATR/Close∈[1.5%,6%] + VOL≥{VOL_MULT}x]")
        lines.extend(trend_list)

    if not lines:
        lines = ["📉 오늘은 '당일 최초' 신호가 없습니다."]

    send_telegram("\n".join(lines))

# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()