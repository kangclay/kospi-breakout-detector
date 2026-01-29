import FinanceDataReader as fdr
import pandas as pd
import requests
import os
from datetime import datetime, timedelta

# 텔레그램 설정 (기존과 동일)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram(message):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown'}
    try: requests.post(url, data=data)
    except: pass

def get_flag_pattern_stocks(market):
    print(f"\n[{market}] '단테 스타일' 깃발형 돌파 매매 분석 중...")
    stocks = fdr.StockListing(market)
     stocks = stocks.head(100) # 테스트 시 주석 해제 (속도 향상)
    
    results = []
    
    for idx, row in stocks.iterrows():
        code = row['Code']
        name = row['Name']
        
        try:
            # 최근 120일 데이터 조회
            df = fdr.DataReader(code, start=datetime.now() - timedelta(days=120))
            if len(df) < 60: continue
            
            # 1. 이평선 계산 (10, 20, 50일)
            df['MA10'] = df['Close'].rolling(window=10).mean()
            df['MA20'] = df['Close'].rolling(window=20).mean()
            df['MA50'] = df['Close'].rolling(window=50).mean()
            
            curr = df.iloc[-1]      # 오늘
            prev = df.iloc[-2]      # 어제
            
            # [조건 1] 이평선 정배열 (10 > 20 > 50)
            if not (curr['MA10'] > curr['MA20'] > curr['MA50']): continue
            
            # [조건 2] 강한 상승 추세 확인 (최근 40봉 중 양봉이 15개 이상인가?)
            # 영상: "녹색 캔들 15개 이상"
            recent_40_days = df.iloc[-40:]
            green_candles = recent_40_days[recent_40_days['Close'] > recent_40_days['Open']]
            if len(green_candles) < 15: continue
            
            # [조건 3] 횡보 박스권 돌파 (Breakout)
            # 최근 5일~20일 사이의 최고가(박스 상단)를 계산
            # 어제까지의 최근 10일간 최고가
            box_range = df['High'].iloc[-12:-1] 
            box_high = box_range.max()
            
            # 오늘 종가가 박스 상단을 돌파했는가?
            # (동시에 너무 많이 오른 건 이미 늦었으니 제외 - 29% 상한가 등)
            if curr['Close'] > box_high and curr['Close'] < box_high * 1.15:
                
                # [조건 4] 거래량 실림 (선택사항, 영상엔 없지만 신뢰도 상승용)
                # 돌파할 때 거래량이 평소보다 좀 더 실리면 좋음
                if curr['Volume'] > prev['Volume']:
                    print(f"포착: {name}")
                    
                    stop_loss = int(curr['MA50']) # 영상 조건: 50일선 이탈 시 손절
                    take_profit_line = int(curr['MA20']) # 영상 조건: 20일선 이탈 시 익절
                    
                    results.append(
                        f"🚩 *{name}* ({code})\n"
                        f"가격: {curr['Close']:,}원 (박스권 돌파!)\n"
                        f"손절가(50일선): {stop_loss:,}원\n"
                        f"익절기준(20일선): {take_profit_line:,}원 깨지면 매도\n"
                        f"[차트보기](https://m.stock.naver.com/domestic/stock/{code}/total)"
                    )
                    
        except Exception:
            continue
            
    return results

def main():
    report = []
    header = f"🚀 *{datetime.now().strftime('%Y-%m-%d')} 깃발형 돌파 매매 리포트* 🚀\n(조건: 정배열 + 양봉다수 + 박스권돌파)"
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
        report.append("\n오늘은 조건에 맞는 종목이 없습니다.")
        
    full_msg = "\n\n".join(report)
    send_telegram(full_msg)

if __name__ == "__main__":
    main()
