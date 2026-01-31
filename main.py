import FinanceDataReader as fdr
import pandas as pd
import requests
import os
import sys
import io
from datetime import datetime, timedelta
import time

# 1. 인코딩 및 텔레그램 설정
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram(message):
    """메시지 전송 함수 (분할 전송 대응)"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': CHAT_ID, 'text': message}
    
    try:
        requests.post(url, data=data)
        time.sleep(1) # 도배 방지
    except Exception as e:
        print(f"전송 에러: {e}")

def get_strong_trend_stocks(market):
    print(f"\n[{market}] 강화된 깃발형 패턴 분석 중...")
    try:
        stocks = fdr.StockListing(market)
    except:
        return []

    # [실전용] 코스피/코스닥 전 종목 대상 (주석 처리 제거함)
    # stocks = stocks.head(200) 
    
    results = []
    for idx, row in stocks.iterrows():
        code = row['Code']
        name = row['Name']
        
        try:
            # 3달 박스권 + 60일선 확인을 위해 150일 데이터 필요
            df = fdr.DataReader(code, start=datetime.now() - timedelta(days=150))
            if len(df) < 120: continue
            
            # 지표 계산
            df['MA10'] = df['Close'].rolling(window=10).mean()
            df['MA20'] = df['Close'].rolling(window=20).mean()
            df['MA60'] = df['Close'].rolling(window=60).mean()
            
            curr = df.iloc[-1]
            prev = df.iloc[-2]
            
            # [조건 1] 이평선 정배열 (10 > 20 > 60)
            if not (curr['MA10'] > curr['MA20'] > curr['MA60']): continue
            
            # [조건 2] 수급: 최근 40일 중 양봉 20개 이상 (백테스트 검증 완료)
            recent_40 = df.iloc[-40:]
            green_cnt = len(recent_40[recent_40['Close'] > recent_40['Open']])
            if green_cnt < 20: continue 
            
            # [조건 3] 60일(3달) 박스권 돌파
            # 오늘을 제외한 과거 60일간의 고점
            box_range = df['High'].iloc[-61:-1] 
            box_high = box_range.max()
            
            # 오늘 종가가 박스 상단을 돌파했는가? (15% 이상 급등은 추격매수 위험으로 제외)
            if curr['Close'] > box_high and curr['Close'] < box_high * 1.15:
                
                # [조건 4] 거래량 폭발 (전일 대비 200% 이상)
                if curr['Volume'] > prev['Volume'] * 2.0:
                    print(f"💎 포착: {name}")
                    
                    # 메시지 포맷
                    msg = (f"💎 {name} ({code})\n"
                           f"가: {int(curr['Close']):,}원\n"
                           f"거: 전일대비 {int(curr['Volume']/prev['Volume']*100)}%\n"
                           f"손절(60일): {int(curr['MA60']):,}원\n"
                           f"익절(20일): {int(curr['MA20']):,}원 깨지면\n"
                           f"https://m.stock.naver.com/domestic/stock/{code}/total")
                    results.append(msg)
        except:
            continue
            
    return results

def main():
    print("🚀 봇 실행 시작...")
    
    # 1. 시작 알림
    header = f"🚀 {datetime.now().strftime('%Y-%m-%d')} 주도주 리포트 🚀\n(조건: 양봉20 + 3달박스돌파 + 거래량2배)"
    send_telegram(header)
    
    # 2. 종목 발굴
    kospi = get_strong_trend_stocks('KOSPI')
    kosdaq = get_strong_trend_stocks('KOSDAQ')
    
    all_picks = []
    if kospi: 
        all_picks.append("\n🔴 [KOSPI]")
        all_picks.extend(kospi)
    if kosdaq: 
        all_picks.append("\n🔵 [KOSDAQ]")
        all_picks.extend(kosdaq)
    
    # 3. 결과 전송 (없으면 없다고 알림)
    if not kospi and not kosdaq:
        send_telegram("오늘은 조건에 맞는 대장주가 없습니다. (휴식 권장)")
        return

    # 4. 메시지 분할 전송 (3000자 단위로 끊어서)
    msg_buffer = ""
    for item in all_picks:
        if len(msg_buffer) + len(item) > 3000:
            send_telegram(msg_buffer)
            msg_buffer = ""
        msg_buffer += item + "\n\n"
        
    if msg_buffer:
        send_telegram(msg_buffer)

    print("✅ 분석 및 전송 완료")

if __name__ == "__main__":
    main()
