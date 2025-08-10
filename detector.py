# detector.py
from pykrx import stock
import pandas as pd
import datetime, time, os, requests
from sheet_logger import log_selection          # Google Sheets 헬퍼 (단건 기록)

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
# (기존) 20일 고가 돌파 + MACD 시그널
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
    # 오늘 골든크로스(오늘 macd>signal & 어제 macd<=signal)
    return macd.iloc[-1] > signal.iloc[-1] and macd.iloc[-2] <= signal.iloc[-2]

# ──────────────────────────────────────────────────────────────
# (신규) 백테스트 기준 매수 신호 체크
#  - 백테스트의 엔트리는 "신호 발생일(D) 확인 후 D+1 시가 진입(next_open)"이었음
#  - 따라서 오늘 17시 실행 시, "오늘 MACD 골든크로스 발생" 종목을 추천
def is_backtest_entry_today(df: pd.DataFrame) -> bool:
    if df is None or df.empty or len(df) < 35:
        return False
    # 백테스트에서도 MACD/Signal(12,26,9) 기반이었음
    return is_macd_bullish(df)

# ──────────────────────────────────────────────────────────────
def main() -> None:
    today      = datetime.datetime.today()
    today_str  = today.strftime("%Y%m%d")
    tickers    = stock.get_market_ticker_list(today_str, market="KOSPI")

    # 텔레그램 메시지용
    breakout_list = []             # 기존 전략: Breakout + MACD
    backtest_entry_list = []       # 신규 전략: BacktestEntry(MACD_GC,next_open)

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

            # ── 1) 기존: Breakout + MACD 동시 충족
            if is_breakout(df) and is_macd_bullish(df):
                name        = stock.get_market_ticker_name(ticker)
                close_price = float(df["Close"].iloc[-1])

                breakout_list.append(f"{name} ({ticker})")
                # 시트 기록
                try:
                    log_selection(
                        ticker=ticker,
                        close_price=close_price,
                        method="Breakout+MACD",
                        when=today
                    )
                except Exception as e:
                    print(f"[{ticker}] 시트 기록 오류: {e}")

            # ── 2) 신규: 백테스트 매수시점 기준(오늘 MACD GC → 내일 시가 진입)
            if is_backtest_entry_today(df):
                name        = stock.get_market_ticker_name(ticker)
                close_price = float(df["Close"].iloc[-1])

                backtest_entry_list.append(f"{name} ({ticker})")
                # 시트 기록 (방법 태그로 구분)
                try:
                    log_selection(
                        ticker=ticker,
                        close_price=close_price,
                        method="BacktestEntry(MACD_GC,next_open)",
                        when=today
                    )
                except Exception as e:
                    print(f"[{ticker}] 시트 기록 오류: {e}")

        except Exception as e:
            print(f"[{ticker}] 데이터 처리 오류: {e}")

    # ────────────────── 텔레그램 전송
    lines = []
    if breakout_list:
        lines.append("[📈 돌파 + MACD]")
        lines.extend(breakout_list)
        lines.append("")  # 빈 줄

    if backtest_entry_list:
        lines.append("[🧪 Backtest Entry: MACD GC → 내일 시가]")
        lines.extend(backtest_entry_list)

    if not lines:
        lines = ["📉 오늘은 추천 신호가 없습니다."]

    send_telegram("\n".join(lines))

# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()