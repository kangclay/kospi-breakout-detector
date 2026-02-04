import FinanceDataReader as fdr
import pandas as pd
import requests
import os
import sys
import io
from datetime import datetime, timedelta
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# 1. 설정
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# 구글 시트 설정 (본인의 환경에 맞게 수정)
JSON_FILE = 'credentials.json' 
SHEET_NAME = '주식알림기록'      

def save_to_google_sheet(data_list):
    """구글 시트에 분석 결과 기록 (gspread 사용)"""
    if not data_list:
        return
    
    try:
        # 인증 및 시트 열기
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_FILE, scope)
        client = gspread.authorize(creds)
        
        # 시트 이름으로 열기
        spreadsheet = client.open(SHEET_NAME)
        sheet = spreadsheet.get_worksheet(0) # 첫 번째 탭
        
        # 데이터 추가 (append_rows는 여러 줄을 한 번에 추가합니다)
        sheet.append_rows(data_list)
        print(f"📊 구글 시트에 {len(data_list)}건 기록 완료")
        
    except Exception as e:
        print(f"구글 시트 기록 에러: {e}")

def send_telegram(message):
    """메시지 전송 함수"""
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {'chat_id': CHAT_ID, 'text': message}
    try:
        requests.post(url, data=data)
        time.sleep(1) # 전송 안정성을 위한 대기
    except Exception as e:
        print(f"전송 에러: {e}")

def analyze_market(market_name, ticker_list):
    """시장별 분석 및 시트 데이터 생성"""
    print(f"\n[{market_name}] {len(ticker_list)}개 종목 분석 시작...")
    
    results = []
    sheet_rows = []
    
    for idx, row in ticker_list.iterrows():
        code = row['Symbol'] if 'Symbol' in row else row['Code']
        name = row['Name']
        
        try:
            df = fdr.DataReader(code, start=(datetime.now() - timedelta(days=200)).strftime('%Y-%m-%d'))
            if len(df) < 120: continue
            
            # 지표 계산
            df['MA10'] = df['Close'].rolling(window=10).mean()
            df['MA20'] = df['Close'].rolling(window=20).mean()
            df['MA60'] = df['Close'].rolling(window=60).mean()
            
            curr = df.iloc[-1]
            prev = df.iloc[-2]
            
            # [조건 1] 정배열
            if not (curr['MA10'] > curr['MA20'] > curr['MA60']): continue
            
            # [조건 2] 수급 (최근 40일 중 양봉 20개 이상)
            recent_40 = df.iloc[-40:]
            if len(recent_40[recent_40['Close'] > recent_40['Open']]) < 20: continue 
            
            # [조건 3] 박스권 돌파
            box_high = df['High'].iloc[-61:-1].max()
            
            if curr['Close'] > box_high and curr['Close'] < box_high * 1.15:
                # [조건 4] 거래량 폭발
                vol_mul = 1.5 if market_name in ['S&P500', 'NASDAQ'] else 2.0
                vol_ratio = int(curr['Volume']/prev['Volume']*100)
                
                if curr['Volume'] > prev['Volume'] * vol_mul:
                    currency = "$" if market_name in ['S&P500', 'NASDAQ'] else "원"
                    link = f"https://m.stock.naver.com/{'world' if currency=='$' else 'domestic'}/stock/{code}/total"

                    # 텔레그램 메시지
                    msg = (f"💎 {name} ({code})\n"
                           f"가: {curr['Close']:,.0f}{currency}\n"
                           f"거: {vol_ratio}%\n"
                           f"{link}")
                    results.append(msg)
                    
                    # 구글 시트 데이터 행 (날짜, 시장, 이름, 코드, 가격, 거래량비율, 링크)
                    sheet_rows.append([
                        datetime.now().strftime('%Y-%m-%d %H:%M'),
                        market_name, name, code, curr['Close'], f"{vol_ratio}%", link
                    ])
        except:
            continue
            
    return results, sheet_rows

def main():
    print("🚀 글로벌 주식 비서 실행...")
    send_telegram(f"🚀 {datetime.now().strftime('%Y-%m-%d')} 주도주 분석 리포트")
    
    all_picks = []
    all_sheet_data = []

    # 분석 대상 설정
    market_targets = [
        ('KOSPI', 'KOSPI'),
        ('KOSDAQ', 'KOSDAQ'),
        ('S&P500', 'S&P500')
    ]

    for label, fdr_code in market_targets:
        try:
            target_list = fdr.StockListing(fdr_code)
            picks, rows = analyze_market(label, target_list)
            
            if picks:
                all_picks.append(f"\n📍 [{label}]")
                all_picks.extend(picks)
                all_sheet_data.extend(rows)
        except Exception as e:
            print(f"{label} 분석 에러: {e}")

    # 결과 처리
    if all_picks:
        # 1. 텔레그램 전송
        msg_buffer = ""
        for item in all_picks:
            if len(msg_buffer) + len(item) > 3500:
                send_telegram(msg_buffer)
                msg_buffer = ""
            msg_buffer += item + "\n\n"
        if msg_buffer:
            send_telegram(msg_buffer)
            
        # 2. 구글 시트 기록
        save_to_google_sheet(all_sheet_data)
    else:
        send_telegram("오늘은 발굴된 종목이 없습니다.")

    print("✅ 모든 작업 완료")

if __name__ == "__main__":
    main()
