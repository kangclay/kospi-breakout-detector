import FinanceDataReader as fdr
import pandas as pd
import requests
import os
from datetime import datetime, timedelta

# --- [설정] ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def send_telegram(message):
    """텔레그램 메시지 전송 함수"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("텔레그램 토큰 설정이 안되어 있어 메시지를 보낼 수 없습니다.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # 메시지가 너무 길면 잘릴 수 있으므로 나눠서 전송하는 로직이 있으면 좋지만, 여기선 단순화
    data = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'Markdown', 'disable_web_page_preview': True}
    
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"전송 실패: {e}")

def get_bullish_stocks(market):
    """특정 시장(KOSPI/KOSDAQ)에서 매수 신호 종목 발굴"""
    print(f"\n[{market}] 분석 시작...")
    
    # 1. 종목 리스트 가져오기
    stocks = fdr.StockListing(market)
    
    # [시간 단축 팁] 전체를 다 돌면 GitHub Actions 제한시간에 걸릴 수 있으므로
    # 시가총액 상위 500개만 먼저 테스트해보는 것을 추천합니다.
    # stocks = stocks.head(500) # 주석을 풀면 상위 500개만 분석
    
    results = []
    
    for idx, row in stocks.iterrows():
        code = row['Code']
        name = row['Name']
        
        try:
            # 2. 최근 60일치 데이터 가져오기
            df = fdr.DataReader(code, start=datetime.now() - timedelta(days=100))
            
            if len(df) < 60: continue # 데이터 부족하면 패스
            if df.iloc[-1]['Close'] < 1000: continue # 1000원 미만 동전주 패스
            
            # 3. 기술적 지표 계산 (이동평균선)
            df['MA5'] = df['Close'].rolling(window=5).mean()
            df['MA20'] = df['Close'].rolling(window=20).mean()
            
            today = df.iloc[-1]
            yesterday = df.iloc[-2]
            
            # 4. 매수 조건 체크
            
            # (1) 거래량 급증: 오늘 거래량 >= 어제 거래량 * 2배
            if today['Volume'] < yesterday['Volume'] * 2.0: continue
            if today['Volume'] < 50000: continue # 절대 거래량이 너무 적으면 패스

            # (2) 골든크로스: 어제는 5일선이 20일선 아래, 오늘은 위
            # (확실한 돌파를 위해 오늘 5일선이 20일선보다 조금이라도 커야 함)
            is_goldencross = (yesterday['MA5'] <= yesterday['MA20']) and (today['MA5'] > today['MA20'])
            
            if is_goldencross:
                print(f"포착: {name}")
                # 변동률 계산
                change_rate = (today['Close'] - yesterday['Close']) / yesterday['Close'] * 100
                
                results.append(
                    f"🔥 *{name}* ({code})\n"
                    f"현재가: {today['Close']:,}원 ({change_rate:.1f}%)\n"
                    f"거래량: 전일대비 {int(today['Volume']/yesterday['Volume']*100)}% 폭발\n"
                    f"[네이버증권 바로가기](https://m.stock.naver.com/domestic/stock/{code}/total)"
                )
                
        except Exception:
            continue
            
    return results

def main():
    report = []
    header = f"📊 *{datetime.now().strftime('%Y-%m-%d')} 주식 매수 포착 리포트* 📊\n"
    report.append(header)
    
    # 코스피, 코스닥 분석
    kospi_picks = get_bullish_stocks('KOSPI')
    kosdaq_picks = get_bullish_stocks('KOSDAQ')
    
    if kospi_picks:
        report.append(f"\n🔴 *KOSPI 포착 ({len(kospi_picks)}개)*")
        report.extend(kospi_picks)
    
    if kosdaq_picks:
        report.append(f"\n🔵 *KOSDAQ 포착 ({len(kosdaq_picks)}개)*")
        report.extend(kosdaq_picks)
        
    if not kospi_picks and not kosdaq_picks:
        report.append("\n오늘은 조건에 맞는 종목이 없습니다.")
    
    # 최종 메시지 전송 (리스트를 문자열로 합침)
    full_message = "\n\n".join(report)
    send_telegram(full_message)

if __name__ == "__main__":
    main()
