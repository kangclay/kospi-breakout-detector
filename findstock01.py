import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pykrx import stock
import matplotlib.pyplot as plt
import requests
from bs4 import BeautifulSoup

# 분석 기간 설정
end_date = datetime.today()
start_date = end_date - timedelta(days=180)

start = start_date.strftime("%Y%m%d")
end = end_date.strftime("%Y%m%d")

# 코스피 종목 전체
tickers = stock.get_market_ticker_list(market="KOSPI")

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

def plot_stock_chart(df, ticker, name):
    df["20MA"] = df["종가"].rolling(window=20).mean()
    df["60MA"] = df["종가"].rolling(window=60).mean()
    macd, signal = calculate_macd(df)

    fig, ax1 = plt.subplots(figsize=(12, 6))
    ax1.set_title(f"{name} ({ticker})", fontsize=14)
    ax1.plot(df.index, df["종가"], label="종가", color='black')
    ax1.plot(df.index, df["20MA"], label="20일선", linestyle="--")
    ax1.plot(df.index, df["60MA"], label="60일선", linestyle="--")
    ax1.set_ylabel("가격")
    ax1.legend(loc="upper left")

    ax2 = ax1.twinx()
    ax2.bar(df.index, df["거래량"], color='gray', alpha=0.3, label="거래량")
    ax2.set_ylabel("거래량")

    plt.tight_layout()
    plt.show()

def get_recent_news(query):
    url = f"https://finance.naver.com/news/news_search.naver?rcdate=&q={query}&x=0&y=0&sm=title.basic&pd=3"
    headers = {'User-Agent': 'Mozilla/5.0'}
    res = requests.get(url, headers=headers)
    soup = BeautifulSoup(res.content, 'html.parser')
    news_list = soup.select(".newsList li")

    print(f"\n📰 {query} 관련 최근 뉴스")
    for news in news_list[:5]:
        title = news.select_one('a').text.strip()
        link = "https://finance.naver.com" + news.select_one('a')['href']
        print(f"- {title}\n  ↳ {link}")

signals = []

for ticker in tickers:
    try:
        df = stock.get_market_ohlcv_by_date(start, end, ticker)
        if df is None or len(df) < 60:
            continue

        macd_cross = detect_macd_golden_cross(df).iloc[-5:].any()
        ma_cross_1 = detect_ma_golden_cross(df, 5, 20).iloc[-5:].any()
        ma_cross_2 = detect_ma_golden_cross(df, 20, 60).iloc[-5:].any()
        vol_surge = detect_volume_surge(df)

        if macd_cross and (ma_cross_1 or ma_cross_2) and vol_surge:
            name = stock.get_market_ticker_name(ticker)
            signals.append({
                "종목명": name,
                "티커": ticker,
                "MACD골든크로스": macd_cross,
                "이평선골든크로스": ma_cross_1 or ma_cross_2,
                "거래량급증": vol_surge
            })
    except:
        continue

# 결과 출력
signal_df = pd.DataFrame(signals)
print(signal_df)

if not signal_df.empty:
    ex = signal_df.iloc[0]
    df_ex = stock.get_market_ohlcv_by_date(start, end, ex['티커'])
    plot_stock_chart(df_ex, ex['티커'], ex['종목명'])
    get_recent_news(ex['종목명'])
else:
    print("시그널 감지된 종목이 없습니다.")