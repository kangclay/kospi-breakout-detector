import FinanceDataReader as fdr
import pandas as pd
import requests
import os
import sys
import io
from datetime import datetime, timedelta

# 한글 출력 에러 방지
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 텔레그램 설정
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("텔레그램 토큰이 없습니다.")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # parse_mode 제거 (에러 방지용)
    data = {'chat_id': CHAT_ID, 'text': message}
    
    try:
        response = requests.post(url, data=data)
        if response.status_code != 200:
            print(f"전송 실패: {response.text}")
    except Exception as e:
        print(f"전송 에러: {e}")

def get_flag_pattern_stocks(market):
    print(f"\n[{market}] 분석 시작...")
    
    try:
        stocks = fdr.StockListing(market)
    except Exception as e:
        print(f"종목 리스트 다운로드 실패: {e}")
        return []
        
    # [테스트용] 속도를 위해 상위 100개만
    stocks = stocks.head(100)
    
    results = []
    
    for idx, row in stocks.iterrows():
        code = row['Code']
        name = row['Name']
        
        try:
            # 최근 120일 데이터 조회
            df = fdr.DataReader(code, start=datetime.now() - timedelta(days=120))
            if len(df) < 60: continue
            
            # 지표 계산
            df['MA10'] = df['Close'].rolling(window=10).mean()
            df['MA20'] = df['Close'].rolling(window=20).mean()
            df['MA50'] = df['Close'].rolling(window=50).mean()
            
            curr = df.iloc[-1]
            prev = df.iloc[-2]
            
            # [조건 1] 정배열
            if not (curr['MA10'] > curr['MA20'] > curr['MA50']): continue
            
            # [조건 2] 양봉 15개 이상
            recent_40 = df.iloc[-40:]
            green_cnt = len(recent_40[recent_40['Close'] > recent_40['Open']])
            if green_cnt < 15: continue
            
            # [조건 3] 박스권 돌파
            box_range = df['High'].iloc[-12:-1]
            box_high = box_range.max()
            
            if curr['Close'] > box_high and curr['Close'] < box_high * 1.15:
                # [조건 4] 거래량 증가 확인 (선택)
                if curr['Volume'] > prev['Volume']:
                    print(f"포착: {name}")
                    
                    results.append(
                        f"🚩 {name} ({code})\n"
                        f"가격: {curr['Close']:,}원\n"
                        f"손절가(50일): {int(curr['MA50']):,}원\n"
                        f"익절가(20일): {int(curr['MA20']):,}원\n"
                        f"https://m.stock.naver.com/domestic/stock/{code}/total"
                    )
                    
        except Exception:
            continue
            
    return results

def main():
    report = []
    header = f"🚀 {datetime.now().strftime('%Y-%m-%d')} 추천 리포트 🚀"
    report.append(header)
    
    kospi = get_flag_pattern_stocks('KOSPI')
    kosdaq = get_flag_pattern_stocks('KOSDAQ')
    
    if kospi:
        report.append(f"\n🔴 KOSPI ({len(kospi)}개)")
        report.extend(kospi)
    if kosdaq:
        report.append(f"\n🔵 KOSDAQ ({len(kosdaq)}개)")
        report.extend(kosdaq)
        
    if not kospi and not kosdaq:
        report.append("\n조건 만족 종목 없음")
        
    # 하나로 합쳐서 전송
    full_msg = "\n\n".join(report)
    print("텔레그램 전송 시도...")
    send_telegram(full_msg)

if __name__ == "__main__":
    main()
