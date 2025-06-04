

from pykrx import stock
import pandas as pd
import datetime
import requests
import os

def send_telegram(message):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("텔레그램 토큰 또는 챗 ID가 없습니다.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"텔레그램 전송 오류: {e}")

def is_breakout(df):
    if df is None or df.empty or len(df) < 21:
        return False
    today = df.iloc[-1]
    yesterday = df.iloc[-2]
    recent_max = df['고가'][:-1].rolling(window=20).max().iloc[-1]
    return today['고가'] > recent_max and today['종가'] > yesterday['종가']

def main():
    today = datetime.datetime.today().strftime('%Y%m%d')
    tickers = stock.get_market_ticker_list(today, market="KOSPI")

    breakout_stocks = []

    for ticker in tickers[:10]:
        try:
            time.sleep(0.5)
            df = stock.get_market_ohlcv_by_date("20220101", today, ticker)
            if df is None or df.empty:
                continue
            df.reset_index(inplace=True)
            df = df.rename(columns={"시가": "Open", "고가": "High", "저가": "Low", "종가": "Close", "거래량": "Volume"})
            if is_breakout(df):
                name = stock.get_market_ticker_name(ticker)
                breakout_stocks.append(f"{name} ({ticker})")
        except Exception as e:
            print(f"[{ticker}] 오류 발생: {e}")

    if breakout_stocks:
        msg = "[📈 돌파 종목 알림]\n\n" + "\n".join(breakout_stocks)
        send_telegram(msg)
    else:
        send_telegram("📉 오늘은 돌파 종목이 없습니다.")

if __name__ == "__main__":
    main()
