from pykrx import stock
import pandas as pd
import datetime, time, os, requests
from sheet_logger import log_selection          # 🆕 시트 로그 헬퍼

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
def is_breakout(df: pd.DataFrame) -> bool:
    if df is None or df.empty or len(df) < 21:
        return False
    df = df.copy()
    df["20일고가최고"] = df["High"].rolling(window=20).max()
    return df["Close"].iloc[-1] > df["20일고가최고"].iloc[-2]

def is_macd_bullish(df: pd.DataFrame) -> bool:
    short_ema = df["Close"].ewm(span=12, adjust=False).mean()
    long_ema  = df["Close"].ewm(span=26, adjust=False).mean()
    macd   = short_ema - long_ema
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd.iloc[-1] > signal.iloc[-1] and macd.iloc[-2] <= signal.iloc[-2]

# ──────────────────────────────────────────────────────────────
def main() -> None:
    today      = datetime.datetime.today()
    today_str  = today.strftime("%Y%m%d")
    tickers    = stock.get_market_ticker_list(today_str, market="KOSPI")

    breakout_list = []          # 텔레그램용 문자열 목록

    for ticker in tickers:
        try:
            time.sleep(0.5)
            df = stock.get_market_ohlcv_by_date("20220101", today_str, ticker)
            if df is None or df.empty:
                continue

            df.reset_index(inplace=True)
            df.rename(
                columns={"시가":"Open", "고가":"High", "저가":"Low",
                         "종가":"Close", "거래량":"Volume"},
                inplace=True
            )

            if is_breakout(df) and is_macd_bullish(df):
                name        = stock.get_market_ticker_name(ticker)
                close_price = df["Close"].iloc[-1]

                # ── ① 텔레그램 메시지용
                breakout_list.append(f"{name} ({ticker})")

                # ── ② Google Sheets 기록
                try:
                    log_selection(
                        ticker=ticker,
                        close_price=close_price,
                        method="Breakout+MACD",
                        when=today
                    )
                except Exception as e:
                    print(f"[{ticker}] 시트 기록 오류: {e}")

        except Exception as e:
            print(f"[{ticker}] 데이터 처리 오류: {e}")

    # ────────────────── 텔레그램 전송
    if breakout_list:
        msg = "[📈 돌파 종목 알림]\n\n" + "\n".join(breakout_list)
    else:
        msg = "📉 오늘은 돌파 종목이 없습니다."
    send_telegram(msg)

# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
