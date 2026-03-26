import FinanceDataReader as fdr
import pandas as pd
import requests
import os
import sys
import io
from datetime import datetime, timedelta
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[WARN] TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 없음")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        'chat_id': CHAT_ID,
        'text': message,
        'disable_web_page_preview': True
    }
    try:
        resp = requests.post(url, data=data, timeout=10)
        print(f"[TELEGRAM] status={resp.status_code}")
        time.sleep(1)
    except Exception as e:
        print(f"전송 에러: {e}")

def calculate_rsi(series, period=14):
    delta = series.diff(1)
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def analyze_market_ranking(market_name, ticker_list):
    print(f"\n[{market_name}] 랭킹 시스템 가동 중...")

    candidates = []
    stats = {
        "scanned": 0,
        "enough_data": 0,
        "trend_pass": 0,
        "green_pass": 0,
        "breakout_pass": 0,
        "volume_pass": 0,
        "errors": 0,
    }

    for _, row in ticker_list.iterrows():
        code = row['Symbol'] if 'Symbol' in row.index else row['Code']
        name = row['Name']
        stats["scanned"] += 1

        try:
            df = fdr.DataReader(code, start=datetime.now() - timedelta(days=180))
            if df is None or df.empty or len(df) < 120:
                continue
            stats["enough_data"] += 1

            df = df.copy()
            df['MA10'] = df['Close'].rolling(window=10).mean()
            df['MA20'] = df['Close'].rolling(window=20).mean()
            df['MA60'] = df['Close'].rolling(window=60).mean()
            df['RSI'] = calculate_rsi(df['Close'])
            df['VolAvg20'] = df['Volume'].rolling(window=20).mean()

            curr = df.iloc[-1]

            # 1) 정배열 + RSI
            if not (curr['MA10'] > curr['MA20'] > curr['MA60']):
                continue
            if pd.isna(curr['RSI']) or curr['RSI'] < 50:
                continue
            stats["trend_pass"] += 1

            # 2) 최근 40일 중 양봉 개수
            recent_40 = df.iloc[-40:]
            green_cnt = len(recent_40[recent_40['Close'] > recent_40['Open']])
            if green_cnt < 18:
                continue
            stats["green_pass"] += 1

            # 3) 60일 박스권 돌파
            box_range = df['High'].iloc[-61:-1]
            box_high = box_range.max()

            if not (curr['Close'] > box_high and curr['Close'] < box_high * 1.15):
                continue
            stats["breakout_pass"] += 1

            # 4) 거래량: 전일이 아니라 20일 평균 대비
            vol_avg20 = curr['VolAvg20']
            vol_ratio = curr['Volume'] / vol_avg20 if pd.notna(vol_avg20) and vol_avg20 > 0 else 0
            if vol_ratio < 1.3:
                continue
            stats["volume_pass"] += 1

            print(f"  -> 후보 포착: {name} / 거래량 {vol_ratio:.1f}배 / RSI {curr['RSI']:.1f}")

            candidates.append({
                'code': code,
                'name': name,
                'price': curr['Close'],
                'vol_ratio': vol_ratio,
                'rsi': curr['RSI'],
                'ma60': curr['MA60'],
                'ma20': curr['MA20'],
            })

        except Exception as e:
            stats["errors"] += 1
            print(f"[{market_name}] {name} ({code}) 에러: {e}")
            continue

    print(f"[{market_name}] stats={stats}")

    if not candidates:
        return [], stats

    candidates.sort(key=lambda x: x['vol_ratio'], reverse=True)
    final_picks = candidates[:5]

    msg_list = []
    for p in final_picks:
        currency = "$" if market_name in ['S&P500', 'NASDAQ'] else "원"
        if currency == "$":
            link = f"https://m.stock.naver.com/worldstock/stock/{p['code']}/total"
        else:
            link = f"https://m.stock.naver.com/domestic/stock/{p['code']}/total"

        msg = (
            f"🏆 {p['name']} ({p['code']})\n"
            f"가: {p['price']:,.0f}{currency}\n"
            f"힘: 거래량 {p['vol_ratio']:.1f}배 / RSI {p['rsi']:.0f}\n"
            f"손(60일): {int(p['ma60']):,} / 익(20일): {int(p['ma20']):,}\n"
            f"{link}"
        )
        msg_list.append(msg)

    return msg_list, stats

def main():
    print("🚀 Top-Ranking 봇 실행...")
    send_telegram(
        f"🚀 {datetime.now().strftime('%Y-%m-%d')} 주도주 Top 5 리포트 🚀\n"
        f"(RSI+박스돌파+거래량랭킹)"
    )

    all_picks = []
    debug_lines = []

    # 1) 한국 시장
    try:
        kospi_list = fdr.StockListing('KOSPI').head(300)
        kosdaq_list = fdr.StockListing('KOSDAQ').head(500)

        k_picks, k_stats = analyze_market_ranking('KOSPI', kospi_list)
        q_picks, q_stats = analyze_market_ranking('KOSDAQ', kosdaq_list)

        debug_lines.append(f"KOSPI stats: {k_stats}")
        debug_lines.append(f"KOSDAQ stats: {q_stats}")

        if k_picks:
            all_picks.append("\n🔴 [KOSPI Top 5]")
            all_picks.extend(k_picks)

        if q_picks:
            all_picks.append("\n🔵 [KOSDAQ Top 5]")
            all_picks.extend(q_picks)

    except Exception as e:
        print(f"한국장 에러: {e}")
        debug_lines.append(f"한국장 에러: {e}")

    # 2) 미국 시장
    try:
        sp500_list = fdr.StockListing('S&P500')

        us_picks, us_stats = analyze_market_ranking('S&P500', sp500_list)
        debug_lines.append(f"S&P500 stats: {us_stats}")

        if us_picks:
            all_picks.append("\n🇺🇸 [US S&P500 Top 5]")
            all_picks.extend(us_picks)

    except Exception as e:
        print(f"미국장 에러: {e}")
        debug_lines.append(f"미국장 에러: {e}")

    # 3) 전송
    if not all_picks:
        msg = "오늘은 쉴 때입니다. (조건 만족 종목 없음)\n\n" + "\n".join(debug_lines[:10])
        send_telegram(msg)
        return

    msg_buffer = ""
    for item in all_picks:
        if len(msg_buffer) + len(item) > 3000:
            send_telegram(msg_buffer)
            msg_buffer = ""
        msg_buffer += item + "\n\n"

    if msg_buffer:
        send_telegram(msg_buffer)

    # 디버그도 마지막에 전송
    debug_msg = "[DEBUG]\n" + "\n".join(debug_lines[:20])
    send_telegram(debug_msg)

    print("✅ 완료")

if __name__ == "__main__":
    main()
