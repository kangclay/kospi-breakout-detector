import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pykrx import stock
import matplotlib.pyplot as plt
import requests
from bs4 import BeautifulSoup
from sheet_logger import log_selection            # 🆕 시트 기록 헬퍼

# ────────────────────────────────────────────────
# 분석 기간 설정
end_date   = datetime.today()
start_date = end_date - timedelta(days=180)
start, end = start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d")

tickers = stock.get_market_ticker_list(market="KOSPI")

# ────────────────────────────────────────────────
def calculate_macd(df):
    exp12 = df["종가"].ewm(span=12, adjust=False).mean()
    exp26 = df["종가"].ewm(span=26, adjust=False).mean()
    macd = exp12 - exp26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal

def detect_macd_golden_cross(df):
    macd, signal = calculate_macd(df)
    return ((macd.shift(1) < signal.shift(1)) & (macd > signal) & (macd > 0)).fillna(False)

def detect_ma_golden_cross(df, short_window, long_window):
    short_ma = df["종가"].rolling(window=short_window).mean()
    long_ma = df["종가"].rolling(window=long_window).mean()
    return ((short_ma.shift(1) < long_ma.shift(1)) & (short_ma > long_ma)).fillna(False)

def detect_volume_surge(df):
    avg_volume = df["거래량"].rolling(window=20).mean()
    return df["거래량"].iloc[-1] > 1.5 * avg_volume.iloc[-2]

# (plot_stock_chart, get_recent_news 그대로…)

# ────────────────────────────────────────────────
signals = []

for ticker in tickers:
    try:
        df = stock.get_market_ohlcv_by_date(start, end, ticker)
        if df is None or len(df) < 60:
            continue

        macd_cross = detect_macd_golden_cross(df).iloc[-5:].any()
        ma_cross_1 = detect_ma_golden_cross(df, 5, 20).iloc[-5:].any()
        ma_cross_2 = detect_ma_golden_cross(df, 20, 60).iloc[-5:].any()
        vol_surge  = detect_volume_surge(df)

        if macd_cross and (ma_cross_1 or ma_cross_2) and vol_surge:
            close_price = df["종가"].iloc[-1]

            name = stock.get_market_ticker_name(ticker)

            # Google Sheets 기록 (Date | Ticker | Name | ClosePrice | Method)
            try:
                log_selection(
                    ticker=ticker,                # 두 번째 열: 티커 그대로
                    name=name,
                    close_price=close_price,
                    method="MACD+MA+VOL",       # 방법 구분 태그
                    when=df.index[-1].to_pydatetime(),
                )
            except Exception as e:
                print(f"[{ticker}] 시트 기록 실패: {e}")

            # ↓ 이하 종전 로직(표·그래프·뉴스)용 리스트
            signals.append({
                "종목명":           name,
                "티커":             ticker,
                "MACD골든크로스":   macd_cross,
                "이평선골든크로스": ma_cross_1 or ma_cross_2,
                "거래량급증":       vol_surge
            })
    except Exception as e:
        print(f"[{ticker}] 데이터 오류: {e}")
        continue

# 결과 출력·시각화 부분은 그대로 유지
signal_df = pd.DataFrame(signals)
print(signal_df)

if not signal_df.empty:
    ex    = signal_df.iloc[0]
    df_ex = stock.get_market_ohlcv_by_date(start, end, ex["티커"])
    plot_stock_chart(df_ex, ex["티커"], ex["종목명"])
    get_recent_news(ex["종목명"])
else:
    print("시그널 감지된 종목이 없습니다.")
