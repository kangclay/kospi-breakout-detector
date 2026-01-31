import FinanceDataReader as fdr
import pandas as pd
import requests
import os
import sys
import io
from datetime import datetime, timedelta
import time

# 1. 설정
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram(message):
    """메시지 전송 함수"""
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': CHAT_ID, 'text': message}
    try:
        requests.post(url, data=data)
        time.sleep(1)
    except Exception as e:
        print(f"전송 에러: {e}")

def analyze_market(market_name, ticker_list):
    """시장별 분석 함수 (한국/미국 통합)"""
    print(f"\n[{market_name}] {len(ticker_list)}개 종목 분석 시작...")
    
    results = []
    
    # 미국 주식은 50일선 손절이 더 잘 맞으므로 로직 분기 처리 가능
    # 여기서는 검증된 공통 로직(3달 박스권 + 2배 거래량) 사용
    
    for idx, row in ticker_list.iterrows():
        # 한국/미국 컬럼명 차이 처리
        if 'Symbol' in row: code = row['Symbol'] # 미국
        else: code = row['Code'] # 한국
            
        name = row['Name']
        
        try:
            # 150일치 데이터 (미국장은 가끔 데이터가 늦게 들어올 수 있어 예외처리)
            df = fdr.DataReader(code, start=datetime.now() - timedelta(days=150))
            if len(df) < 120: continue
            
            # 지표 계산
            df['MA10'] = df['Close'].rolling(window=10).mean()
            df['MA20'] = df['Close'].rolling(window=20).mean()
            df['MA60'] = df['Close'].rolling(window=60).mean() # 60일(분기선)
            
            curr = df.iloc[-1]
            prev = df.iloc[-2]
            
            # [조건 1] 정배열 (10 > 20 > 60)
            if not (curr['MA10'] > curr['MA20'] > curr['MA60']): continue
            
            # [조건 2] 수급: 양봉 20개 이상 (매집 흔적)
            recent_40 = df.iloc[-40:]
            green_cnt = len(recent_40[recent_40['Close'] > recent_40['Open']])
            if green_cnt < 20: continue 
            
            # [조건 3] 60일(3달) 박스권 돌파
            box_range = df['High'].iloc[-61:-1]
            box_high = box_range.max()
            
            # 오늘 종가가 신고가 돌파 (15% 이상 급등은 추격매수 주의)
            if curr['Close'] > box_high and curr['Close'] < box_high * 1.15:
                
                # [조건 4] 거래량 폭발 (미국은 1.5배, 한국은 2.0배 적용)
                # 시장별로 거래량 특성이 다르므로 유동적 적용
                vol_multiplier = 1.5 if market_name in ['S&P500', 'NASDAQ'] else 2.0
                
                if curr['Volume'] > prev['Volume'] * vol_multiplier:
                    print(f"💎 포착: {name}")
                    
                    # 화폐 단위 표시
                    currency = "$" if market_name in ['S&P500', 'NASDAQ'] else "원"
                    
                    # 네이버 증권 링크 (해외/국내 구분)
                    if currency == "$":
                        link = f"https://m.stock.naver.com/worldstock/stock/{code}/total"
                    else:
                        link = f"https://m.stock.naver.com/domestic/stock/{code}/total"

                    msg = (f"💎 {name} ({code})\n"
                           f"가: {curr['Close']:,.0f}{currency}\n"
                           f"거: 전일대비 {int(curr['Volume']/prev['Volume']*100)}%\n"
                           f"손(60일): {int(curr['MA60']):,.0f}\n"
                           f"익(20일): {int(curr['MA20']):,.0f} 깨지면\n"
                           f"{link}")
                    results.append(msg)
        except:
            continue
            
    return results

def main():
    print("🚀 글로벌 주식 비서 실행...")
    send_telegram(f"🚀 {datetime.now().strftime('%Y-%m-%d')} 글로벌 주도주 리포트 🚀")
    
    all_picks = []

    # 1. 한국 시장 (KOSPI / KOSDAQ)
    # 속도를 위해 테스트 시엔 head(100) 유지, 실전엔 제거
    try:
        kospi_list = fdr.StockListing('KOSPI') #.head(200) 
        kosdaq_list = fdr.StockListing('KOSDAQ') #.head(200)
        
        k_picks = analyze_market('KOSPI', kospi_list)
        q_picks = analyze_market('KOSDAQ', kosdaq_list)
        
        if k_picks: all_picks.append("\n🔴 [KOSPI]") + all_picks.extend(k_picks)
        if q_picks: all_picks.append("\n🔵 [KOSDAQ]") + all_picks.extend(q_picks)
    except Exception as e:
        print(f"한국장 분석 중 에러: {e}")

    # 2. 미국 시장 (S&P500)
    # NASDAQ 전체는 너무 많아서(4000개) S&P500과 NASDAQ100 위주로 봄
    try:
        sp500_list = fdr.StockListing('S&P500')
        # S&P500은 종목 수가 적절(500개)하여 전체 스캔 가능
        us_picks = analyze_market('S&P500', sp500_list)
        
        if us_picks: 
            all_picks.append("\n🇺🇸 [US S&P500]")
            all_picks.extend(us_picks)
    except Exception as e:
        print(f"미국장 분석 중 에러: {e}")

    # 3. 결과 전송
    if not all_picks:
        send_telegram("오늘은 전 세계적으로 쉴 때입니다. (발굴 종목 없음)")
        return

    # 분할 전송
    msg_buffer = ""
    for item in all_picks:
        if len(msg_buffer) + len(item) > 3000:
            send_telegram(msg_buffer)
            msg_buffer = ""
        msg_buffer += item + "\n\n"
        
    if msg_buffer:
        send_telegram(msg_buffer)

    print("✅ 분석 완료")

if __name__ == "__main__":
    main()
