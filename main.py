import FinanceDataReader as fdr
import pandas as pd
import requests
import os
import sys
import io
from datetime import datetime, timedelta
import time
import numpy as np

# 1. 설정
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': CHAT_ID, 'text': message, 'disable_web_page_preview': True}
    try:
        requests.post(url, data=data)
        time.sleep(1)
    except Exception as e:
        print(f"전송 에러: {e}")

# RSI 계산 함수 (백테스트와 동일 로직)
def calculate_rsi(series, period=14):
    delta = series.diff(1)
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def analyze_market_ranking(market_name, ticker_list):
    print(f"\n[{market_name}] 랭킹 시스템 가동 중...")
    
    candidates = [] # 1차 합격자 저장소
    
    for idx, row in ticker_list.iterrows():
        if 'Symbol' in row: code = row['Symbol'] # 미국
        else: code = row['Code'] # 한국
        name = row['Name']
        
        try:
            # 백테스트와 동일하게 150일 데이터 확보
            df = fdr.DataReader(code, start=datetime.now() - timedelta(days=150))
            if len(df) < 120: continue
            
            # --- 지표 계산 ---
            df['MA10'] = df['Close'].rolling(window=10).mean()
            df['MA20'] = df['Close'].rolling(window=20).mean()
            df['MA60'] = df['Close'].rolling(window=60).mean()
            df['RSI'] = calculate_rsi(df['Close'])
            
            curr = df.iloc[-1]
            prev = df.iloc[-2]
            
            # [필터 1] 정배열 + RSI 50 이상 (추세 살아있음)
            if not (curr['MA10'] > curr['MA20'] > curr['MA60']): continue
            if curr['RSI'] < 50: continue

            # [필터 2] 수급: 양봉 20개 이상 (매집 흔적)
            recent_40 = df.iloc[-40:]
            green_cnt = len(recent_40[recent_40['Close'] > recent_40['Open']])
            if green_cnt < 20: continue 
            
            # [필터 3] 60일 박스권 돌파
            box_range = df['High'].iloc[-61:-1]
            box_high = box_range.max()
            
            # 15% 이상 급등은 추격매수 위험으로 제외
            if curr['Close'] > box_high and curr['Close'] < box_high * 1.15:
                
                # [필터 4] 거래량 폭발 (최소 1.5배)
                vol_ratio = curr['Volume'] / prev['Volume']
                if vol_ratio >= 1.5:
                    # 1차 합격! 후보군에 등록
                    print(f"  -> 후보 포착: {name} (거래량 {vol_ratio:.1f}배)")
                    
                    candidates.append({
                        'code': code,
                        'name': name,
                        'price': curr['Close'],
                        'vol_ratio': vol_ratio, # 랭킹 산정 기준
                        'rsi': curr['RSI'],
                        'ma60': curr['MA60'],
                        'ma20': curr['MA20']
                    })
        except:
            continue
            
    # --- [Top 5 랭킹 선별] ---
    if not candidates:
        return []
        
    # 거래량 증가율(폭발력) 순으로 내림차순 정렬
    candidates.sort(key=lambda x: x['vol_ratio'], reverse=True)
    
    # 상위 5개만 최종 선발
    final_picks = candidates[:5]
    
    # 메시지 생성
    msg_list = []
    for p in final_picks:
        currency = "$" if market_name in ['S&P500', 'NASDAQ'] else "원"
        if currency == "$":
            link = f"https://m.stock.naver.com/worldstock/stock/{p['code']}/total"
        else:
            link = f"https://m.stock.naver.com/domestic/stock/{p['code']}/total"
            
        msg = (f"🏆 {p['name']} ({p['code']})\n"
               f"가: {p['price']:,.0f}{currency}\n"
               f"힘: 거래량 {p['vol_ratio']:.1f}배 / RSI {p['rsi']:.0f}\n"
               f"손(60일): {int(p['ma60']):,.0f} / 익(20일): {int(p['ma20']):,.0f}\n"
               f"{link}")
        msg_list.append(msg)
        
    return msg_list

def main():
    print("🚀 Top-Ranking 봇 실행...")
    send_telegram(f"🚀 {datetime.now().strftime('%Y-%m-%d')} 주도주 Top 5 리포트 🚀\n(RSI+박스돌파+거래량랭킹)")
    
    all_picks = []

    # 1. 한국 시장 (KOSPI / KOSDAQ) - 상위 200개 대상 (백테스트 환경과 유사하게)
    try:
        kospi_list = fdr.StockListing('KOSPI').head(200)
        kosdaq_list = fdr.StockListing('KOSDAQ').head(200)
        
        k_picks = analyze_market_ranking('KOSPI', kospi_list)
        q_picks = analyze_market_ranking('KOSDAQ', kosdaq_list)
        
        if k_picks: all_picks.append("\n🔴 [KOSPI Top 5]") + all_picks.extend(k_picks)
        if q_picks: all_picks.append("\n🔵 [KOSDAQ Top 5]") + all_picks.extend(q_picks)
    except Exception as e:
        print(f"한국장 에러: {e}")

    # 2. 미국 시장 (S&P500)
    try:
        sp500_list = fdr.StockListing('S&P500')
        us_picks = analyze_market_ranking('S&P500', sp500_list)
        
        if us_picks: 
            all_picks.append("\n🇺🇸 [US S&P500 Top 5]")
            all_picks.extend(us_picks)
    except Exception as e:
        print(f"미국장 에러: {e}")

    # 3. 전송
    if not all_picks:
        send_telegram("오늘은 쉴 때입니다. (조건 만족 종목 없음)")
        return

    msg_buffer = ""
    for item in all_picks:
        if len(msg_buffer) + len(item) > 3000:
            send_telegram(msg_buffer)
            msg_buffer = ""
        msg_buffer += item + "\n\n"
        
    if msg_buffer:
        send_telegram(msg_buffer)

    print("✅ 완료")

if __name__ == "__main__":
    main()
